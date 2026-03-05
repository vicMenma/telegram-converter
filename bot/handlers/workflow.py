"""
Main workflow
─────────────
States:
  "choosing_operation"   — video received, waiting for button press
  "waiting_for_subtitle" — waiting for .srt/.ass/… file
"""

import os
import asyncio
import re
import time
import uuid
import logging
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from client import app
from user_client import get_user_client
from utils.settings import get as user_setting
from config import (
    TEMP_DIR,
    VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
)
from utils.file_utils import format_size, output_filename, cleanup
from utils.queue import register, update_status, set_task, finish
from processors.ffmpeg import burn_subtitles, download_url
from processors.leech import ytdlp_download, detect_link_type

logger = logging.getLogger(__name__)

# ── In-memory state ───────────────────────────────────────────────
STATE: dict[int, dict] = {}
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


# ── Operation keyboard ────────────────────────────────────────────
def operation_keyboard(mode: str = "upload"):
    EXTRAS = [
        InlineKeyboardButton("📊 MediaInfo", callback_data="op:mediainfo"),
        InlineKeyboardButton("🎵 Streams",   callback_data="op:streams"),
    ]
    CANCEL = [InlineKeyboardButton("✕ Cancel", callback_data="op:cancel")]

    if mode == "upload":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔤 Burn Subtitles", callback_data="op:subtitles")],
            EXTRAS,
            CANCEL,
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇︎ Download Only", callback_data="op:leech")],
            [InlineKeyboardButton("🔤 Burn Subtitles", callback_data="op:subtitles")],
            EXTRAS,
            CANCEL,
        ])


