"""
Extra features handler:
  - 🗜️ Compress to target MB
  - 📊 MediaInfo
  - 🎵 Stream extractor (audio, video-only, subtitles)
"""

import os
import logging
import uuid
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from client import app
from config import TEMP_DIR
from utils.file_utils import format_size, cleanup, output_filename
from utils.queue import register, finish, update_status
# workflow imports done lazily inside functions to avoid circular imports

logger = logging.getLogger(__name__)

# ── Compress state: uid → {source, file_id, file_name, file_size, url} ──
COMPRESS_STATE: dict[int, dict] = {}

# ── Stream state: uid → {source, ..., streams: [...]} ──────────────────
STREAM_STATE: dict[int, dict] = {}

# ── Waiting for compress MB input ──────────────────────────────────────
WAITING_COMPRESS: dict[int, bool] = {}


# ═══════════════════════════════════════════════════════════════════
# ── Operation keyboard additions ───────────────────────────────────
# ═══════════════════════════════════════════════════════════════════

def extra_keyboard() -> InlineKeyboardMarkup:
    """Shown after video is ready — extra features row."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗜️ Compress",   callback_data="op:compress"),
            InlineKeyboardButton("📊 MediaInfo",  callback_data="op:mediainfo"),
            InlineKeyboardButton("🎵 Streams",    callback_data="op:streams"),
        ],
    ])


# ═══════════════════════════════════════════════════════════════════
# ── MediaInfo ──────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^op:mediainfo$"))
async def cb_mediainfo(client: Client, cb: CallbackQuery):
    from handlers.workflow import STATE
    uid  = cb.from_user.id
    data = STATE.get(uid, {})
    if not data.get("source"):
        await cb.answer("⏰ Session expired — send your video again.", show_alert=True)
        return

    await cb.answer()
    progress_msg = await cb.message.edit("<i>Analysing…</i>")

    job_id   = str(uuid.uuid4())[:8]
    username = cb.from_user.username or cb.from_user.first_name or str(uid)
    register(job_id, uid, username, "mediainfo", data.get("file_name", "video"))

    video_path = None
    try:
        from processors.ffmpeg import get_media_info, format_media_info
        update_status(job_id, "📥 Downloading…")
        video_path = await _get_video(client, data, job_id, progress_msg)

        update_status(job_id, "📊 Analysing…")
        info = await get_media_info(video_path)
        text = format_media_info(info, filename=data.get("file_name", ""))

        await progress_msg.edit(text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("‹ Back", callback_data="op:back")]
        ]))
    except Exception as e:
        logger.error(f"MediaInfo failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ <b>MediaInfo failed</b>\n\n<code>{str(e)[:200]}</code>")
    finally:
        finish(job_id)
        cleanup(video_path)


# ═══════════════════════════════════════════════════════════════════
# ── Compress ───────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^op:compress$"))
async def cb_compress(client: Client, cb: CallbackQuery):
    from handlers.workflow import STATE
    uid  = cb.from_user.id
    data = STATE.get(uid, {})
    if not data.get("source"):
        await cb.answer("⏰ Session expired — send your video again.", show_alert=True)
        return

    COMPRESS_STATE[uid] = dict(data)
    WAITING_COMPRESS[uid] = True
    await cb.answer()

    file_size = data.get("file_size", 0)
    size_hint = f"\n> Current size: <b>{format_size(file_size)}</b>" if file_size else ""

    await cb.message.edit(
        "🗜️✨ <b>COMPRESS TO TARGET SIZE</b> ✨🗜️\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
        f"Type the target file size in MB:{size_hint}\n\n"
        "> Examples: <code>500</code> · <code>250</code> · <code>100</code>\n\n"
        "⚠️ <i>Very small targets reduce quality significantly</i>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✕ Cancel", callback_data="op:cancel_compress")]
        ])
    )


@app.on_callback_query(filters.regex(r"^op:cancel_compress$"))
async def cb_cancel_compress(client: Client, cb: CallbackQuery):
    uid = cb.from_user.id
    WAITING_COMPRESS.pop(uid, None)
    COMPRESS_STATE.pop(uid, None)
    await cb.answer()
    # Restore operation keyboard
    data = STATE.get(uid, {})
    if data:
        from handlers.workflow import STATE as _STATE, operation_keyboard
        data2 = _STATE.get(uid, {})
        mode  = data2.get("mode", "upload") if data2 else "upload"
        await cb.message.edit(
            "🎬✨ <b>VIDEO READY</b> ✨🎬\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "<i>What would you like to do with it?</i>",
            reply_markup=operation_keyboard(mode=mode)
        )
    else:
        await cb.message.edit("<i>Cancelled.</i>")


async def _run_compress(client: Client, msg: Message, status, uid: int, target_mb: float):
    """Run the actual compress job."""
    data     = COMPRESS_STATE.pop(uid, {})
    job_id   = str(uuid.uuid4())[:8]
    username = msg.from_user.username or msg.from_user.first_name or str(uid)
    register(job_id, uid, username, "compress", f"→ {target_mb:.0f} MB")

    video_path = output_path = None
    try:
        from processors.ffmpeg import compress_to_size

        update_status(job_id, "📥 Downloading…")
        video_path = await _get_video(client, data, job_id, status)

        t0 = time.monotonic()

        async def compress_progress(pct, speed, eta):
            filled = pct // 5
            bar    = "█" * filled + "░" * (20 - filled)
            try:
                await status.edit(
                    f"🗜️ <b>Compressing…</b> <b>{pct}%</b>\n"
                    f"<code>{bar}</code>\n"
                    f"🎯 Target: <b>{target_mb:.0f} MB</b>  ·  🚀 {speed}  ·  ⏱ {eta}"
                )
            except Exception:
                pass

        update_status(job_id, "🗜️ Compressing…")
        output_path = await compress_to_size(video_path, target_mb, compress_progress, uid=uid)
        elapsed     = time.monotonic() - t0

        update_status(job_id, "📤 Uploading…")
        out_name = output_filename(data.get("file_name") or "video.mp4", f"compressed_{target_mb:.0f}MB")
        from handlers.workflow import _send_output
        await _send_output(client, msg, status, output_path, out_name, elapsed)

    except Exception as e:
        logger.error(f"Compress failed: {e}", exc_info=True)
        await status.edit(f"❌ <b>Compression failed</b>\n\n<code>{str(e)[:300]}</code>")
    finally:
        finish(job_id)
        cleanup(video_path, output_path)


# ═══════════════════════════════════════════════════════════════════
# ── Stream extractor ───────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^op:streams$"))
async def cb_streams(client: Client, cb: CallbackQuery):
    from handlers.workflow import STATE
    uid  = cb.from_user.id
    data = STATE.get(uid, {})
    if not data.get("source"):
        await cb.answer("⏰ Session expired — send your video again.", show_alert=True)
        return

    await cb.answer()
    progress_msg = await cb.message.edit("<i>Reading streams…</i>")

    job_id   = str(uuid.uuid4())[:8]
    username = cb.from_user.username or cb.from_user.first_name or str(uid)
    register(job_id, uid, username, "streams", data.get("file_name", "video"))

    video_path = None
    try:
        from processors.ffmpeg import list_streams
        update_status(job_id, "📥 Downloading…")
        video_path = await _get_video(client, data, job_id, progress_msg)

        streams = await list_streams(video_path)
        if not streams:
            await progress_msg.edit(
                "⚠️ <b>No extractable streams found</b>\n\n"
                "<i>This file has no separate audio or subtitle tracks.</i>"
            )
            finish(job_id)
            cleanup(video_path)
            return

        # Store for extraction
        STREAM_STATE[uid] = dict(data)
        STREAM_STATE[uid]["streams"]    = streams
        STREAM_STATE[uid]["video_path"] = video_path  # already downloaded

        await progress_msg.edit(
            "🎵✨ <b>STREAM EXTRACTOR</b> ✨🎵\n"
            "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
            "<i>Choose a stream to extract:</i>",
            reply_markup=_streams_keyboard(streams)
        )
    except Exception as e:
        logger.error(f"List streams failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ <b>Failed to read streams</b>\n\n<code>{str(e)[:200]}</code>")
        cleanup(video_path)
    finally:
        finish(job_id)


def _streams_keyboard(streams: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for s in streams:
        idx   = s["index"]
        stype = s["type"]
        codec = s["codec"].upper()
        lang  = f" [{s['lang']}]" if s["lang"] else ""
        title = f" {s['title']}" if s["title"] else ""

        if stype == "audio":
            ch_str = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(s["channels"], f"{s['channels']}ch")
            label  = f"🔊 Audio{lang}{title} — {codec} {ch_str}"
            rows.append([InlineKeyboardButton(label, callback_data=f"stream:audio:{idx}")])
        elif stype == "subtitle":
            label = f"💬 Subtitle{lang}{title} — {codec}"
            rows.append([InlineKeyboardButton(label, callback_data=f"stream:sub:{idx}")])

    rows.append([InlineKeyboardButton("✕ Cancel", callback_data="stream:cancel")])
    return InlineKeyboardMarkup(rows)


def _audio_format_keyboard(stream_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 MP3",  callback_data=f"streamfmt:mp3:{stream_index}"),
            InlineKeyboardButton("🎵 AAC",  callback_data=f"streamfmt:aac:{stream_index}"),
            InlineKeyboardButton("🎵 FLAC", callback_data=f"streamfmt:flac:{stream_index}"),
            InlineKeyboardButton("🎵 OPUS", callback_data=f"streamfmt:opus:{stream_index}"),
        ],
        [InlineKeyboardButton("‹ Back", callback_data="stream:back")],
    ])


def _sub_format_keyboard(stream_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 SRT", callback_data=f"streamfmt:srt:{stream_index}"),
            InlineKeyboardButton("📄 ASS", callback_data=f"streamfmt:ass:{stream_index}"),
            InlineKeyboardButton("📄 VTT", callback_data=f"streamfmt:vtt:{stream_index}"),
        ],
        [InlineKeyboardButton("‹ Back", callback_data="stream:back")],
    ])


@app.on_callback_query(filters.regex(r"^stream:"))
async def cb_stream_select(client: Client, cb: CallbackQuery):
    uid    = cb.from_user.id
    parts  = cb.data.split(":")
    action = parts[1]

    if action == "cancel":
        STREAM_STATE.pop(uid, None)
        await cb.answer()
        await cb.message.edit("✕ <i>Cancelled.</i>")
        return

    if action == "back":
        await cb.answer()
        state = STREAM_STATE.get(uid, {})
        streams = state.get("streams", [])
        if streams:
            await cb.message.edit(
                "🎵✨ <b>STREAM EXTRACTOR</b> ✨🎵\n"
                "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
                "<i>Choose a stream to extract:</i>",
                reply_markup=_streams_keyboard(streams)
            )
        return

    if action == "audio":
        stream_index = int(parts[2])
        await cb.answer()
        await cb.message.edit(
            "🎵 <b>Choose audio format:</b>",
            reply_markup=_audio_format_keyboard(stream_index)
        )
        return

    if action == "sub":
        stream_index = int(parts[2])
        await cb.answer()
        await cb.message.edit(
            "💬 <b>Choose subtitle format:</b>",
            reply_markup=_sub_format_keyboard(stream_index)
        )
        return


@app.on_callback_query(filters.regex(r"^streamfmt:"))
async def cb_stream_extract(client: Client, cb: CallbackQuery):
    uid    = cb.from_user.id
    parts  = cb.data.split(":")
    fmt    = parts[1]
    idx    = int(parts[2])

    state = STREAM_STATE.pop(uid, {})
    if not state:
        await cb.answer("⏰ Session expired.", show_alert=True)
        return

    await cb.answer()
    progress_msg = await cb.message.edit(f"<i>Extracting stream…</i>")

    job_id   = str(uuid.uuid4())[:8]
    username = cb.from_user.username or cb.from_user.first_name or str(uid)
    register(job_id, uid, username, "extract", f"stream #{idx} → {fmt}")

    video_path  = state.get("video_path")
    output_path = None
    owns_video  = bool(video_path)  # already downloaded during list_streams

    try:
        from processors.ffmpeg import extract_audio, extract_subtitle

        # Download if not already cached
        if not video_path or not os.path.exists(video_path):
            owns_video = True
            progress_msg2 = await progress_msg.edit("📥 <i>Downloading…</i>")
            video_path = await _get_video(client, state, job_id, progress_msg2)

        update_status(job_id, f"🎵 Extracting…")

        audio_fmts = {"mp3", "aac", "flac", "opus"}
        sub_fmts   = {"srt", "ass", "vtt"}

        if fmt in audio_fmts:
            async def audio_progress(pct, speed, eta):
                filled = pct // 5
                bar    = "█" * filled + "░" * (20 - filled)
                try:
                    await progress_msg.edit(
                        f"🎵 <b>Extracting audio…</b> <b>{pct}%</b>\n"
                        f"<code>{bar}</code>\n"
                        f"🚀 {speed}  ·  ⏱ {eta}"
                    )
                except Exception:
                    pass

            output_path = await extract_audio(video_path, idx, fmt, audio_progress)

        elif fmt in sub_fmts:
            await progress_msg.edit(f"💬 <i>Extracting subtitle…</i>")
            output_path = await extract_subtitle(video_path, idx, fmt)
        else:
            raise ValueError(f"Unknown format: {fmt}")

        update_status(job_id, "📤 Uploading…")

        # Send as document (audio/sub files)
        out_size = os.path.getsize(output_path)
        out_name = Path(output_path).name

        await progress_msg.edit(
            f"📤 <i>Uploading…</i>\n📦 {format_size(out_size)}"
        )
        await client.send_document(
            chat_id=cb.message.chat.id,
            document=output_path,
            caption=f"✅ <b>Done</b>",
            file_name=out_name,
        )
        await progress_msg.delete()

    except Exception as e:
        logger.error(f"Stream extract failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ <b>Extraction failed</b>\n\n<code>{str(e)[:300]}</code>")
    finally:
        finish(job_id)
        if owns_video:
            cleanup(video_path)
        cleanup(output_path)


# ═══════════════════════════════════════════════════════════════════
# ── Shared: receive compress MB input ──────────────────────────────
# ═══════════════════════════════════════════════════════════════════

@app.on_message(filters.private & filters.text, group=2)
async def features_text_input(client: Client, msg: Message):
    uid = msg.from_user.id
    if uid not in WAITING_COMPRESS:
        return

    WAITING_COMPRESS.pop(uid)
    text = msg.text.strip().lower().replace("mb", "").replace("mib", "").strip()

    try:
        target_mb = float(text)
        assert 1 <= target_mb <= 4096
    except Exception:
        await msg.reply(
            "❌ <b>Invalid size</b>\n\n"
            "Enter a number between <code>1</code> and <code>4096</code> MB.\n"
            "Example: <code>500</code>"
        )
        WAITING_COMPRESS[uid] = True  # keep waiting
        return

    status = await msg.reply(
        f"🗜️ <i>Starting compression to {target_mb:.0f} MB…</i>"
    )
    await _run_compress(client, msg, status, uid, target_mb)


# ═══════════════════════════════════════════════════════════════════
# ── Shared: _get_video (mirror from workflow) ───────────────────────
# ═══════════════════════════════════════════════════════════════════

async def _get_video(client, data, job_id, progress_msg):
    """Download video based on source type."""
    from processors.ffmpeg import download_url
    from processors.leech import ytdlp_download
    source = data.get("source")

    if source == "upload":
        ext        = Path(data.get("file_name", "video.mp4")).suffix.lower() or ".mp4"
        video_path = os.path.join(TEMP_DIR, f"{job_id}_video{ext}")
        file_size  = data.get("file_size", 0)
        await progress_msg.edit(
            "📥 <i>Downloading from Telegram…</i>\n<code>░░░░░░░░░░░░░░░░░░░░</code> 0%"
        )
        await client.download_media(
            data["file_id"],
            file_name=video_path,
            progress=make_progress(progress_msg, "Downloading…", known_total=file_size),
        )
        return video_path

    elif source == "url":
        await progress_msg.edit("🌐 <i>Downloading from URL…</i>")
        return await download_url(data["url"], job_id, progress_msg=progress_msg)

    elif source == "ytdlp":
        await progress_msg.edit("📡 <i>Downloading HLS stream…</i>")
        return await ytdlp_download(data["url"], "bestvideo+bestaudio/best", job_id, progress_msg=progress_msg)

    elif source == "magnet":
        from processors.leech import magnet_download
        await progress_msg.edit("🧲 <i>Connecting to peers…</i>")
        return await magnet_download(data["url"], job_id, progress_msg=progress_msg)

    raise ValueError(f"Unknown source: {source}")
