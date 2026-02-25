"""
Leech handler
─────────────
Intercepts URLs and magnet links sent to the bot.
Detects type → routes to appropriate downloader.

Flow:
  User sends link
      ↓
  detect_link_type()
      ├─ "ytdlp"   → fetch formats → show resolution keyboard → download → upload
      ├─ "direct"  → download with progress → upload
      └─ "magnet"  → torrent download with progress → upload
"""

import os
import uuid
from utils.settings import get as user_setting
import logging
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from client import app
from config import TEMP_DIR
from utils.file_utils import format_size, cleanup
from utils.queue import register, update_status, set_task, finish
from processors.leech import (
    detect_link_type, get_formats,
    ytdlp_download, direct_download, magnet_download,
)

logger = logging.getLogger(__name__)

# ── In-memory store for pending yt-dlp jobs ────────────────────────
# { user_id: { "url": str, "formats": list, "job_id": str } }
YTDLP_STATE: dict[int, dict] = {}


# ── Resolution keyboard for yt-dlp ────────────────────────────────
def format_keyboard(formats: list[dict], job_id: str) -> InlineKeyboardMarkup:
    rows = []
    # Max 2 per row, skip "Best quality" into its own row
    best = formats[0]
    rows.append([InlineKeyboardButton(
        f"⭐ {best['label']}", callback_data=f"leech:0:{job_id}"
    )])
    rest = formats[1:]
    for i in range(0, len(rest), 2):
        row = []
        for j, fmt in enumerate(rest[i:i+2]):
            idx = i + j + 1
            row.append(InlineKeyboardButton(
                fmt["label"], callback_data=f"leech:{idx}:{job_id}"
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("✕  Cancel", callback_data="leech:cancel")])
    return InlineKeyboardMarkup(rows)


# ── Main link receiver ─────────────────────────────────────────────
@app.on_message(filters.private & filters.regex(r"(https?://|magnet:\?)"))
async def recv_link(client: Client, msg: Message):
    """Catch any message containing a URL or magnet link."""
    text = msg.text or msg.caption or ""
    url  = text.strip().split()[0]   # take first token

    uid    = msg.from_user.id
    job_id = str(uuid.uuid4())[:8]

    # ── Route by type ──────────────────────────────────────────────
    link_type = detect_link_type(url)

    username = msg.from_user.username or msg.from_user.first_name or str(uid)

    if link_type == "ytdlp":
        status = await msg.reply("🔍 _Fetching available qualities…_")
        try:
            formats, title = await get_formats(url)
            YTDLP_STATE[uid] = {"url": url, "formats": formats, "job_id": job_id}
            await status.edit(
                f"🎬✨ **{title[:35]}** ✨🎬\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
                f"_Choose download quality:_",
                reply_markup=format_keyboard(formats, job_id),
            )
        except Exception as e:
            logger.error(f"get_formats failed: {e}", exc_info=True)
            await status.edit(
                f"❌ **Could not fetch video info**\n\n"
                f"`{str(e)[:200]}`\n\n"
                f"> _Try sending the direct video URL instead_"
            )

    elif link_type == "direct":
        status = await msg.reply("📥 _Starting download…_")
        register(job_id, uid, username, "direct", url[:60])
        update_status(job_id, "🌐 Downloading…")
        await _run_direct(client, msg, status, url, job_id)

    elif link_type == "magnet":
        status = await msg.reply(
            "🧲 _Connecting to peers…_"
        )
        register(job_id, uid, username, "magnet", url[:60])
        update_status(job_id, "🧲 Connecting to peers…")
        await _run_magnet(client, msg, status, url, job_id)


# ── yt-dlp resolution chosen ──────────────────────────────────────
@app.on_callback_query(filters.regex(r"^leech:"))
async def leech_callback(client: Client, cb: CallbackQuery):
    parts = cb.data.split(":")

    if parts[1] == "cancel":
        uid = cb.from_user.id
        YTDLP_STATE.pop(uid, None)
        await cb.message.edit("✕ _Cancelled._")
        await cb.answer()
        return

    idx    = int(parts[1])
    job_id = parts[2]
    uid    = cb.from_user.id
    data   = YTDLP_STATE.pop(uid, None)

    if not data or data["job_id"] != job_id:
        await cb.answer("⏰ Session expired — please send the link again.", show_alert=True)
        return

    await cb.answer()
    fmt       = data["formats"][idx]
    url       = data["url"]
    format_id = fmt["format_id"]
    label     = fmt["label"]

    progress_msg = await cb.message.edit(
        f"📥 **Downloading** `{label}`\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n`░░░░░░░░░░░░░░░░░░░░` 0%"
    )

    username = cb.from_user.username or cb.from_user.first_name or str(uid)
    register(job_id, uid, username, "ytdlp", f"{label} — {url[:40]}")
    update_status(job_id, f"📥 Downloading {label}…")

    path = None
    try:
        path = await ytdlp_download(url, format_id, job_id, progress_msg=progress_msg)
        update_status(job_id, "📤 Uploading…")
        await _upload_file(client, cb.message, progress_msg, path)
    except Exception as e:
        logger.error(f"yt-dlp download failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ **Download failed**\n\n`{str(e)[:200]}`")
    finally:
        finish(job_id)
        cleanup(path)


