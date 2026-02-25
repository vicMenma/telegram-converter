"""
File upload handler — receives any file type, shows format selection keyboard
"""

import os
import logging
from aiogram import Router, Bot, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import FORMAT_MAP, MAX_FILE_SIZE_BYTES, get_file_category, TEMP_DIR
from utils.file_utils import format_size, file_icon, sniff_mime, is_conversion_possible

logger = logging.getLogger(__name__)
router = Router()


class ConversionState(StatesGroup):
    waiting_for_format = State()


def build_format_keyboard(category: str, page: int = 0) -> InlineKeyboardMarkup:
    """Build paginated inline keyboard of output formats."""
    formats = FORMAT_MAP[category]["formats"]
    icon = FORMAT_MAP[category]["icon"]
    
    PAGE_SIZE = 8
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_formats = formats[start:end]
    
    # Build buttons in rows of 4
    rows = []
    row = []
    for i, fmt in enumerate(page_formats):
        row.append(InlineKeyboardButton(
            text=fmt.upper(),
            callback_data=f"fmt:{fmt}"
        ))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    
    # Pagination row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀ Back", callback_data=f"page:{category}:{page-1}"))
    if end < len(formats):
        nav_row.append(InlineKeyboardButton(text="Next ▶", callback_data=f"page:{category}:{page+1}"))
    if nav_row:
        rows.append(nav_row)
    
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def process_incoming_file(msg: Message, state: FSMContext, file_id: str, 
                                 file_name: str, file_size: int, mime_type: str):
    """Shared logic for all file types."""
    
    # Size check
    if file_size and file_size > MAX_FILE_SIZE_BYTES:
        await msg.answer(
            f"❌ File too large! Max size is <b>50 MB</b>.\n"
            f"Your file: <b>{file_size / 1024 / 1024:.1f} MB</b>"
        )
        return
    
    # Detect category
    category = get_file_category(mime_type or "", file_name or "")
    
    if not category:
        await msg.answer(
            "❓ <b>Unsupported file type</b>\n\n"
            "Send /formats to see all supported formats."
        )
        return
    
    info = FORMAT_MAP[category]
    
    # Save file info to FSM state
    await state.set_state(ConversionState.waiting_for_format)
    await state.update_data(
        file_id=file_id,
        file_name=file_name,
        mime_type=mime_type,
        category=category,
        original_message_id=msg.message_id,
    )
    
    icon     = file_icon(file_name or "", mime_type or "")
    size_str = format_size(file_size)

    await msg.answer(
        f"{icon} <b>File received!</b>\n\n"
        f"📁 <b>Name:</b> <code>{file_name or 'Unknown'}</code>\n"
        f"📦 <b>Size:</b> {size_str}\n"
        f"🏷️ <b>Type:</b> {info['label']}\n\n"
        f"👇 <b>Choose output format:</b>",
        reply_markup=build_format_keyboard(category)
    )


# ─── Handlers for each Telegram file type ──────────────────────

@router.message(F.document)
async def handle_document(msg: Message, state: FSMContext):
    doc = msg.document
    await process_incoming_file(
        msg, state,
        file_id=doc.file_id,
        file_name=doc.file_name or "file",
        file_size=doc.file_size or 0,
        mime_type=doc.mime_type or "",
    )


@router.message(F.photo)
async def handle_photo(msg: Message, state: FSMContext):
    photo = msg.photo[-1]  # Highest resolution
    await process_incoming_file(
        msg, state,
        file_id=photo.file_id,
        file_name="photo.jpg",
        file_size=photo.file_size or 0,
        mime_type="image/jpeg",
    )


@router.message(F.video)
async def handle_video(msg: Message, state: FSMContext):
    video = msg.video
    await process_incoming_file(
        msg, state,
        file_id=video.file_id,
        file_name=video.file_name or "video.mp4",
        file_size=video.file_size or 0,
        mime_type=video.mime_type or "video/mp4",
    )


@router.message(F.audio)
async def handle_audio(msg: Message, state: FSMContext):
    audio = msg.audio
    await process_incoming_file(
        msg, state,
        file_id=audio.file_id,
        file_name=audio.file_name or "audio.mp3",
        file_size=audio.file_size or 0,
        mime_type=audio.mime_type or "audio/mpeg",
    )


@router.message(F.voice)
async def handle_voice(msg: Message, state: FSMContext):
    voice = msg.voice
    await process_incoming_file(
        msg, state,
        file_id=voice.file_id,
        file_name="voice.ogg",
        file_size=voice.file_size or 0,
        mime_type="audio/ogg",
    )


@router.message(F.video_note)
async def handle_video_note(msg: Message, state: FSMContext):
    vnote = msg.video_note
    await process_incoming_file(
        msg, state,
        file_id=vnote.file_id,
        file_name="video_note.mp4",
        file_size=vnote.file_size or 0,
        mime_type="video/mp4",
    )
