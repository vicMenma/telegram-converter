"""
Extra features:
  - 📊 MediaInfo
  - 🎵 Stream extractor (audio + subtitles)
"""

import os
import logging
import uuid
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from client import app
from config import TEMP_DIR
from utils.file_utils import format_size, cleanup
from utils.queue import register, finish, update_status

logger = logging.getLogger(__name__)

# ── Stream state: uid → {source, ..., streams, video_path} ────────
STREAM_STATE: dict[int, dict] = {}


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
    progress_msg = await cb.message.edit("<i>Fetching media info…</i>")

    job_id   = str(uuid.uuid4())[:8]
    username = cb.from_user.username or cb.from_user.first_name or str(uid)
    register(job_id, uid, username, "mediainfo", data.get("file_name", "video"))

    video_path = None
    try:
        from processors.ffmpeg import get_media_info, format_media_info
        update_status(job_id, "📥 Downloading…")
        video_path = await _get_video(client, data, job_id, progress_msg)

        update_status(job_id, "📊 Reading info…")
        info = await get_media_info(video_path)
        text = format_media_info(info, data.get("file_name", "video"))

        from handlers.workflow import operation_keyboard
        mode = data.get("mode", "upload")
        await progress_msg.edit(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("‹ Back", callback_data="op:back")]
            ])
        )
    except Exception as e:
        logger.error(f"MediaInfo failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ <b>MediaInfo failed</b>\n\n<code>{str(e)[:300]}</code>")
    finally:
        finish(job_id)
        cleanup(video_path)


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

        STREAM_STATE[uid] = {**data, "streams": streams, "video_path": video_path}

        await progress_msg.edit(
            "🎵 <b>STREAM EXTRACTOR</b>\n\n<i>Choose a stream to extract:</i>",
            reply_markup=_streams_keyboard(streams)
        )
    except Exception as e:
        logger.error(f"List streams failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ <b>Failed to read streams</b>\n\n<code>{str(e)[:200]}</code>")
        cleanup(video_path)
    finally:
        finish(job_id)


# ISO 639-2/1 language code → display name + flag
_LANG_MAP = {
    "fre": ("French", "🇫🇷"), "fra": ("French", "🇫🇷"), "fr": ("French", "🇫🇷"),
    "eng": ("English", "🇬🇧"), "en": ("English", "🇬🇧"),
    "jpn": ("Japanese", "🇯🇵"), "ja": ("Japanese", "🇯🇵"),
    "ger": ("German", "🇩🇪"), "deu": ("German", "🇩🇪"), "de": ("German", "🇩🇪"),
    "spa": ("Spanish", "🇪🇸"), "esp": ("Spanish", "🇪🇸"), "es": ("Spanish", "🇪🇸"),
    "ita": ("Italian", "🇮🇹"), "it": ("Italian", "🇮🇹"),
    "por": ("Portuguese", "🇵🇹"), "pt": ("Portuguese", "🇵🇹"),
    "rus": ("Russian", "🇷🇺"), "ru": ("Russian", "🇷🇺"),
    "ara": ("Arabic", "🇸🇦"), "ar": ("Arabic", "🇸🇦"),
    "chi": ("Chinese", "🇨🇳"), "zho": ("Chinese", "🇨🇳"), "zh": ("Chinese", "🇨🇳"),
    "kor": ("Korean", "🇰🇷"), "ko": ("Korean", "🇰🇷"),
    "dut": ("Dutch", "🇳🇱"), "nld": ("Dutch", "🇳🇱"), "nl": ("Dutch", "🇳🇱"),
    "pol": ("Polish", "🇵🇱"), "pl": ("Polish", "🇵🇱"),
    "tur": ("Turkish", "🇹🇷"), "tr": ("Turkish", "🇹🇷"),
    "vie": ("Vietnamese", "🇻🇳"), "vi": ("Vietnamese", "🇻🇳"),
    "tha": ("Thai", "🇹🇭"), "th": ("Thai", "🇹🇭"),
    "ind": ("Indonesian", "🇮🇩"), "id": ("Indonesian", "🇮🇩"),
    "und": ("Unknown", "❓"),
}

def _lang_label(lang: str, title: str, idx: int) -> str:
    """Return a readable language label. Falls back to title, then stream index."""
    # Try lang code first
    if lang and lang.lower() not in ("und", ""):
        name, flag = _LANG_MAP.get(lang.lower(), (lang.upper(), "🌐"))
        label = f"{flag} {name}"
        if title:
            label += f" ({title})"
        return label
    # Use title if available and meaningful
    if title and title.lower() not in ("und", "unknown", ""):
        # Check if title itself is a language code
        t = title.strip().lower()
        if t in _LANG_MAP:
            name, flag = _LANG_MAP[t]
            return f"{flag} {name}"
        return f"🌐 {title}"
    # Last resort: stream number
    return f"🌐 Track {idx}"


