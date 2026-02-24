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
from config import (
    TEMP_DIR,
    VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS,
    RESOLUTIONS, MAX_FILE_SIZE_BYTES,
)
from utils.file_utils import format_size, output_filename, cleanup
from processors.ffmpeg import burn_subtitles, change_resolution, download_url

logger = logging.getLogger(__name__)

# ── In-memory state store ─────────────────────────────────────────
# { user_id: { "state": str, "source": str, "file_id": str, ... } }
STATE: dict[int, dict] = {}

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


# ── Keyboards ─────────────────────────────────────────────────────
def operation_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔤 Burn Subtitles",    callback_data="op:subtitles"),
            InlineKeyboardButton("📐 Change Resolution", callback_data="op:resolution"),
        ],
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
                f"⏳ **{action}**\n\n"
                f"`{bar}`\n"
                f"**{pct}%** — {format_size(current)} / {format_size(real_total)}\n"
                f"🚀 {speed_str} · ⏱ ETA {eta_str}"
            )
        else:
            text = (
                f"⏳ **{action}**\n\n"
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
    else:
        short = url[:60] + "…" if len(url) > 60 else url
        desc  = f"🔗 `{short}`"

    await msg.reply(
        f"🎬 **Video ready**\n\n{desc}\n\nWhat do you want to do?",
        reply_markup=operation_keyboard(),
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
        if ext not in VIDEO_EXTENSIONS:
            if ext in SUBTITLE_EXTENSIONS:
                await msg.reply("📝 Send your **video** first — then I'll ask for the subtitle.")
            else:
                await msg.reply("⚠️ Unrecognised file. Send a video file or a direct URL.")
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
        await msg.reply("📎 Please send the subtitle as a **file attachment**.")
        return

    match = URL_RE.search(msg.text or "")
    if not match:
        await msg.reply(
            "👋 Send me a video file or a direct URL to get started.\n\n"
            "Example: `https://example.com/video.mp4`\n\n"
            "Use /help for instructions."
        )
        return

    url = match.group(0)
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
        await cb.message.edit("❌ Cancelled.")
        await cb.answer()
        return

    if not data.get("source"):
        await cb.answer("⚠️ Session expired — send your video again.", show_alert=True)
        return

    if op == "subtitles":
        STATE[uid]["state"] = "waiting_for_subtitle"
        await cb.message.edit(
            "🔤 **Burn Subtitles**\n\n"
            "Now send me your subtitle file.\n"
            "Accepted: `.srt  .ass  .ssa  .vtt  .sub`"
        )
    elif op == "resolution":
        STATE[uid]["state"] = "choosing_resolution"
        await cb.message.edit(
            "📐 **Change Resolution**\n\nChoose the target resolution:",
            reply_markup=resolution_keyboard(),
        )

    await cb.answer()


# ── Subtitle processing ───────────────────────────────────────────
async def _process_subtitle(client: Client, msg: Message):
    uid  = msg.from_user.id
    data = STATE.pop(uid, {})
    ext  = Path(msg.document.file_name or "").suffix.lower()

    if ext not in SUBTITLE_EXTENSIONS:
        await msg.reply(
            f"⚠️ **{ext}** is not a supported subtitle format.\n"
            f"Accepted: {', '.join(sorted(SUBTITLE_EXTENSIONS))}"
        )
        return

    progress_msg = await msg.reply("⏳ **Starting…**")
    job_id       = str(uuid.uuid4())[:8]
    video_path   = sub_path = output_path = None

    try:
        # Download video
        video_path = await _get_video(client, data, job_id, progress_msg)

        # Download subtitle
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

        output_path = await burn_subtitles(video_path, sub_path, ffmpeg_progress_sub)
        elapsed     = time.monotonic() - t0

        out_name = output_filename(data.get("file_name") or "video.mp4", "subtitled")
        await _send_output(client, msg, progress_msg, output_path, out_name, elapsed)

    except Exception as e:
        logger.error(f"Subtitle burn failed: {e}", exc_info=True)
        await progress_msg.edit(
            f"❌ **Failed to burn subtitles**\n\n`{str(e)[:300]}`"
        )
    finally:
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

    try:
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

        output_path = await change_resolution(video_path, scale, ffmpeg_progress_res)
        elapsed     = time.monotonic() - t0

        out_name = output_filename(data.get("file_name") or "video.mp4", res_key)
        await _send_output(client, cb.message, progress_msg, output_path, out_name, elapsed)

    except Exception as e:
        logger.error(f"Resolution change failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ **Conversion failed**\n\n`{str(e)[:300]}`")
    finally:
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
            progress=make_progress(progress_msg, "Downloading from Telegram…", known_total=file_size),
        )
        return video_path

    elif source == "url":
        await progress_msg.edit("⏳ **Downloading from URL…**\n_Large files may take a while._")
        return await download_url(data["url"], job_id)

    raise RuntimeError("Unknown video source in session state.")


async def _send_output(client: Client, msg: Message, progress_msg,
                       output_path: str, out_name: str, elapsed: float):
    """Upload the processed file back to the user with thumbnail and duration."""
    import subprocess, shutil as _shutil

    out_size = os.path.getsize(output_path)

    # ── Generate thumbnail ────────────────────────────────────────
    thumb_path = output_path.replace(".mp4", "_thumb.jpg")
    try:
        _ffmpeg_bin = _shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
        subprocess.run([
            _ffmpeg_bin, "-y",
            "-i", output_path,
            "-ss", "00:00:03",
            "-vframes", "1",
            "-vf", "scale=320:-1",
            "-q:v", "2",
            thumb_path,
        ], capture_output=True, timeout=30)
    except Exception:
        thumb_path = None

    # ── Get duration ──────────────────────────────────────────────
    duration = 0
    try:
        _ffprobe_bin = _shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
        result = subprocess.run([
            _ffprobe_bin, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            output_path,
        ], capture_output=True, text=True, timeout=10)
        duration = int(float(result.stdout.strip()))
    except Exception:
        duration = 0

    await progress_msg.edit(
        f"⏳ **Uploading…**\n`░░░░░░░░░░░░░░░░░░░░` 0%\n📦 {format_size(out_size)}"
    )

    await client.send_video(
        chat_id=msg.chat.id,
        video=output_path,
        thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
        duration=duration,
        caption=(
            f"✅ **Done!**\n\n"
            f"📁 `{out_name}`\n"
            f"📦 {format_size(out_size)}  ·  ⚡ {elapsed:.1f}s"
        ),
        file_name=out_name,
        supports_streaming=True,
        progress=make_progress(progress_msg, "Uploading…"),
    )

    await progress_msg.delete()

    # Cleanup thumbnail
    if thumb_path and os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
        except Exception:
            pass