# ── Progress callback ─────────────────────────────────────────────
def make_progress(msg, action: str, known_total: int = 0):
    import time
    state = {"time": 0, "start": time.time()}

    async def progress(current, total):
        now = time.time()
        if now - state["time"] < 3:
            return
        state["time"] = now
        real_total = total if (total and total > 0) else known_total
        elapsed    = max(now - state["start"], 0.1)
        speed      = current / elapsed
        speed_str  = f"{format_size(int(speed))}/s"
        if real_total > 0:
            pct     = min(int(current * 100 / real_total), 99)
            bar     = "█" * (pct // 5) + "░" * (20 - pct // 5)
            eta     = int((real_total - current) / speed) if speed > 0 else 0
            eta_str = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"
            text = (
                f"<b>{action}</b>\n\n"
                f"<code>{bar}</code> <b>{pct}%</b>\n"
                f"📦 {format_size(current)} / {format_size(real_total)}\n"
                f"🚀 {speed_str}  ·  ⏱ {eta_str}"
            )
        else:
            text = (
                f"<b>{action}</b>\n\n"
                f"📥 {format_size(current)} transferred\n"
                f"🚀 {speed_str}"
            )
        try:
            await msg.edit(text)
        except Exception:
            pass

    return progress


# ── Video accepted ────────────────────────────────────────────────
async def _video_accepted(msg: Message, source: str, file_id: str = "",
                           file_name: str = "", file_size: int = 0, url: str = ""):
    uid = msg.from_user.id

    if source == "upload":
        desc = f"📁 <code>{file_name}</code>  ·  {format_size(file_size)}"
        mode = "upload"
    elif source == "ytdlp":
        short = url[:60] + "…" if len(url) > 60 else url
        desc  = f"📡 <b>HLS Stream</b>\n<code>{short}</code>"
        mode  = "url"
    elif source == "magnet":
        short = url[:60] + "…" if len(url) > 60 else url
        desc  = f"🧲 <b>Magnet link</b>\n<code>{short}</code>"
        mode  = "url"
    else:
        short = url[:60] + "…" if len(url) > 60 else url
        desc  = f"🔗 <code>{short}</code>"
        mode  = "url"

    STATE[uid] = {
        "state":     "choosing_operation",
        "source":    source,
        "file_id":   file_id,
        "file_name": file_name,
        "file_size": file_size,
        "url":       url,
        "mode":      mode,
    }

    await msg.reply(
        f"🎬 <b>Video ready</b>\n\n{desc}\n\nWhat do you want to do?",
        reply_markup=operation_keyboard(mode=mode),
    )


# ── Input: File upload ────────────────────────────────────────────
@app.on_message(filters.private & (filters.video | filters.document))
async def recv_file(client: Client, msg: Message):
    uid  = msg.from_user.id
    data = STATE.get(uid, {})

    if msg.document:
        ext       = Path(msg.document.file_name or "").suffix.lower()
        file_name = msg.document.file_name or "file"
        file_size = msg.document.file_size or 0

        # Subtitle waiting
        if data.get("state") == "waiting_for_subtitle" and ext in SUBTITLE_EXTENSIONS:
            await _process_subtitle(client, msg)
            return

        # .torrent file
        if ext == ".torrent":
            from processors.leech import magnet_download
            from handlers.leech import _upload_file
            job_id   = str(uuid.uuid4())[:8]
            username = msg.from_user.username or msg.from_user.first_name or str(uid)
            register(job_id, uid, username, "torrent", file_name)
            status = await msg.reply(
                f"🌱 <b>Torrent detected</b>\n\n📄 <code>{file_name}</code>\n\n<i>Downloading…</i>"
            )
            torrent_path = os.path.join(TEMP_DIR, f"{job_id}.torrent")
            await client.download_media(msg.document.file_id, file_name=torrent_path)
            path = None
            try:
                update_status(job_id, "🧲 Connecting…")
                path = await magnet_download(torrent_path, job_id, progress_msg=status)
                update_status(job_id, "📤 Uploading…")
                await _upload_file(client, msg, status, path)
            except Exception as e:
                await status.edit(f"❌ <b>Download failed</b>\n\n<code>{str(e)[:200]}</code>")
            finally:
                finish(job_id)
                cleanup(torrent_path, path)
            return

        # Regular video document
        if ext not in VIDEO_EXTENSIONS:
            if ext in SUBTITLE_EXTENSIONS:
                await msg.reply("📝 Send your <b>video</b> first, then I'll ask for the subtitle.")
            else:
                await msg.reply(
                    f"⚠️ Unsupported file (<code>{ext or 'unknown'}</code>)\n\n"
                    "Send a <b>video file</b>, a <b>URL</b>, a <b>magnet link</b>, or a <b>.torrent</b>."
                )
            return

        media     = msg.document
        file_name = msg.document.file_name or "video.mp4"
        file_size = msg.document.file_size or 0
    else:
        media     = msg.video
        file_name = msg.video.file_name or "video.mp4"
        file_size = msg.video.file_size or 0

    if file_size > MAX_FILE_SIZE_BYTES:
        await msg.reply(f"❌ File too large — max 2 GB.\nYour file: <b>{format_size(file_size)}</b>")
        return

    STATE.pop(uid, None)  # clear any previous session
    await _video_accepted(msg, "upload",
                          file_id=media.file_id,
                          file_name=file_name,
                          file_size=file_size)


# ── Input: Text / URL / magnet ────────────────────────────────────
@app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "settings", "stats", "queue"]))
async def recv_text(client: Client, msg: Message):
    uid = msg.from_user.id

    # Let settings handler handle channel input
    try:
        from handlers.settings import _WAITING_CHANNEL
        if uid in _WAITING_CHANNEL:
            return
    except ImportError:
        pass

    data = STATE.get(uid, {})

    # Waiting for subtitle
    if data.get("state") == "waiting_for_subtitle":
        sub_url_match = URL_RE.search(msg.text or "")
        if sub_url_match:
            sub_url  = sub_url_match.group(0)
            ext      = Path(sub_url.split("?")[0]).suffix.lower()
            if ext not in SUBTITLE_EXTENSIONS:
                ext = ".srt"
            status   = await msg.reply("<i>Downloading subtitle…</i>")
            sub_path = os.path.join(TEMP_DIR, f"{str(uuid.uuid4())[:8]}_sub{ext}")
            try:
                import aiohttp, aiofiles
                async with aiohttp.ClientSession() as session:
                    async with session.get(sub_url, allow_redirects=True,
                                           timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status != 200:
                            await status.edit(f"❌ HTTP {resp.status}")
                            return
                        async with aiofiles.open(sub_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(256 * 1024):
                                await f.write(chunk)
                await status.edit("✅ <i>Subtitle downloaded — burning…</i>")
                STATE[uid]["state"] = "subtitle_ready"
                await _process_subtitle(client, msg, sub_path_override=sub_path)
            except Exception as e:
                await status.edit(f"❌ <b>Subtitle download failed</b>\n\n<code>{str(e)[:200]}</code>")
                cleanup(sub_path)
            return
        await msg.reply(
            "📎 <b>Send your subtitle file</b> or paste a direct URL:\n"
            "<code>https://example.com/subtitle.srt</code>"
        )
        return

    text = (msg.text or "").strip()

    # Magnet link
    if text.lower().startswith("magnet:"):
        STATE.pop(uid, None)  # clear any old session
        await _video_accepted(msg, "magnet", url=text, file_name="torrent")
        return

    match = URL_RE.search(text)
    if not match:
        await msg.reply(
            "👋 Send me a <b>video file</b>, a <b>URL</b>, or a <b>magnet link</b>.\n\n"
            "Use /help for instructions."
        )
        return

    url = match.group(0)
    STATE.pop(uid, None)  # always clear old session before accepting new link

    # Use detect_link_type so YouTube/yt-dlp links are routed correctly
    link_type = detect_link_type(url)

    if link_type == "blocked":
        await msg.reply(
            "🔒 <b>This link requires authentication</b>\n\n"
            f"<code>{url[:80]}</code>\n\n"
            "<i>This service requires login. Download the file first, then send it to the bot.</i>"
        )
        return
    elif link_type == "ytdlp":
        await _video_accepted(msg, "ytdlp", url=url, file_name="stream.mp4")
    elif link_type == "magnet":
        await _video_accepted(msg, "magnet", url=url, file_name="torrent")
    else:
        # direct link
        await _video_accepted(msg, "url", url=url,
                              file_name=Path(url.split("?")[0]).name or "video.mp4")


# ── Callback: Operation chosen ────────────────────────────────────
@app.on_callback_query(filters.regex(r"^op:(?!mediainfo|streams)"))
async def operation_chosen(client: Client, cb: CallbackQuery):
    uid  = cb.from_user.id
    op   = cb.data.split(":")[1]
    data = STATE.get(uid, {})

    if op == "cancel":
        STATE.pop(uid, None)
        await cb.message.edit("✕ <i>Cancelled.</i>")
        await cb.answer()
        return

    if op == "back":
        await cb.answer()
        if data:
            await cb.message.edit(
                "🎬 <b>Video ready</b>\n\nWhat do you want to do?",
                reply_markup=operation_keyboard(mode=data.get("mode", "upload"))
            )
        else:
            await cb.message.edit("✕ <i>Session expired.</i>")
        return

    if not data.get("source"):
        await cb.answer("⏰ Session expired — please send your video again.", show_alert=True)
        return

    if op == "leech":
        STATE.pop(uid, None)
        await cb.answer()
        progress_msg = await cb.message.edit("🔄 <i>Preparing…</i>")
        job_id   = str(uuid.uuid4())[:8]
        path     = None
        source   = data.get("source", "url")
        url      = data.get("url", "")
        username = cb.from_user.username or cb.from_user.first_name or str(uid)

        try:
            from processors.leech import direct_download, magnet_download, get_formats
            from handlers.leech import _upload_file, format_keyboard, YTDLP_STATE

            if source == "ytdlp":
                await progress_msg.edit("🔍 <i>Fetching qualities…</i>")
                formats, title = await get_formats(url)
                YTDLP_STATE[uid] = {"url": url, "formats": formats, "job_id": job_id}
                await progress_msg.edit(
                    f"📡 <b>{title[:40]}</b>\n\n<i>Choose quality:</i>",
                    reply_markup=format_keyboard(formats, job_id),
                )
            elif source == "magnet":
                register(job_id, uid, username, "magnet", url[:60])
                update_status(job_id, "🧲 Connecting…")
                path = await magnet_download(url, job_id, progress_msg=progress_msg)
                update_status(job_id, "📤 Uploading…")
                await _upload_file(client, cb.message, progress_msg, path)
            else:
                register(job_id, uid, username, "direct", url[:60])
                update_status(job_id, "🌐 Downloading…")
                path = await direct_download(url, job_id, progress_msg=progress_msg)
                update_status(job_id, "📤 Uploading…")
                await _upload_file(client, cb.message, progress_msg, path)

        except Exception as e:
            logger.error(f"Leech failed: {e}", exc_info=True)
            await progress_msg.edit(f"❌ <b>Download Failed</b>\n\n<code>{str(e)[:200]}</code>")
        finally:
            finish(job_id)
            if path:
                cleanup(path)
        return

    if op == "subtitles":
        STATE[uid]["state"] = "waiting_for_subtitle"
        await cb.message.edit(
            "🔤 <b>BURN SUBTITLES</b>\n\n"
            "Send your subtitle file or paste a direct URL.\n\n"
            "📎 <code>.srt</code> · <code>.ass</code> · <code>.ssa</code> · <code>.vtt</code> · <code>.sub</code>"
        )

    await cb.answer()


# ── Subtitle processing ───────────────────────────────────────────
async def _process_subtitle(client: Client, msg: Message, sub_path_override: str = None):
    uid  = msg.from_user.id
    data = STATE.pop(uid, {})

    ext = Path(sub_path_override).suffix.lower() if sub_path_override else \
          Path(msg.document.file_name or "").suffix.lower()

    if ext not in SUBTITLE_EXTENSIONS:
        await msg.reply(f"⚠️ <b>{ext or 'unknown'}</b> is not a supported subtitle format.")
        return

    progress_msg = await msg.reply("🔄 <i>Preparing…</i>")
    job_id       = str(uuid.uuid4())[:8]
    video_path   = output_path = None
    sub_path     = sub_path_override

    username = msg.from_user.username or msg.from_user.first_name or str(uid)
    desc     = data.get("file_name") or data.get("url", "unknown")[:50]
    register(job_id, uid, username, "burn", desc)

    try:
        update_status(job_id, "📥 Downloading video…")
        video_path = await _get_video(client, data, job_id, progress_msg)

        if not sub_path:
            sub_path = os.path.join(TEMP_DIR, f"{job_id}_sub{ext}")
            await client.download_media(msg.document.file_id, file_name=sub_path)

        t0 = time.monotonic()

        async def ffmpeg_progress(pct, speed, eta):
            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
            try:
                await progress_msg.edit(
                    f"🔥 <b>Burning subtitles…</b>\n\n"
                    f"<code>{bar}</code> <b>{pct}%</b>\n"
                    f"🚀 {speed}  ·  ⏱ ETA {eta}"
                )
            except Exception:
                pass

        update_status(job_id, "🔥 Burning subtitles…")
        output_path = await burn_subtitles(video_path, sub_path, ffmpeg_progress, uid=uid)
        elapsed     = time.monotonic() - t0

        update_status(job_id, "📤 Uploading…")
        out_name = output_filename(data.get("file_name") or "video.mp4", "subtitled")
        await _send_output(client, msg, progress_msg, output_path, out_name, elapsed)

    except Exception as e:
        logger.error(f"Subtitle burn failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ <b>Burn failed</b>\n\n<code>{str(e)[:200]}</code>")
    finally:
        finish(job_id)
        cleanup(video_path, sub_path, output_path)


# ── Download video from any source ───────────────────────────────
async def _get_video(client: Client, data: dict, job_id: str, progress_msg) -> str:
    source = data.get("source")

    if source == "upload":
        ext        = Path(data.get("file_name", "video.mp4")).suffix.lower() or ".mp4"
        video_path = os.path.join(TEMP_DIR, f"{job_id}_video{ext}")
        file_size  = data.get("file_size", 0)
        await progress_msg.edit("📥 <i>Downloading from Telegram…</i>\n<code>░░░░░░░░░░░░░░░░░░░░</code> 0%")
        await client.download_media(
            data["file_id"],
            file_name=video_path,
            progress=make_progress(progress_msg, "Downloading…", known_total=file_size),
        )
        return video_path

    elif source == "url":
        await progress_msg.edit("🌐 <i>Downloading from URL…</i>")
        from processors.leech import direct_download
        return await direct_download(data["url"], job_id, progress_msg=progress_msg)

    elif source == "ytdlp":
        await progress_msg.edit("📡 <i>Downloading HLS stream…</i>")
        return await ytdlp_download(data["url"], "bestvideo+bestaudio/best", job_id, progress_msg=progress_msg)

    elif source == "magnet":
        from processors.leech import magnet_download
        await progress_msg.edit("🧲 <i>Connecting to peers…</i>")
        return await magnet_download(data["url"], job_id, progress_msg=progress_msg)

    raise RuntimeError("Unknown video source.")


# ── Upload processed file ─────────────────────────────────────────
async def _send_output(client: Client, msg: Message, progress_msg,
                       output_path: str, out_name: str, elapsed: float):
    import subprocess, shutil as _shutil, time as _time

    out_size     = os.path.getsize(output_path)
    _ffmpeg_bin  = _shutil.which("ffmpeg")  or "ffmpeg"
    _ffprobe_bin = _shutil.which("ffprobe") or "ffprobe"

    # Get duration + dimensions
    duration = width = height = 0
    try:
        r = subprocess.run([
            _ffprobe_bin, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            output_path,
        ], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
        if len(lines) >= 3:
            width, height, duration = int(lines[0]), int(lines[1]), int(float(lines[2]))
        elif len(lines) == 2:
            width, height = int(lines[0]), int(lines[1])
    except Exception:
        pass

    # Generate thumbnail
    thumb_path = output_path.rsplit(".", 1)[0] + "_thumb.jpg"
    thumb_ok   = False
    try:
        for seek in ("00:00:05", "00:00:03"):
            subprocess.run([
                _ffmpeg_bin, "-y", "-ss", seek, "-i", output_path,
                "-vframes", "1", "-q:v", "2", thumb_path,
            ], capture_output=True, timeout=30)
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                thumb_ok = True
                break
    except Exception:
        pass

    upload_start = _time.time()
    last_up      = [0.0]

    async def upload_progress(current, total):
        now = _time.time()
        if now - last_up[0] < 2:
            return
        last_up[0] = now
        real_total = total if total else out_size
        elapsed_up = max(now - upload_start, 0.1)
        speed      = current / elapsed_up
        speed_str  = format_size(int(speed)) + "/s"
        if real_total > 0:
            pct     = min(int(current * 100 / real_total), 99)
            bar     = "█" * (pct // 5) + "░" * (20 - pct // 5)
            eta     = int((real_total - current) / speed) if speed > 0 else 0
            eta_str = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"
            text    = (
                f"📤 <b>Uploading…</b>\n\n"
                f"<code>{bar}</code> <b>{pct}%</b>\n"
                f"📦 {format_size(current)} / {format_size(real_total)}\n"
                f"🚀 {speed_str}  ·  ⏱ {eta_str}"
            )
        else:
            text = f"📤 <b>Uploading…</b>\n\n📦 {format_size(current)}\n🚀 {speed_str}"
        try:
            await progress_msg.edit(text)
        except Exception:
            pass

    await progress_msg.edit(
        f"📤 <i>Uploading…</i> <code>0%</code>\n<code>░░░░░░░░░░░░░░░░░░░░</code>\n📦 {format_size(out_size)}"
    )

    upload_type = user_setting(msg.chat.id, "upload_type")
    uploader    = client  # always use bot client — sends to user's chat, not Saved Messages
    thumb       = thumb_path if thumb_ok else None

    if upload_type == "document":
        sent = await uploader.send_document(
            chat_id=msg.chat.id, document=output_path,
            thumb=thumb, caption="✅ Done", file_name=out_name,
            progress=upload_progress,
        )
    else:
        sent = await uploader.send_video(
            chat_id=msg.chat.id, video=output_path,
            thumb=thumb, duration=duration,
            width=width or None, height=height or None,
            caption="✅ Done", file_name=out_name,
            supports_streaming=True, progress=upload_progress,
        )

    await progress_msg.delete()
    if thumb_ok and os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
        except Exception:
            pass

    # Channel forwarding
    from utils.settings import get as _get_setting
    _channels = _get_setting(msg.chat.id, "channel_ids")
    if _channels:
        if user_setting(msg.chat.id, "auto_forward"):
            for _ch in _channels:
                try:
                    await client.copy_message(
                        chat_id=_ch,
                        from_chat_id=sent.chat.id,
                        message_id=sent.id,
                    )
                except Exception as e:
                    logger.warning(f"Auto-forward to {_ch} failed: {e}")
        else:
            FORWARD_PENDING[sent.id] = {
                "chat_id":    sent.chat.id,
                "message_id": sent.id,
                "channel_ids": _channels,
            }
            ch_list = "\n".join(f"  • <code>{c}</code>" for c in _channels)
            await client.send_message(
                chat_id=msg.chat.id,
                text=(
                    "📢 <b>Forward to channels?</b>\n\n"
                    f"{ch_list}\n\n"
                    "<i>Would you like to forward this file?</i>"
                ),
                reply_markup=_forward_keyboard(sent.id),
            )


# ── Forward to channel ────────────────────────────────────────────
FORWARD_PENDING: dict[int, dict] = {}


def _forward_keyboard(sent_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, forward", callback_data=f"fwd:yes:{sent_msg_id}"),
        InlineKeyboardButton("✕ No thanks",    callback_data=f"fwd:no:{sent_msg_id}"),
    ]])


@app.on_callback_query(filters.regex(r"^fwd:"))
async def forward_callback(client: Client, cb: CallbackQuery):
    parts   = cb.data.split(":")
    action  = parts[1]
    key     = int(parts[2])
    pending = FORWARD_PENDING.pop(key, None)

    if not pending:
        await cb.answer("⚠️ Already handled or expired.", show_alert=True)
        return

    if action == "no":
        await cb.message.edit("<i>Not forwarded.</i>")
        await cb.answer()
        return

    from utils.settings import get as _get_ch
    channel_ids = pending.get("channel_ids") or _get_ch(cb.from_user.id, "channel_ids")
    if not channel_ids:
        await cb.answer("⚠️ No channels configured.", show_alert=True)
        await cb.message.delete()
        return

    errors = []
    for ch in channel_ids:
        try:
            await client.copy_message(
                chat_id=ch,
                from_chat_id=pending["chat_id"],
                message_id=pending["message_id"],
            )
        except Exception as e:
            errors.append(f"{ch}: {str(e)[:60]}")

    if errors:
        await cb.message.edit(
            f"⚠️ <i>Forwarded with errors:</i>\n\n" +
            "\n".join(f"<code>{e}</code>" for e in errors)
        )
        await cb.answer("⚠️ Some forwards failed", show_alert=True)
    else:
        await cb.message.edit(f"📢 ✅ <i>Forwarded to {len(channel_ids)} channel(s).</i>")
        await cb.answer("✅ Forwarded!")