def _streams_keyboard(streams: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    # Count types to number them if multiple unknowns
    audio_count = 0
    sub_count   = 0
    for s in streams:
        idx    = s["index"]
        codec  = s["codec"].upper()
        lang_l = _lang_label(s["lang"], s["title"], idx)
        if s["type"] == "audio":
            audio_count += 1
            ch_str = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(s["channels"], f"{s['channels']}ch")
            label  = f"🔊 Audio {audio_count} · {lang_l} — {codec} {ch_str}"
            rows.append([InlineKeyboardButton(label, callback_data=f"stream:audio:{idx}")])
        elif s["type"] == "subtitle":
            sub_count += 1
            label = f"💬 Sub {sub_count} · {lang_l} — {codec}"
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
        state = STREAM_STATE.pop(uid, {})
        cleanup(state.get("video_path"))
        await cb.answer()
        await cb.message.edit("✕ <i>Cancelled.</i>")
        return

    if action == "back":
        await cb.answer()
        streams = STREAM_STATE.get(uid, {}).get("streams", [])
        if streams:
            await cb.message.edit(
                "🎵 <b>STREAM EXTRACTOR</b>\n\n<i>Choose a stream to extract:</i>",
                reply_markup=_streams_keyboard(streams)
            )
        return

    if action == "audio":
        await cb.answer()
        await cb.message.edit("🎵 <b>Choose audio format:</b>", reply_markup=_audio_format_keyboard(int(parts[2])))
        return

    if action == "sub":
        await cb.answer()
        await cb.message.edit("💬 <b>Choose subtitle format:</b>", reply_markup=_sub_format_keyboard(int(parts[2])))
        return


@app.on_callback_query(filters.regex(r"^streamfmt:"))
async def cb_stream_extract(client: Client, cb: CallbackQuery):
    uid   = cb.from_user.id
    parts = cb.data.split(":")
    fmt   = parts[1]
    idx   = int(parts[2])

    state = STREAM_STATE.pop(uid, {})
    if not state:
        await cb.answer("⏰ Session expired.", show_alert=True)
        return

    await cb.answer()
    progress_msg = await cb.message.edit("<i>Extracting stream…</i>")

    job_id   = str(uuid.uuid4())[:8]
    username = cb.from_user.username or cb.from_user.first_name or str(uid)
    register(job_id, uid, username, "extract", f"stream #{idx} → {fmt}")

    video_path  = state.get("video_path")
    output_path = None

    try:
        from processors.ffmpeg import extract_audio, extract_subtitle

        if not video_path or not os.path.exists(video_path):
            video_path = await _get_video(client, state, job_id, progress_msg)

        update_status(job_id, "🎵 Extracting…")

        if fmt in {"mp3", "aac", "flac", "opus"}:
            async def audio_progress(pct, speed, eta):
                bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                try:
                    await progress_msg.edit(
                        f"🎵 <b>Extracting audio…</b> <b>{pct}%</b>\n"
                        f"<code>{bar}</code>\n🚀 {speed}  ·  ⏱ {eta}"
                    )
                except Exception:
                    pass
            output_path = await extract_audio(video_path, idx, fmt, audio_progress)

        elif fmt in {"srt", "ass", "vtt"}:
            await progress_msg.edit("<i>Extracting subtitle…</i>")
            output_path = await extract_subtitle(video_path, idx, fmt)
        else:
            raise ValueError(f"Unknown format: {fmt}")

        update_status(job_id, "📤 Uploading…")
        out_size = os.path.getsize(output_path)
        await progress_msg.edit(f"📤 <i>Uploading…</i>\n📦 {format_size(out_size)}")
        await client.send_document(
            chat_id=cb.message.chat.id,
            document=output_path,
            caption="✅ <b>Done</b>",
            file_name=Path(output_path).name,
        )
        await progress_msg.delete()

    except Exception as e:
        logger.error(f"Stream extract failed: {e}", exc_info=True)
        await progress_msg.edit(f"❌ <b>Extraction failed</b>\n\n<code>{str(e)[:300]}</code>")
    finally:
        finish(job_id)
        cleanup(video_path, output_path)


# ═══════════════════════════════════════════════════════════════════
# ── Shared: download video from any source ─────────────────────────
# ═══════════════════════════════════════════════════════════════════

async def _get_video(client, data, job_id, progress_msg):
    source = data.get("source")

    if source == "upload":
        from handlers.workflow import make_progress
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
        from processors.leech import direct_download
        await progress_msg.edit("🌐 <i>Downloading from URL…</i>")
        return await direct_download(data["url"], job_id, progress_msg=progress_msg)

    elif source == "ytdlp":
        from processors.leech import ytdlp_download
        await progress_msg.edit("📡 <i>Downloading HLS stream…</i>")
        return await ytdlp_download(data["url"], "bestvideo+bestaudio/best", job_id, progress_msg=progress_msg)

    elif source == "magnet":
        from processors.leech import magnet_download
        await progress_msg.edit("🧲 <i>Connecting to peers…</i>")
        return await magnet_download(data["url"], job_id, progress_msg=progress_msg)

    raise ValueError(f"Unknown source: {source}")
