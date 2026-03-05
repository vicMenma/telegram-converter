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
from user_client import get_user_client
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


# Links are handled by workflow.py recv_text → shows operation keyboard first


# ── yt-dlp resolution chosen ──────────────────────────────────────
@app.on_callback_query(filters.regex(r"^leech:"))
async def leech_callback(client: Client, cb: CallbackQuery):
    parts = cb.data.split(":")

    if parts[1] == "cancel":
        uid = cb.from_user.id
        YTDLP_STATE.pop(uid, None)
        await cb.message.edit("✕ <i>Cancelled.</i>")
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
        f"📥 <b>Downloading</b> <code>{label}</code>\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n<code>░░░░░░░░░░░░░░░░░░░░</code> 0%"
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
        await progress_msg.edit(f"❌ <b>Download failed</b>\n\n<code>{str(e)[:200]}</code>")
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
        await status.edit(f"❌ <b>Download failed</b>\n\n<code>{str(e)[:200]}</code>")
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
        await status.edit(f"❌ <b>Download failed</b>\n\n<code>{str(e)[:200]}</code>")
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

    VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".ts", ".3gp"}

    await progress_msg.edit(
        f"📤 <i>Uploading…</i> <code>0%</code>\n<code>░░░░░░░░░░░░░░░░░░░░</code>\n📦 {format_size(size)}"
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
                f"📤 <i>Uploading…</i> <b>{pct}%</b>\n"
                f"<code>{bar}</code>\n"
                f"📦 {format_size(current)} / {format_size(real_total)}\n"
                f"🚀 {speed_str}  ·  ⏱ {eta_str}"
            )
        else:
            text = f"📤 <i>Uploading…</i>\n📦 {format_size(current)}  ·  🚀 {speed_str}"
        try:
            await progress_msg.edit(text)
        except Exception:
            pass

    caption = f"✅ Done"

    upload_type = user_setting(msg.chat.id, "upload_type")
    uploader    = client  # always use bot client — sends to user's chat, not Saved Messages

    if ext in VIDEO_EXTS and upload_type == "video":
        thumb = await _make_thumb(file_path)
        duration = await _get_duration(file_path)
        width, height = await _get_dimensions(file_path)
        sent = await uploader.send_video(
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
        sent = await uploader.send_document(
            chat_id=msg.chat.id,
            document=file_path,
            caption=caption,
            file_name=file_name,
            progress=upload_progress,
        )

    await progress_msg.delete()

    # ── Ask to forward to channels ────────────────────────────────
    from handlers.workflow import FORWARD_PENDING, _forward_keyboard
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
                    logger.error(f"Auto-forward to {_ch} failed: {e}")
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
