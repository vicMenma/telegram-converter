"""
Mini App handler — processes conversions triggered from the Telegram Web App
"""

import json
import logging
from aiogram import Router, F
from aiogram.types import Message

router = Router()
logger = logging.getLogger(__name__)


@router.message(F.web_app_data)
async def handle_miniapp_data(msg: Message):
    """Called when the Mini App sends data back via Telegram.sendData()"""
    try:
        data = json.loads(msg.web_app_data.data)
        action = data.get("action")
        
        if action == "ping":
            await msg.answer("✅ Mini App connected successfully!")
        else:
            await msg.answer(
                f"📱 <b>Mini App request received</b>\n"
                f"Action: <code>{action}</code>\n"
                f"Use the in-app upload flow for conversions."
            )
    except Exception as e:
        logger.error(f"Mini app data error: {e}")
        await msg.answer("⚠️ Received data from Mini App but couldn't parse it.")
