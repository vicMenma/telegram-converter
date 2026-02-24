"""
/start and /help commands — Pyrogram
"""

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)
from client import app
from config import MINI_APP_URL


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Open Video Studio", web_app=WebAppInfo(url=MINI_APP_URL))],
        [
            InlineKeyboardButton("🔤 Burn Subtitles",    callback_data="op:subtitles"),
            InlineKeyboardButton("📐 Change Resolution", callback_data="op:resolution"),
        ],
        [InlineKeyboardButton("❓ How it works", callback_data="menu:help")],
    ])


WELCOME = """
🎬 **Video Studio Bot**

Two powerful tools, zero complexity:

🔤 **Burn Subtitles**
Hardcode .srt / .ass / .vtt into your video permanently.

📐 **Change Resolution**
Convert to 360p · 480p · 720p · 1080p · 1440p · 4K.

**Two ways to send your video:**
📎 Upload directly — up to **2 GB** (no Bot API limit!)
🔗 Send a direct download URL — up to **2 GB**

_Powered by FFmpeg · Files deleted after processing_
"""


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, msg: Message):
    await msg.reply(WELCOME, reply_markup=main_menu_keyboard())


@app.on_message(filters.command("help") & filters.private)
async def cmd_help(client: Client, msg: Message):
    await msg.reply(
        "❓ **How to use**\n\n"

        "**Option A — Upload a file (up to 2 GB):**\n"
        "Send the video file directly in this chat.\n"
        "Pyrogram uses MTProto so there's no 50 MB restriction.\n\n"

        "**Option B — Send a URL:**\n"
        "Paste a direct download link, e.g.:\n"
        "`https://example.com/myvideo.mp4`\n"
        "The bot downloads it server-side.\n\n"

        "**Then:**\n"
        "1. Choose Burn Subtitles or Change Resolution\n"
        "2. Follow the prompts\n"
        "3. Receive your processed video ✅\n\n"

        "**Supported video:** MP4, MKV, AVI, MOV, WEBM, FLV, TS, M4V, 3GP\n"
        "**Supported subtitles:** SRT, ASS, SSA, VTT, SUB"
    )


@app.on_callback_query(filters.regex(r"^menu:help$"))
async def cb_help(client: Client, cb):
    await cb.answer()
    await cb.message.reply(
        "Send me a video file or a direct URL and I'll ask what you want to do!\n\n"
        "Use /help for full instructions."
    )
