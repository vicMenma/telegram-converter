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
    workers=16,
    max_concurrent_transmissions=10,
    parse_mode=enums.ParseMode.HTML,
)
