"""
/start, /help, /stats commands — Pyrogram
"""

import os
import time
import platform
from utils.queue import JOBS, cancel, get_all, elapsed_str, TYPE_EMOJI
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from client import app
from config import ADMIN_ID

_START_TIME = time.time()


def main_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔤 Burn Subtitles", callback_data="op:subtitles"),
                InlineKeyboardButton(
                    "📐 Change Resolution", callback_data="op:resolution"
                ),
            ],
            [
                InlineKeyboardButton("🔗 Leech a Link", callback_data="menu:leech"),
                InlineKeyboardButton("🧲 Magnet", callback_data="menu:magnet"),
            ],
            [
                InlineKeyboardButton("❓ Help", callback_data="menu:help"),
                InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
            ],
        ]
    )


WELCOME = """
🎬✨ __Welcome to **Video Studio Bot**!__ ✨🎬
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Your all-in-one video tool — right inside Telegram.
No apps. No watermarks. No limits. 🚀

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔤 **BURN SUBTITLES**
> Permanently embed subtitles into your video frames.
> 📄 Supports: `SRT` · `ASS` · `SSA` · `VTT` · `SUB`

📐 **CHANGE RESOLUTION**
> Re-encode to any standard resolution instantly.
> 🖥 `360p` · `480p` · `720p` · `1080p` · `1440p` · `4K`

🔗 **LEECH ANY LINK**
> Download from YouTube, Twitter, Instagram, TikTok & more.
> Choose resolution before downloading! 🎯

🧲 **MAGNET / TORRENT**
> Paste any magnet link — bot downloads and uploads to you.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💪 **WHY USE THIS BOT?**

📁 Up to **2 GB** — zero Telegram restrictions
⚡ Powered by **FFmpeg + yt-dlp** — industry standard
🔒 **Privacy first** — files deleted right after processing
📱 Works on **any device** — phone, tablet, desktop

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👇 **Ready? Send a video, or paste any link below!**
"""

HELP_TEXT = """
❓✨ **HOW TO USE VIDEO STUDIO BOT** ✨❓
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**📤 STEP 1 — Send your video**

📎 **Upload a file** _(up to 2 GB)_
Just drop your video directly in the chat.
Supported: `MP4` `MKV` `AVI` `MOV` `WEBM` `FLV` `TS` `M4V` `3GP`

🔗 **Send a URL** _(up to 2 GB)_
Paste a direct download link:
`https://example.com/video.mp4`
The bot fetches it straight from the source 🌐

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔤 **BURNING SUBTITLES**
1️⃣ Send your video
2️⃣ Tap 🔤 **Burn Subtitles**
3️⃣ Send your subtitle file
4️⃣ Receive your video with permanent subs ✅

📄 Subtitle formats: `SRT` · `ASS` · `SSA` · `VTT` · `SUB`

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📐 **CHANGING RESOLUTION**
1️⃣ Send your video
2️⃣ Tap 📐 **Change Resolution**
3️⃣ Pick your target resolution
4️⃣ Receive your re-encoded video ✅

🖥 Options: `360p` · `480p` · `720p` · `1080p` · `1440p` · `4K`

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 **TIPS**
🔸 Lower resolution = smaller file = faster upload
🔸 Use 720p or 1080p for best quality/size balance
🔸 ASS subtitles preserve custom fonts and styles
🔸 SRT is the safest subtitle format for compatibility
"""


def _get_stats() -> str:
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        ram_used = ram.used / 1024**3
        ram_total = ram.total / 1024**3
        disk_used = disk.used / 1024**3
        disk_total = disk.total / 1024**3
        disk_free = disk.free / 1024**3

        def bar(pct):
            filled = int(pct // 5)
            empty = 20 - filled
            return "█" * filled + "░" * empty

        def cpu_emoji(pct):
            if pct < 40:
                return "🟢"
            if pct < 75:
                return "🟡"
            return "🔴"

        def ram_emoji(pct):
            if pct < 60:
                return "🟢"
            if pct < 85:
                return "🟡"
            return "🔴"

        def disk_emoji(pct):
            if pct < 60:
                return "🟢"
            if pct < 85:
                return "🟠"
            return "🔴"

        uptime_secs = int(time.time() - _START_TIME)
        h, m = divmod(uptime_secs // 60, 60)
        s = uptime_secs % 60
        uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

        return f"""
📊✨ **SERVER STATS** ✨📊
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{cpu_emoji(cpu)} **CPU Usage**
`{bar(cpu)}` **{cpu:.1f}%**

{ram_emoji(ram.percent)} **RAM Usage**
`{bar(ram.percent)}` **{ram.percent:.1f}%**
💾 `{ram_used:.2f} GB / {ram_total:.2f} GB` used

{disk_emoji(disk.percent)} **Disk Usage**
`{bar(disk.percent)}` **{disk.percent:.1f}%**
📁 `{disk_used:.1f} GB` used · `{disk_free:.1f} GB` free · `{disk_total:.1f} GB` total

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱ **Uptime:** `{uptime_str}`
🐍 **Python:** `{platform.python_version()}`
🖥 **OS:** `{platform.system()} {platform.release()}`
"""
    except ImportError:
        uptime_secs = int(time.time() - _START_TIME)
        h, m = divmod(uptime_secs // 60, 60)
        s = uptime_secs % 60
        uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
        return f"""
📊 **SERVER STATS**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱ **Uptime:** `{uptime_str}`
🐍 **Python:** `{platform.python_version()}`
🖥 **OS:** `{platform.system()} {platform.release()}`

⚠️ _Install psutil for full CPU/RAM/disk stats_
"""


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, msg: Message):
    await msg.reply(WELCOME, reply_markup=main_menu_keyboard())


@app.on_message(filters.command("help") & filters.private)
async def cmd_help(client: Client, msg: Message):
    await msg.reply(
        HELP_TEXT,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Back to Menu", callback_data="menu:start")]]
        ),
    )


@app.on_message(filters.command("stats") & filters.private)
async def cmd_stats(client: Client, msg: Message):
    loading = await msg.reply("📊 _Fetching stats…_")
    await loading.edit(
        _get_stats(),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Refresh", callback_data="menu:stats")]]
        ),
    )


