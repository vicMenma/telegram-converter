"""
/start, /help, /stats, /queue commands
Beautiful redesigned UI with consistent visual identity.
"""

import os
import time
import platform
from utils.queue import JOBS, cancel, get_all, elapsed_str, TYPE_EMOJI
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from client import app

_START_TIME = time.time()
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0")) or None


# ── Keyboards ─────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✦ Burn Subtitles",     callback_data="op:subtitles"),
            InlineKeyboardButton("✦ Change Resolution",  callback_data="op:resolution"),
        ],
        [
            InlineKeyboardButton("⬇︎  Leech a Link",      callback_data="menu:leech"),
            InlineKeyboardButton("🧲  Magnet / Torrent",  callback_data="menu:magnet"),
        ],
        [
            InlineKeyboardButton("📖  How to Use",        callback_data="menu:help"),
            InlineKeyboardButton("📡  Server Stats",      callback_data="menu:stats"),
        ],
    ])


# ── Welcome message ───────────────────────────────────────────────

WELCOME = """╔══════════════════════════════╗
    🎬  **VIDEO STUDIO BOT**
╚══════════════════════════════╝

_Your personal video lab — inside Telegram._
_No apps. No watermarks. No file size drama._

──────────────────────────────

**✦ BURN SUBTITLES**
Permanently embed subs into your video.
Supports `SRT` · `ASS` · `SSA` · `VTT` · `SUB` · `TXT`

**✦ CHANGE RESOLUTION**
Re-encode to any resolution in seconds.
`360p` · `480p` · `720p` · `1080p` · `1440p` · `4K`

**⬇︎  LEECH A LINK**
YouTube, Twitter, TikTok, Instagram & 1000+ sites.
Pick your quality before downloading.

**🧲 MAGNET / TORRENT**
Drop any magnet link — bot handles the rest.

──────────────────────────────

📁 Up to **2 GB** per file
⚡ Powered by **FFmpeg** + **yt-dlp**
🔒 Files auto-deleted after processing

👇 _Send a video or paste any link to begin_"""


# ── Help message ──────────────────────────────────────────────────

HELP_TEXT = """╔══════════════════════════════╗
    📖  **HOW TO USE**
╚══════════════════════════════╝

──────────────────────────────
**STEP 1 — Provide your video**

📎 **Upload a file** _(up to 2 GB)_
Drop any video directly in the chat.
`MP4` `MKV` `AVI` `MOV` `WEBM` `FLV` `TS` `3GP`

🔗 **Paste a URL**
Direct link or supported site:
`https://example.com/video.mp4`

🧲 **Magnet link**
`magnet:?xt=urn:btih:…`

──────────────────────────────
**STEP 2 — Choose an operation**

**✦ Burn Subtitles**
① Send video → tap **Burn Subtitles**
② Send subtitle file or paste a URL
③ Receive your video with permanent subs ✅

Formats: `SRT` · `ASS` · `SSA` · `VTT` · `SUB` · `TXT`

**✦ Change Resolution**
① Send video → tap **Change Resolution**
② Pick your target resolution
③ Receive re-encoded video ✅

Options: `360p` · `480p` · `720p` · `1080p` · `1440p` · `4K`

──────────────────────────────
**💡 TIPS**

› 720p is the sweet spot for quality vs size
› ASS preserves custom subtitle styles & fonts
› SRT is the most compatible subtitle format
› Lower resolution = faster processing"""


# ── Stats ─────────────────────────────────────────────────────────

