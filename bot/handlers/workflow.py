"""
Main workflow — Pyrogram
────────────────────────
State is tracked in a simple in-memory dict keyed by user_id.
Pyrogram doesn't have built-in FSM so we implement a lightweight one.

States:
  "choosing_operation"    — video received, waiting for button press
  "waiting_for_subtitle"  — waiting for .srt/.ass/… file
  "choosing_resolution"   — waiting for resolution button press
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
from config import (
    TEMP_DIR,
    VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS,
    RESOLUTIONS, MAX_FILE_SIZE_BYTES,
)
from utils.file_utils import format_size, output_filename, cleanup
from utils.queue import register, update_status, set_task, finish
from processors.ffmpeg import burn_subtitles, change_resolution, download_url
from processors.leech import ytdlp_download, detect_link_type

logger = logging.getLogger(__name__)

# ── In-memory state store ─────────────────────────────────────────
# { user_id: { "state": str, "source": str, "file_id": str, ... } }
STATE: dict[int, dict] = {}

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


# ── Keyboards ─────────────────────────────────────────────────────
def operation_keyboard(mode: str = "upload"):
    """
    mode="upload"  → Burn Subtitles + Change Resolution
    mode="direct"  → Leech + Burn Subtitles + Change Resolution
    mode="m3u8"    → Leech (res picker) + Burn Subtitles
    mode="magnet"  → Leech (download only) + Burn Subtitles
    """
    if mode == "upload":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔤 Burn Subtitles",    callback_data="op:subtitles"),
                InlineKeyboardButton("📐 Change Resolution", callback_data="op:resolution"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="op:cancel")],
        ])
    elif mode == "direct":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Leech (download only)", callback_data="op:leech")],
            [
                InlineKeyboardButton("🔤 Burn Subtitles",    callback_data="op:subtitles"),
                InlineKeyboardButton("📐 Change Resolution", callback_data="op:resolution"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="op:cancel")],
        ])
    elif mode in ("m3u8", "magnet"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Leech (download only)", callback_data="op:leech")],
            [InlineKeyboardButton("🔤 Burn Subtitles",        callback_data="op:subtitles")],
            [InlineKeyboardButton("❌ Cancel",                callback_data="op:cancel")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="op:cancel")],
    ])


def resolution_keyboard():
    rows = []
    items = list(RESOLUTIONS.items())
    for i in range(0, len(items), 2):
        row = []
        for key, (label, _) in items[i:i+2]:
            row.append(InlineKeyboardButton(label.strip(), callback_data=f"res:{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="op:cancel")])
    return InlineKeyboardMarkup(rows)


# ── Progress callback for Pyrogram downloads/uploads ──────────────
def make_progress(msg, action: str, known_total: int = 0):
    """
    Returns a Pyrogram progress callback.
    Updates every 3 seconds to stay within Telegram flood limits.
    known_total: pass the file size so progress works even when
                 Pyrogram reports total=0 during download.
    """
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
            filled  = pct // 5
            bar     = "█" * filled + "░" * (20 - filled)
            remain  = real_total - current
            eta     = int(remain / speed) if speed > 0 else 0
            eta_str = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"
            text = (
                f"**{action}**\n\n"
                f"`{bar}`\n"
                f"**{pct}%** — {format_size(current)} / {format_size(real_total)}\n"
                f"🚀 {speed_str} · ⏱ ETA {eta_str}"
            )
        else:
            text = (
                f"**{action}**\n\n"
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
    STATE[uid] = {
        "state":     "choosing_operation",
        "source":    source,
        "file_id":   file_id,
        "file_name": file_name,
        "file_size": file_size,
        "url":       url,
    }

    if source == "upload":
        desc = f"📁 `{file_name}`  ·  {format_size(file_size)}"
        mode = "upload"
    elif source == "ytdlp":
        short = url[:60] + "…" if len(url) > 60 else url
        desc  = f"📡 **HLS Stream detected**\n`{short}`"
        mode  = "m3u8"
    elif source == "magnet":
        short = url[:60] + "…" if len(url) > 60 else url
        desc  = f"🧲 **Magnet link**\n`{short}`"
        mode  = "magnet"
    else:
        short = url[:60] + "…" if len(url) > 60 else url
        desc  = f"🔗 `{short}`"
        mode  = "direct"

    await msg.reply(
        f"🎬 **Video ready**\n\n{desc}\n\nWhat do you want to do?",
        reply_markup=operation_keyboard(mode=mode),
    )


# ── Input: Video file upload ──────────────────────────────────────
@app.on_message(filters.private & (filters.video | filters.document))
async def recv_file(client: Client, msg: Message):
    uid  = msg.from_user.id
    data = STATE.get(uid, {})

    # Route subtitle files if waiting for one
    if msg.document:
        ext = Path(msg.document.file_name or "").suffix.lower()
        if data.get("state") == "waiting_for_subtitle" and ext in SUBTITLE_EXTENSIONS:
            await _process_subtitle(client, msg)
            return
        media     = msg.document
        file_name = media.file_name or "video.mp4"
        file_size = media.file_size or 0
        ext       = Path(file_name).suffix.lower()
        if ext == ".torrent":
            # .torrent file — download via libtorrent
            import os, uuid as _uuid
            from processors.leech import magnet_download
            from handlers.leech import _upload_file
            from utils.queue import register as _register, update_status as _update, finish as _finish

            job_id   = str(_uuid.uuid4())[:8]
            username = msg.from_user.username or msg.from_user.first_name or str(uid)
            torrent_name = file_name
            _register(job_id, uid, username, "magnet", torrent_name)

            status = await msg.reply(
                f"🌱 **Torrent file detected!**\n\n"
                f"📄 `{torrent_name}`\n\n"
                f"_Downloading torrent file…_"
            )

            # Download the .torrent file first
            torrent_path = os.path.join(TEMP_DIR, f"{job_id}.torrent")
            await client.download_media(media.file_id, file_name=torrent_path)

            path = None
            try:
                _update(job_id, "🧲 Connecting to peers…")
                path = await magnet_download(torrent_path, job_id, progress_msg=status)
                _update(job_id, "📤 Uploading…")
                await _upload_file(client, msg, status, path)
            except Exception as e:
                logger.error(f"Torrent download failed: {e}", exc_info=True)
                await status.edit(f"❌ **Download failed**\n\n`{str(e)[:300]}`")
            finally:
                _finish(job_id)
                cleanup(torrent_path, path)
            return

        if ext not in VIDEO_EXTENSIONS:
            if ext in SUBTITLE_EXTENSIONS:
                await msg.reply("📝 Send your **video** first — then I'll ask for the subtitle.")
            else:
                await msg.reply(
                    f"⚠️ Unrecognised file (`{ext or 'unknown'}`)\n\n"
                    "Send a **video file**, a **direct URL**, a **magnet link**, or a **.torrent file**."
                )
            return
    else:
        media     = msg.video
        file_name = media.file_name or "video.mp4"
        file_size = media.file_size or 0

    if file_size > MAX_FILE_SIZE_BYTES:
        await msg.reply(
            f"❌ File too large — max is **2 GB**.\n"
            f"Your file: **{format_size(file_size)}**"
        )
        return

    await _video_accepted(msg, "upload",
                          file_id=media.file_id,
                          file_name=file_name,
                          file_size=file_size)


# ── Input: URL ────────────────────────────────────────────────────
@app.on_message(filters.private & filters.text & ~filters.command(["start", "help"]))
async def recv_text(client: Client, msg: Message):
    uid  = msg.from_user.id
    data = STATE.get(uid, {})

    if data.get("state") == "waiting_for_subtitle":
        # Check if user sent a URL to download the subtitle from
        sub_url_match = URL_RE.search(msg.text or "")
        if sub_url_match:
            sub_url = sub_url_match.group(0)
            ext     = Path(sub_url.split("?")[0]).suffix.lower()

            # If no recognisable extension, assume .srt
            if ext not in SUBTITLE_EXTENSIONS:
                ext = ".srt"

            status = await msg.reply("_Downloading subtitle…_")
            sub_job = str(uuid.uuid4())[:8]
            sub_path = os.path.join(TEMP_DIR, f"{sub_job}_sub{ext}")

            try:
                import aiohttp, aiofiles
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        sub_url, allow_redirects=True,
                        timeout=aiohttp.ClientTimeout(total=60)
                    ) as resp:
                        if resp.status != 200:
                            await status.edit(f"❌ Could not download subtitle — HTTP `{resp.status}`")
                            return
                        async with aiofiles.open(sub_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(256 * 1024):
                                await f.write(chunk)

                await status.edit("_Subtitle downloaded — processing…_")

                # Inject downloaded subtitle path into state and trigger burn
                STATE[uid]["sub_path"]  = sub_path
                STATE[uid]["sub_ext"]   = ext
                STATE[uid]["state"]     = "subtitle_ready"
                await _process_subtitle(client, msg, sub_path_override=sub_path)

            except Exception as e:
                logger.error(f"Subtitle URL download failed: {e}", exc_info=True)
                await status.edit(f"❌ **Failed to download subtitle**\n\n`{str(e)[:200]}`")
                cleanup(sub_path)
            return

        await msg.reply(
            "📎 _Send your subtitle as a file or paste a direct URL._\n\n"
            "Example: `https://example.com/subtitle.srt`"
        )
        return

    text = msg.text or ""

    # Check for magnet link first (doesn't start with http)
    if text.strip().lower().startswith("magnet:"):
        await _video_accepted(msg, "magnet", url=text.strip(), file_name="torrent")
        return

    match = URL_RE.search(text)
    if not match:
        await msg.reply(
            "👋 Send me a video file, a direct URL, or a magnet link to get started.\n\n"
            "Examples:\n"
            "• `https://example.com/video.mp4`\n"
            "• `https://youtube.com/watch?v=...`\n"
            "• `magnet:?xt=urn:btih:...`\n\n"
            "Use /help for instructions."
        )
        return

    url = match.group(0)

    if ".m3u8" in url.lower():
        await _video_accepted(msg, "ytdlp", url=url, file_name="stream.mp4")
    elif url.lower().startswith("magnet:"):
        await _video_accepted(msg, "magnet", url=url, file_name="torrent")
    else:
        await _video_accepted(msg, "url",
                              url=url,
                              file_name=Path(url.split("?")[0]).name or "video.mp4")


# ── Callback: Operation chosen ────────────────────────────────────
@app.on_callback_query(filters.regex(r"^op:"))
async def operation_chosen(client: Client, cb: CallbackQuery):
    uid  = cb.from_user.id
    op   = cb.data.split(":")[1]
    data = STATE.get(uid, {})

    if op == "cancel":
        STATE.pop(uid, None)
        await cb.message.edit("_Cancelled._")
        await cb.answer()
        return

    if not data.get("source"):
        await cb.answer("⚠️ Session expired — send your video again.", show_alert=True)
        return

    if op == "leech":
        STATE.pop(uid, None)
        await cb.answer()
        progress_msg = await cb.message.edit("_Starting…_")
        job_id   = str(uuid.uuid4())[:8]
        path     = None
        source   = data.get("source", "url")
        url      = data.get("url", "")
        username = cb.from_user.username or cb.from_user.first_name or str(uid)

        try:
            from processors.leech import ytdlp_download, direct_download, magnet_download, get_formats
            from handlers.leech import _upload_file, format_keyboard, YTDLP_STATE

            if source == "ytdlp":
                # m3u8 — show all available resolutions first (no job registered yet)
                await progress_msg.edit("_Fetching available qualities…_")
                formats, title = await get_formats(url)
                YTDLP_STATE[uid] = {"url": url, "formats": formats, "job_id": job_id}
                await progress_msg.edit(
                    f"📡 **{title}**\n\n📐 Choose download quality:",
                    reply_markup=format_keyboard(formats, job_id),
                )

            elif source == "magnet":
                register(job_id, uid, username, "magnet", url[:60])
                update_status(job_id, "🧲 Connecting to peers…")
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
            await progress_msg.edit(f"❌ **Download Failed**\n\n`{str(e)[:200]}`")
        finally:
            finish(job_id)
            if path:
                cleanup(path)
        return

    if op == "subtitles":
        STATE[uid]["state"] = "waiting_for_subtitle"
        await cb.message.edit(
            "🔤✨ **BURN SUBTITLES** ✨🔤\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "Send me your subtitle file **or** paste a direct URL to download it.\n\n"
            "📎 **File:** `.srt` · `.ass` · `.ssa` · `.vtt` · `.sub` · `.txt`\n"
            "🔗 **URL:** `https://example.com/subtitle.srt`"
        )
    elif op == "resolution":
        STATE[uid]["state"] = "choosing_resolution"
        await cb.message.edit(
            "📐✨ **CHANGE RESOLUTION** ✨📐\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "_Choose the target resolution:_",
            reply_markup=resolution_keyboard(),
        )

    await cb.answer()


# ── Subtitle processing ───────────────────────────────────────────
async def _process_subtitle(client: Client, msg: Message, sub_path_override: str = None):
    uid  = msg.from_user.id
    data = STATE.pop(uid, {})

    # Determine subtitle ext — from override path or from attached file
    if sub_path_override:
        ext = Path(sub_path_override).suffix.lower()
    else:
        ext = Path(msg.document.file_name or "").suffix.lower()

    if ext not in SUBTITLE_EXTENSIONS:
        await msg.reply(
            f"⚠️ **{ext or 'unknown'}** is not a supported subtitle format.\n"
            f"Accepted: {', '.join(sorted(SUBTITLE_EXTENSIONS))}"
        )
        return

    progress_msg = await msg.reply("⏳ **Starting…**")
    job_id       = str(uuid.uuid4())[:8]
    video_path   = output_path = None
    sub_path     = sub_path_override  # may be None if file attachment

    username = msg.from_user.username or msg.from_user.first_name or str(uid)
    desc     = data.get("file_name") or data.get("url", "unknown")[:50]
    register(job_id, uid, username, "burn", desc)

    try:
        update_status(job_id, "📥 Downloading video…")
        video_path = await _get_video(client, data, job_id, progress_msg)

        # Download subtitle from Telegram only if not already downloaded via URL
        if not sub_path:
            sub_path = os.path.join(TEMP_DIR, f"{job_id}_sub{ext}")
            await client.download_media(
                msg.document.file_id,
                file_name=sub_path,
            )

        # Burn with live FFmpeg progress
        t0 = time.monotonic()

        async def ffmpeg_progress_sub(pct, speed, eta):
            filled = pct // 5
            bar    = "█" * filled + "░" * (20 - filled)
            try:
                await progress_msg.edit(
                    f"🔥 **Burning subtitles…**\n\n"
                    f"`{bar}`\n"
                    f"**{pct}%** · 🚀 {speed} · ⏱ ETA {eta}"
                )
            except Exception:
                pass

        update_status(job_id, "🔥 Burning subtitles…")
        output_path = await burn_subtitles(video_path, sub_path, ffmpeg_progress_sub)
        elapsed     = time.monotonic() - t0

        update_status(job_id, "📤 Uploading…")
        out_name = output_filename(data.get("file_name") or "video.mp4", "subtitled")
        await _send_output(client, msg, progress_msg, output_path, out_name, elapsed)

    except Exception as e:
        logger.error(f"Subtitle burn failed: {e}", exc_info=True)
        await progress_msg.edit(
            f"❌ **Failed to burn subtitles**\n\n`{str(e)[:300]}`"
        )
    finally:
        finish(job_id)
        cleanup(video_path, sub_path, output_path)


# ── Callback: Resolution chosen ───────────────────────────────────
@app.on_callback_query(filters.regex(r"^res:"))
async def resolution_chosen(client: Client, cb: CallbackQuery):
    uid     = cb.from_user.id
    res_key = cb.data.split(":")[1]
    data    = STATE.pop(uid, {})

    if res_key not in RESOLUTIONS or not data.get("source"):
        await cb.answer("⚠️ Session expired — send your video again.", show_alert=True)
        return

    _, scale     = RESOLUTIONS[res_key]
    progress_msg = await cb.message.edit(f"⏳ **Starting conversion to {res_key}…**")
    await cb.answer()

    job_id     = str(uuid.uuid4())[:8]
    video_path = output_path = None

    username = cb.from_user.username or cb.from_user.first_name or str(uid)
    desc     = data.get("file_name") or data.get("url", "unknown")[:50]
    register(job_id, uid, username, "resolution", f"{desc} → {res_key}")

    try:
        update_status(job_id, "📥 Downloading video…")
        video_path = await _get_video(client, data, job_id, progress_msg)

        t0 = time.monotonic()

        async def ffmpeg_progress_res(pct, speed, eta):
            filled = pct // 5
            bar    = "█" * filled + "░" * (20 - filled)
            try:
                await progress_msg.edit(
                    f"⚙️ **Converting to {res_key}…**\n\n"
                    f"`{bar}`\n"
                    f"**{pct}%** · 🚀 {speed} · ⏱ ETA {eta}"
                )
            except Exception:
                pass

        update_status(job_id, f"⚙️ Converting to {res_key}…")
        output_path = await change_resolution(video_path, scale, ffmpeg_progress_res)
        elapsed     = time.monotonic() - t0

        update_status(job_id, "📤 Uploading…")
        out_name = output_filename(data.get("file_name") or "video.mp4", res_key)
        await _send_output(client, cb.message, progress_msg, output_path, out_name, elapsed)

    except Exception as e:
        logger.error(f"Resolution change failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ **Conversion failed**\n\n`{str(e)[:300]}`")
    finally:
        finish(job_id)
        cleanup(video_path, output_path)


# ── Shared helpers ────────────────────────────────────────────────
async def _get_video(client: Client, data: dict, job_id: str, progress_msg) -> str:
    """Download video from Telegram or from a URL."""
    source = data.get("source")

    if source == "upload":
        ext        = Path(data.get("file_name", "video.mp4")).suffix.lower() or ".mp4"
        video_path = os.path.join(TEMP_DIR, f"{job_id}_video{ext}")
        file_size = data.get("file_size", 0)
        await progress_msg.edit("⏳ **Downloading from Telegram…**\n`░░░░░░░░░░░░░░░░░░░░` 0%")
        await client.download_media(
            data["file_id"],
            file_name=video_path,
            progress=make_progress(progress_msg, "Downloading…", known_total=file_size),
        )
        return video_path

    elif source == "url":
        await progress_msg.edit("⏳ **Downloading from URL…**\n_Large files may take a while._")
        return await download_url(data["url"], job_id, progress_msg=progress_msg)

    elif source == "ytdlp":
        # m3u8 — download best quality for processing
        await progress_msg.edit(
            "📡 **Downloading HLS stream…**\n"
            "_Fetching and merging all segments via yt-dlp…_"
        )
        return await ytdlp_download(
            data["url"],
            "bestvideo+bestaudio/best",
            job_id,
            progress_msg=progress_msg,
        )

    elif source == "magnet":
        # Magnet — download via libtorrent before processing
        from processors.leech import magnet_download
        await progress_msg.edit(
            "🧲 **Downloading torrent…**\n"
            "_Connecting to peers…_"
        )
        return await magnet_download(data["url"], job_id, progress_msg=progress_msg)

    raise RuntimeError("Unknown video source in session state.")


async def _send_output(client: Client, msg: Message, progress_msg,
                       output_path: str, out_name: str, elapsed: float):
    """Upload the processed file back to the user with thumbnail and duration."""
    import subprocess, shutil as _shutil

    out_size     = os.path.getsize(output_path)
    _ffmpeg_bin  = _shutil.which("ffmpeg")  or r"C:\ffmpeg\bin\ffmpeg.exe"
    _ffprobe_bin = _shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"

    # ── Get duration + dimensions via ffprobe ─────────────────────
    duration = width = height = 0
    try:
        result = subprocess.run([
            _ffprobe_bin, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            output_path,
        ], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        # lines order: width, height, duration
        if len(lines) >= 3:
            width    = int(lines[0])
            height   = int(lines[1])
            duration = int(float(lines[2]))
        elif len(lines) == 2:
            width  = int(lines[0])
            height = int(lines[1])
    except Exception:
        pass

    # ── Generate thumbnail at actual video resolution ─────────────
    thumb_path = output_path.replace(".mp4", "_thumb.jpg")
    try:
        # Seek to 5s (or 3s fallback) for a good representative frame
        subprocess.run([
            _ffmpeg_bin, "-y",
            "-ss", "00:00:05",
            "-i", output_path,
            "-vframes", "1",
            "-vf", f"scale={width}:{height}" if width and height else "scale=iw:ih",
            "-q:v", "2",
            thumb_path,
        ], capture_output=True, timeout=30)
        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            # Fallback to 3s if 5s seek failed (short video)
            subprocess.run([
                _ffmpeg_bin, "-y",
                "-ss", "00:00:03",
                "-i", output_path,
                "-vframes", "1",
                "-vf", f"scale={width}:{height}" if width and height else "scale=iw:ih",
                "-q:v", "2",
                thumb_path,
            ], capture_output=True, timeout=30)
    except Exception:
        thumb_path = None

    import time as _time
    upload_start = _time.time()
    last_up      = [0.0]

    async def upload_progress(current, total):
        now = _time.time()
        if now - last_up[0] < 2:
            return
        last_up[0] = now
        real_total = total if total else out_size
        elapsed    = max(now - upload_start, 0.1)
        speed      = current / elapsed
        speed_str  = format_size(int(speed)) + "/s"
        if real_total > 0:
            pct     = min(int(current * 100 / real_total), 99)
            filled  = pct // 5
            bar     = "█" * filled + "░" * (20 - filled)
            remain  = real_total - current
            eta     = int(remain / speed) if speed > 0 else 0
            eta_str = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"
            text    = (
                f"📤 **Uploading…**\n\n"
                f"`{bar}`\n"
                f"**{pct}%** — {format_size(current)} / {format_size(real_total)}\n"
                f"🚀 {speed_str} · ⏱ ETA {eta_str}"
            )
        else:
            text = f"📤 **Uploading…**\n\n📦 {format_size(current)}\n🚀 {speed_str}"
        try:
            await progress_msg.edit(text)
        except Exception:
            pass

    await progress_msg.edit(
        f"_Uploading…_ `0%`\n📦 {format_size(out_size)}"
    )

    # Always use bot client to send — user account doesn't have access to user chats
    sent = await client.send_video(
        chat_id=msg.chat.id,
        video=output_path,
        thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
        duration=duration,
        width=width   if width  else None,
        height=height if height else None,
        caption="✅ Done",
        file_name=out_name,
        supports_streaming=True,
        progress=upload_progress,
    )

    await progress_msg.delete()

    # Cleanup thumbnail
    if thumb_path and os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
        except Exception:
            pass

    # ── Ask to forward to channel ─────────────────────────────────
    import os as _os
    if _os.getenv("FORWARD_CHANNEL_ID", "").strip():
        FORWARD_PENDING[sent.id] = {
            "chat_id":    sent.chat.id,
            "message_id": sent.id,
        }
        await client.send_message(
            chat_id=msg.chat.id,
            text=(
                "📢✨ **FORWARD TO CHANNEL?** ✨📢\n"
                "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
                "_Would you like to send this file to your channel?_"
            ),
            reply_markup=_forward_keyboard(sent.id),
        )


# ── Forward to channel ────────────────────────────────────────────
# Stores pending forward: { msg_id: { "chat_id": ..., "message_id": ... } }
FORWARD_PENDING: dict[int, dict] = {}


def _forward_keyboard(sent_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, forward",  callback_data=f"fwd:yes:{sent_msg_id}"),
            InlineKeyboardButton("✕  No thanks",     callback_data=f"fwd:no:{sent_msg_id}"),
        ]
    ])


@app.on_callback_query(filters.regex(r"^fwd:"))
async def forward_callback(client: Client, cb: CallbackQuery):
    import os
    parts  = cb.data.split(":")
    action = parts[1]
    key    = int(parts[2])

    channel_id = os.getenv("FORWARD_CHANNEL_ID", "").strip()

    pending = FORWARD_PENDING.pop(key, None)
    if not pending:
        await cb.answer("⚠️ Already handled or expired.", show_alert=True)
        return

    if action == "no":
        await cb.message.edit("_Got it — not forwarded._")
        await cb.answer()
        return

    if not channel_id:
        await cb.answer("⚠️ No channel configured. Set FORWARD_CHANNEL_ID in env.", show_alert=True)
        await cb.message.delete()
        return

    try:
        # Copy without "Forwarded from" header
        await client.copy_message(
            chat_id=channel_id,
            from_chat_id=pending["chat_id"],
            message_id=pending["message_id"],
        )
        await cb.message.edit("✅ _Forwarded to channel._")
        await cb.answer("✅ Forwarded!")
    except Exception as e:
        await cb.message.edit(f"❌ _Forward failed_\n\n`{str(e)[:200]}`")
        await cb.answer("❌ Failed", show_alert=True)
