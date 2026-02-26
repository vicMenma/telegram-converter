"""
Pyrogram client instance — imported by both main.py and handlers.
Using pyrofork — a faster fork of pyrogram with optimized MTProto.
"""

from pyrogram import Client, enums
from config import BOT_TOKEN, API_ID, API_HASH

app = Client(
    name="video_studio_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=32,                        # more async workers for concurrent ops
    max_concurrent_transmissions=20,   # more parallel upload/download streams
    parse_mode=enums.ParseMode.HTML,
)