def _get_stats() -> str:
    uptime_secs = int(time.time() - _START_TIME)
    h, m = divmod(uptime_secs // 60, 60)
    s    = uptime_secs % 60
    uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=1)
        ram  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        ram_used   = ram.used  / 1024**3
        ram_total  = ram.total / 1024**3
        disk_used  = disk.used  / 1024**3
        disk_free  = disk.free  / 1024**3
        disk_total = disk.total / 1024**3

        def bar(pct):
            filled = int(pct // 5)
            return "█" * filled + "░" * (20 - filled)

        def dot(pct, thresholds=(50, 80)):
            if pct < thresholds[0]: return "🟢"
            if pct < thresholds[1]: return "🟡"
            return "🔴"

        return f"""╔══════════════════════════════╗
    📡  **SERVER STATS**
╚══════════════════════════════╝

{dot(cpu)} **CPU**
`{bar(cpu)}` {cpu:.1f}%

{dot(ram.percent)} **RAM**
`{bar(ram.percent)}` {ram.percent:.1f}%
_{ram_used:.2f} GB used of {ram_total:.2f} GB_

{dot(disk.percent)} **Disk**
`{bar(disk.percent)}` {disk.percent:.1f}%
_{disk_used:.1f} GB used · {disk_free:.1f} GB free_

──────────────────────────────
⏱ **Uptime** `{uptime_str}`
🐍 **Python** `{platform.python_version()}`
🖥 **OS** `{platform.system()} {platform.release()}`"""

    except ImportError:
        return f"""╔══════════════════════════════╗
    📡  **SERVER STATS**
╚══════════════════════════════╝

⏱ **Uptime** `{uptime_str}`
🐍 **Python** `{platform.python_version()}`
🖥 **OS** `{platform.system()} {platform.release()}`

_⚠️ Install psutil for full stats_"""


# ── Queue helpers ─────────────────────────────────────────────────

def _queue_text(jobs: list) -> str:
    lines = [
        f"╔══════════════════════════════╗\n"
        f"    ⚙️  **ACTIVE JOBS** — {len(jobs)} running\n"
        f"╚══════════════════════════════╝\n"
    ]
    for job in jobs:
        emoji   = TYPE_EMOJI.get(job["type"], "⚙️")
        elapsed = elapsed_str(job["started"])
        desc    = job["desc"][:40] + "…" if len(job["desc"]) > 40 else job["desc"]
        lines.append(
            f"{emoji} **{job['type'].upper()}**\n"
            f"👤 {job['username']}\n"
            f"📄 `{desc}`\n"
            f"📊 _{job['status']}_\n"
            f"⏱ `{elapsed}` elapsed\n"
            f"──────────────────────────────"
        )
    return "\n".join(lines)


def _queue_keyboard(jobs: list) -> InlineKeyboardMarkup:
    rows = []
    for job in jobs:
        emoji = TYPE_EMOJI.get(job["type"], "⚙️")
        rows.append([InlineKeyboardButton(
            f"🛑 Cancel — {emoji} {job['type']} · {job['job_id']}",
            callback_data=f"queue:cancel:{job['job_id']}"
        )])
    rows.append([
        InlineKeyboardButton("🔄 Refresh",   callback_data="queue:refresh"),
        InlineKeyboardButton("🛑 Cancel All", callback_data="queue:cancelall"),
    ])
    return InlineKeyboardMarkup(rows)


# ── Command handlers ──────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, msg: Message):
    await msg.reply(WELCOME, reply_markup=main_menu_keyboard())


@app.on_message(filters.command("help") & filters.private)
async def cmd_help(client: Client, msg: Message):
    await msg.reply(HELP_TEXT, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("‹ Back to Menu", callback_data="menu:start")]
    ]))


@app.on_message(filters.command("stats") & filters.private)
async def cmd_stats(client: Client, msg: Message):
    loading = await msg.reply("_Fetching stats…_")
    await loading.edit(_get_stats(), reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="menu:stats")]
    ]))


