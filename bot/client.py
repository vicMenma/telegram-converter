"""
Pyrogram client instance — imported by both main.py and handlers.
Keeping it in a separate file breaks the circular import.
"""

from pyrogram import Client
from config import BOT_TOKEN, API_ID, API_HASH

app = Client(
    name="video_studio_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)