# ── Direct download runner ─────────────────────────────────────────
async def _run_direct(client, msg, status, url, job_id):
    path = None
    try:
        path = await direct_download(url, job_id, progress_msg=status)
        await _upload_file(client, msg, status, path)
    except Exception as e:
        logger.error(f"Direct download failed: {e}", exc_info=True)
        await status.edit(f"❌ **Download failed**\n\n`{str(e)[:200]}`")
    finally:
        finish(job_id)
        cleanup(path)


# ── Magnet download runner ─────────────────────────────────────────
async def _run_magnet(client, msg, status, magnet, job_id):
    path = None
    try:
        path = await magnet_download(magnet, job_id, progress_msg=status)
        await _upload_file(client, msg, status, path)
    except Exception as e:
        logger.error(f"Magnet download failed: {e}", exc_info=True)
        await status.edit(f"❌ **Download failed**\n\n`{str(e)[:200]}`")
    finally:
        finish(job_id)
        cleanup(path)


# ── Upload to Telegram ─────────────────────────────────────────────
async def _upload_file(client: Client, msg: Message, progress_msg, file_path: str):
    """
    Upload the downloaded file back to the user.
    Sends as video if it's a video file, otherwise as document.
    """
    import time
    from utils.file_utils import format_size
    from processors.leech import _safe_edit

    size      = os.path.getsize(file_path)
    file_name = Path(file_path).name
    ext       = Path(file_path).suffix.lower()

    VIDEO_EXTS = {".mp4", ".m4v"}  # only these render nicely as Telegram videos

    await progress_msg.edit(
        f"📤 _Uploading…_ `0%`\n`░░░░░░░░░░░░░░░░░░░░`\n📦 {format_size(size)}"
    )

    import time as _time
    last_up  = [0.0]
    start_up = [_time.time()]

    async def upload_progress(current, total):
        now = _time.time()
        if now - last_up[0] < 3:
            return
        last_up[0] = now
        real_total = total if total else size
        elapsed    = max(now - start_up[0], 0.1)
        speed      = current / elapsed
        speed_str  = f"{format_size(int(speed))}/s"
        if real_total > 0:
            pct     = min(int(current * 100 / real_total), 99)
            filled  = pct // 5
            bar     = "█" * filled + "░" * (20 - filled)
            remain  = real_total - current
            eta     = int(remain / speed) if speed > 0 else 0
            eta_str = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"
            text    = (
                f"📤 _Uploading…_ **{pct}%**\n"
                f"`{bar}`\n"
                f"📦 {format_size(current)} / {format_size(real_total)}\n"
                f"🚀 {speed_str}  ·  ⏱ {eta_str}"
            )
        else:
            text = f"📤 _Uploading…_\n📦 {format_size(current)}  ·  🚀 {speed_str}"
        try:
            await progress_msg.edit(text)
        except Exception:
            pass

    caption = f"✅ Done"

    upload_type = user_setting(msg.chat.id, "upload_type")

    if ext in VIDEO_EXTS and upload_type == "video":
        thumb = await _make_thumb(file_path)
        duration = await _get_duration(file_path)
        width, height = await _get_dimensions(file_path)
        sent = await client.send_video(
            chat_id=msg.chat.id,
            video=file_path,
            thumb=thumb,
            duration=duration,
            width=width   if width  else None,
            height=height if height else None,
            caption=caption,
            file_name=file_name,
            supports_streaming=True,
            progress=upload_progress,
        )
        if thumb and os.path.exists(thumb):
            os.remove(thumb)
    else:
        sent = await client.send_document(
            chat_id=msg.chat.id,
            document=file_path,
            caption=caption,
            file_name=file_name,
            progress=upload_progress,
        )

    await progress_msg.delete()

    # ── Ask to forward to channel ─────────────────────────────────
    _channel = user_setting(msg.chat.id, "channel_id")
    if _channel:
        from handlers.workflow import FORWARD_PENDING, _forward_keyboard
        _auto_fwd = user_setting(msg.chat.id, "auto_forward")
        if _auto_fwd:
            try:
                await client.copy_message(
                    chat_id=_channel,
                    from_chat_id=sent.chat.id,
                    message_id=sent.id,
                )
            except Exception as e:
                logger.error(f"Auto-forward failed: {e}")
        else:
            FORWARD_PENDING[sent.id] = {
                "chat_id":    sent.chat.id,
                "message_id": sent.id,
                "channel_id": _channel,
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


async def _get_dimensions(video_path: str) -> tuple[int, int]:
    """Get video width and height via ffprobe."""
    import shutil, subprocess
    ffprobe = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    try:
        r = subprocess.run([
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
        if len(lines) >= 2:
            return int(lines[0]), int(lines[1])
    except Exception:
        pass
    return 0, 0


async def _make_thumb(video_path: str) -> str | None:
    """Generate a thumbnail from the video."""
    import shutil, subprocess
    ffmpeg = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    thumb  = video_path.replace(Path(video_path).suffix, "_thumb.jpg")
    try:
        subprocess.run([
            ffmpeg, "-y", "-i", video_path,
            "-ss", "00:00:03", "-vframes", "1",
            "-vf", "scale=320:-1", "-q:v", "2", thumb,
        ], capture_output=True, timeout=15)
        return thumb if os.path.exists(thumb) else None
    except Exception:
        return None


async def _get_duration(video_path: str) -> int:
    """Get video duration in seconds."""
    import shutil, subprocess
    ffprobe = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    try:
        r = subprocess.run([
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ], capture_output=True, text=True, timeout=10)
        return int(float(r.stdout.strip()))
    except Exception:
        return 0
