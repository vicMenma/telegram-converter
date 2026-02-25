"""
Configuration — Pyrogram MTProto client
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram credentials ──────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
API_ID       = int(os.getenv("API_ID", "0"))
API_HASH     = os.getenv("API_HASH", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://your-mini-app.vercel.app")

# ── Admin ─────────────────────────────────────────────────────────
# Your Telegram user ID — only this user can use /queue
# Find your ID by messaging @userinfobot on Telegram
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))

# ── Storage ───────────────────────────────────────────────────────
TEMP_DIR            = os.getenv("TEMP_DIR", "C:/tmp/tgvideobot").replace("\\", "/")
MAX_FILE_SIZE_BYTES = 2 * 1024 ** 3   # 2 GB — Telegram's actual limit

# ── URL download limit ────────────────────────────────────────────
MAX_DOWNLOAD_SIZE_BYTES = 2 * 1024 ** 3   # 2 GB

os.makedirs(TEMP_DIR, exist_ok=True)

# ── Supported formats ─────────────────────────────────────────────
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".txt"}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".flv", ".ts", ".m4v", ".3gp",
}

# ── Resolution presets ────────────────────────────────────────────
RESOLUTIONS: dict[str, tuple[str, str]] = {
    "360p":  ("360p  (640×360)",   "640:360"),
    "480p":  ("480p  (854×480)",   "854:480"),
    "720p":  ("720p  (1280×720)",  "1280:720"),
    "1080p": ("1080p (1920×1080)", "1920:1080"),
    "1440p": ("1440p (2560×1440)", "2560:1440"),
    "4K":    ("4K    (3840×2160)", "3840:2160"),
}

# ── FFmpeg quality ────────────────────────────────────────────────
FFMPEG_VIDEO_CODEC = "libx264"
FFMPEG_AUDIO_CODEC = "aac"
FFMPEG_PRESET      = "ultrafast"  # fastest encoding — good enough for subtitle burn
FFMPEG_CRF         = "23"

# Use all available CPU cores for encoding
import multiprocessing
FFMPEG_THREADS = str(multiprocessing.cpu_count())