@app.on_message(filters.command("queue") & filters.private)
async def cmd_queue(client: Client, msg: Message):
    # Admin only
    if ADMIN_ID and msg.from_user.id != ADMIN_ID:
        await msg.reply("🚫 This command is restricted to the bot admin.")
        return

    jobs = get_all()
    if not jobs:
        await msg.reply(
            "📭 **No active jobs**\n\n" "_Nothing is currently being processed._"
        )
        return
    await msg.reply(_queue_text(jobs), reply_markup=_queue_keyboard(jobs))


@app.on_callback_query(filters.regex(r"^queue:"))
async def queue_callback(client: Client, cb: CallbackQuery):
    # Admin only
    if ADMIN_ID and cb.from_user.id != ADMIN_ID:
        await cb.answer("🚫 Admin only.", show_alert=True)
        return

    parts = cb.data.split(":")
    action = parts[1]

    if action == "refresh":
        jobs = get_all()
        if not jobs:
            await cb.message.edit(
                "📭 **No active jobs**\n\n_Nothing is currently being processed._"
            )
        else:
            await cb.message.edit(_queue_text(jobs), reply_markup=_queue_keyboard(jobs))
        await cb.answer("🔄 Refreshed")

    elif action == "cancel":
        job_id = parts[2]
        ok = cancel(job_id)
        await cb.answer(
            (
                "✅ Job cancelled."
                if ok
                else "⚠️ Job not found — may have already finished."
            ),
            show_alert=True,
        )
        jobs = get_all()
        if not jobs:
            await cb.message.edit(
                "📭 **No active jobs**\n\n_Nothing is currently being processed._"
            )
        else:
            await cb.message.edit(_queue_text(jobs), reply_markup=_queue_keyboard(jobs))

    elif action == "cancelall":
        jobs = get_all()
        count = len(jobs)
        for job in jobs:
            cancel(job["job_id"])
        await cb.answer(f"🛑 Cancelled {count} job(s).", show_alert=True)
        await cb.message.edit("📭 **All jobs cancelled.**")


def _queue_text(jobs: list) -> str:
    lines = ["⚙️ **Active Jobs** — {} running\n".format(len(jobs))]
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    for job in jobs:
        emoji = TYPE_EMOJI.get(job["type"], "⚙️")
        elapsed = elapsed_str(job["started"])
        desc = job["desc"][:45] + "…" if len(job["desc"]) > 45 else job["desc"]
        lines.append(
            f"{emoji} **{job['type'].upper()}** · `{job['job_id']}`\n"
            f"👤 {job['username']}\n"
            f"📄 `{desc}`\n"
            f"📊 {job['status']}\n"
            f"⏱ Running for `{elapsed}`"
        )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _queue_keyboard(jobs: list) -> InlineKeyboardMarkup:
    rows = []
    for job in jobs:
        emoji = TYPE_EMOJI.get(job["type"], "⚙️")
        rows.append(
            [
                InlineKeyboardButton(
                    f"🛑 Stop {emoji} {job['type']} · {job['job_id']}",
                    callback_data=f"queue:cancel:{job['job_id']}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="queue:refresh"),
            InlineKeyboardButton("🛑 Stop All", callback_data="queue:cancelall"),
        ]
    )
    return InlineKeyboardMarkup(rows)


@app.on_callback_query(filters.regex(r"^menu:"))
async def menu_callbacks(client: Client, cb: CallbackQuery):
    action = cb.data.split(":")[1]
    await cb.answer()

    if action == "leech":
        await cb.message.reply(
            "🔗 **Leech a Link**\n\n"
            "Just paste any direct download URL or supported site link:\n\n"
            "• YouTube, Twitter, Instagram, TikTok, Vimeo…\n"
            "• Direct file links (`.mp4`, `.mkv`, `.zip`, etc.)\n\n"
            "I'll detect the type automatically and offer quality options for YouTube! 🎯"
        )
    elif action == "magnet":
        await cb.message.reply(
            "🧲 **Magnet Download**\n\n"
            "Paste a magnet link like:\n"
            "`magnet:?xt=urn:btih:...`\n\n"
            "The bot will connect to peers, download the file and upload it to you directly. 📥"
        )
    elif action == "help":
        await cb.message.reply(
            HELP_TEXT,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Back to Menu", callback_data="menu:start")]]
            ),
        )
    elif action == "stats":
        await cb.message.edit("📊 _Fetching stats…_")
        await cb.message.edit(
            _get_stats(),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Refresh", callback_data="menu:stats")]]
            ),
        )
    elif action == "start":
        await cb.message.reply(WELCOME, reply_markup=main_menu_keyboard())