@app.on_message(filters.command("queue") & filters.private)
async def cmd_queue(client: Client, msg: Message):
    if ADMIN_ID and msg.from_user.id != ADMIN_ID:
        await msg.reply("🚫 This command is restricted to the bot admin.")
        return
    jobs = get_all()
    if not jobs:
        await msg.reply(
            "╔══════════════════════════════╗\n"
            "    ⚙️  **ACTIVE JOBS**\n"
            "╚══════════════════════════════╝\n\n"
            "_No jobs running right now._"
        )
        return
    await msg.reply(_queue_text(jobs), reply_markup=_queue_keyboard(jobs))


# ── Callback: queue actions ───────────────────────────────────────

@app.on_callback_query(filters.regex(r"^queue:"))
async def queue_callback(client: Client, cb: CallbackQuery):
    if ADMIN_ID and cb.from_user.id != ADMIN_ID:
        await cb.answer("🚫 Admin only.", show_alert=True)
        return

    parts  = cb.data.split(":")
    action = parts[1]

    if action == "refresh":
        jobs = get_all()
        if not jobs:
            await cb.message.edit(
                "╔══════════════════════════════╗\n"
                "    ⚙️  **ACTIVE JOBS**\n"
                "╚══════════════════════════════╝\n\n"
                "_No jobs running right now._"
            )
        else:
            await cb.message.edit(_queue_text(jobs), reply_markup=_queue_keyboard(jobs))
        await cb.answer("🔄 Refreshed")

    elif action == "cancel":
        job_id = parts[2]
        ok     = cancel(job_id)
        await cb.answer(
            "✅ Job cancelled." if ok else "⚠️ Job not found — may have already finished.",
            show_alert=True
        )
        jobs = get_all()
        if not jobs:
            await cb.message.edit(
                "╔══════════════════════════════╗\n"
                "    ⚙️  **ACTIVE JOBS**\n"
                "╚══════════════════════════════╝\n\n"
                "_No jobs running right now._"
            )
        else:
            await cb.message.edit(_queue_text(jobs), reply_markup=_queue_keyboard(jobs))

    elif action == "cancelall":
        jobs  = get_all()
        count = len(jobs)
        for job in jobs:
            cancel(job["job_id"])
        await cb.answer(f"🛑 Cancelled {count} job(s).", show_alert=True)
        await cb.message.edit("_All jobs cancelled._")


# ── Callback: menu navigation ─────────────────────────────────────

@app.on_callback_query(filters.regex(r"^menu:"))
async def menu_callbacks(client: Client, cb: CallbackQuery):
    action = cb.data.split(":")[1]
    await cb.answer()

    if action == "leech":
        await cb.message.reply(
            "╔══════════════════════════════╗\n"
            "    ⬇︎  **LEECH A LINK**\n"
            "╚══════════════════════════════╝\n\n"
            "Paste any link and I'll download it for you.\n\n"
            "**Supported sources**\n"
            "› YouTube · Twitter · Instagram\n"
            "› TikTok · Vimeo · Facebook\n"
            "› Direct file links `.mp4` `.mkv` `.zip`…\n"
            "› 1000+ more sites via yt-dlp\n\n"
            "_Quality selector shown automatically for YouTube_ 🎯"
        )
    elif action == "magnet":
        await cb.message.reply(
            "╔══════════════════════════════╗\n"
            "    🧲  **MAGNET / TORRENT**\n"
            "╚══════════════════════════════╝\n\n"
            "Paste a magnet link or upload a `.torrent` file.\n\n"
            "**Example**\n"
            "`magnet:?xt=urn:btih:…`\n\n"
            "_Bot connects to peers, downloads and uploads directly to you_ 📥"
        )
    elif action == "help":
        await cb.message.reply(HELP_TEXT, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("‹ Back to Menu", callback_data="menu:start")]
        ]))
    elif action == "stats":
        await cb.message.edit("_Fetching stats…_")
        await cb.message.edit(_get_stats(), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="menu:stats"),
             InlineKeyboardButton("‹ Back",     callback_data="menu:start")]
        ]))
    elif action == "start":
        await cb.message.edit(WELCOME, reply_markup=main_menu_keyboard())
