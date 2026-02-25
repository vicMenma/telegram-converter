"""
Callback query handler — handles format button presses, pagination, cancellation
and triggers the actual file conversion pipeline.
"""

import os
import logging
import time
from pathlib import Path
from aiogram import Router
from aiogram.types import CallbackQuery, FSInputFile, BufferedInputFile
from aiogram.fsm.context import FSMContext

from config import TEMP_DIR, FORMAT_MAP
from handlers.file_handler import ConversionState, build_format_keyboard
from converters.pipeline import convert_file
from utils.file_utils import format_size, output_filename, cleanup

logger = logging.getLogger(__name__)
router = Router()

os.makedirs(TEMP_DIR, exist_ok=True)


@router.callback_query(lambda c: c.data and c.data.startswith("page:"))
async def handle_pagination(callback: CallbackQuery, state: FSMContext):
    _, category, page = callback.data.split(":")
    await callback.message.edit_reply_markup(
        reply_markup=build_format_keyboard(category, int(page))
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "cancel")
async def handle_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Conversion cancelled.")
    await callback.answer()


@router.callback_query(lambda c: c.data == "help")
async def handle_help_cb(callback: CallbackQuery):
    await callback.answer("Send any file to get started!", show_alert=True)


@router.callback_query(lambda c: c.data == "stats")
async def handle_stats_cb(callback: CallbackQuery):
    await callback.answer("📊 Stats coming soon!", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("fmt:"))
async def handle_format_selection(callback: CallbackQuery, state: FSMContext, bot):
    """User selected an output format — download, convert, send back."""
    target_format = callback.data.split(":", 1)[1]
    
    data = await state.get_data()
    if not data.get("file_id"):
        await callback.answer("⚠️ Session expired. Please send your file again.", show_alert=True)
        await state.clear()
        return
    
    file_id = data["file_id"]
    file_name = data.get("file_name", "file")
    category = data.get("category", "document")
    
    await state.clear()
    
    # Show progress
    progress_msg = await callback.message.edit_text(
        f"⏳ <b>Converting...</b>\n\n"
        f"📁 <code>{file_name}</code>\n"
        f"🎯 Format: <b>{target_format.upper()}</b>\n\n"
        f"<i>This may take a moment for large files...</i>"
    )
    
    input_path = None
    output_path = None
    
    try:
        # ── Step 1: Download file from Telegram ─────────────────
        tg_file = await bot.get_file(file_id)
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
        input_path = os.path.join(TEMP_DIR, f"{file_id}.{ext}")
        
        await bot.download_file(tg_file.file_path, destination=input_path)
        logger.info(f"Downloaded: {input_path}")
        
        # ── Step 2: Convert ──────────────────────────────────────
        start_time = time.time()
        output_path = await convert_file(input_path, target_format, category)
        elapsed = time.time() - start_time
        
        if not output_path or not os.path.exists(output_path):
            raise Exception("Conversion produced no output file.")
        
        output_size = os.path.getsize(output_path)
        size_str     = format_size(output_size)

        # ── Step 3: Send converted file ──────────────────────────
        out_filename = output_filename(file_name, target_format)
        
        await callback.message.answer_document(
            document=FSInputFile(output_path, filename=out_filename),
            caption=(
                f"✅ <b>Conversion complete!</b>\n\n"
                f"📁 <b>File:</b> <code>{out_filename}</code>\n"
                f"📦 <b>Size:</b> {size_str}\n"
                f"⚡ <b>Time:</b> {elapsed:.1f}s"
            )
        )

        await progress_msg.delete()
        logger.info(f"Sent converted file: {out_filename} ({size_str})")
        
    except Exception as e:
        logger.error(f"Conversion error: {e}", exc_info=True)
        await progress_msg.edit_text(
            f"❌ <b>Conversion failed</b>\n\n"
            f"<i>{str(e)[:200]}</i>\n\n"
            f"Try a different format or check that your file is not corrupted."
        )
    
    finally:
        # Cleanup temp files via file_utils
        cleanup(input_path, output_path)
    
    await callback.answer()
