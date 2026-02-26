"""
Leech Processor
───────────────
Handles three kinds of links:

  1. yt-dlp links  — YouTube, Twitter, Instagram, TikTok, etc.
                     Offers resolution picker before download.

  2. Direct links  — any http/https URL pointing to a file.
                     Streams straight to disk.

  3. Magnet links  — torrent magnet URIs.
                     Downloads via libtorrent.
"""

import os
import re
import asyncio
import logging
import time
from pathlib import Path

from config import TEMP_DIR, MAX_DOWNLOAD_SIZE_BYTES
from utils.file_utils import format_size

logger = logging.getLogger(__name__)

# ── Link type detection ────────────────────────────────────────────

MAGNET_RE  = re.compile(r"^magnet:\?", re.IGNORECASE)
YTDLP_DOMAINS = {
    "youtube.com", "youtu.be",
    "twitter.com", "x.com",
    "instagram.com", "tiktok.com",
    "facebook.com", "fb.watch",
    "twitch.tv", "vimeo.com",
    "dailymotion.com", "reddit.com",
    "streamable.com", "bilibili.com",
    "nicovideo.jp", "rumble.com",
    "odysee.com", "ok.ru",
}

def detect_link_type(url: str) -> str:
    """Return 'magnet', 'ytdlp', or 'direct'."""
    if MAGNET_RE.match(url):
        return "magnet"

    # m3u8 / HLS streams — always use yt-dlp to merge segments
    if ".m3u8" in url.lower():
        return "ytdlp"

    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().lstrip("www.")
        if any(host == d or host.endswith("." + d) for d in YTDLP_DOMAINS):
            return "ytdlp"
    except Exception:
        pass
    return "direct"


# ── yt-dlp: fetch available formats ───────────────────────────────

async def get_formats(url: str) -> list[dict]:
    """
    Return a list of available video formats from yt-dlp.
    Each dict: { label, format_id, ext, filesize, height }
    """
    import yt_dlp

    formats = []
    seen    = set()

    def _extract():
        ydl_opts = {
            "quiet":            True,
            "no_warnings":      True,
            "skip_download":    True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _extract)

    title = info.get("title", "video")

    for f in info.get("formats", []):
        height    = f.get("height")
        ext       = f.get("ext", "mp4")
        fmt_id    = f.get("format_id", "")
        filesize  = f.get("filesize") or f.get("filesize_approx") or 0
        vcodec    = f.get("vcodec", "none")

        # Skip audio-only
        if not height or vcodec == "none":
            continue

        key = height
        if key in seen:
            continue
        seen.add(key)

        label = f"{height}p"
        if filesize:
            label += f"  (~{format_size(filesize)})"

        formats.append({
            "label":     label,
            "format_id": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
            "height":    height,
            "ext":       ext,
            "filesize":  filesize,
        })

    # Sort highest → lowest resolution
    formats.sort(key=lambda x: x["height"], reverse=True)

    # Add "Best quality" option at top
    formats.insert(0, {
        "label":     "⭐ Best quality",
        "format_id": "bestvideo+bestaudio/best",
        "height":    9999,
        "ext":       "mp4",
        "filesize":  0,
    })

    return formats, title


# ── yt-dlp: download chosen format ────────────────────────────────

async def ytdlp_download(url: str, format_id: str, job_id: str, progress_msg=None) -> str:
    """
    Download a video using yt-dlp with the chosen format.
    Returns the local file path.
    """
    import yt_dlp

    output_template = os.path.join(TEMP_DIR, f"{job_id}_ytdlp.%(ext)s")
    last_update     = [0.0]
    final_path      = [None]

    # Capture loop before entering thread executor
    loop = asyncio.get_event_loop()

    def progress_hook(d):
        if d["status"] == "finished":
            final_path[0] = d.get("filename") or d.get("info_dict", {}).get("filepath")
            return

        if d["status"] != "downloading":
            return

        now = time.time()
        if now - last_update[0] < 3:
            return
        last_update[0] = now

        if not progress_msg:
            return

        downloaded = d.get("downloaded_bytes", 0)
        total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        speed      = d.get("speed") or 0
        eta        = d.get("eta") or 0
        speed_str  = f"{format_size(int(speed))}/s" if speed else "…"
        eta_str    = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"

        if total > 0:
            pct    = min(int(downloaded * 100 / total), 99)
            filled = pct // 5
            bar    = "█" * filled + "░" * (20 - filled)
            text   = (
                f"📥 <i>Downloading…</i> <b>{pct}%</b>\n"
                f"<code>{bar}</code>\n"
                f"📦 {format_size(downloaded)} / {format_size(total)}\n"
                f"🚀 {speed_str}  ·  ⏱ {eta_str}"
            )
        else:
            text = (
                f"📥 <i>Downloading…</i>\n"
                f"📦 {format_size(downloaded)}  ·  🚀 {speed_str}"
            )

        asyncio.run_coroutine_threadsafe(_safe_edit(progress_msg, text), loop)

    ydl_opts = {
        "format":              format_id,
        "outtmpl":             output_template,
        "quiet":               True,
        "no_warnings":         True,
        "progress_hooks":      [progress_hook],
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 16,  # parallel HLS/DASH fragments
        "http_chunk_size":     10 * 1024 * 1024,  # 10MB chunks
        "buffersize":          1024 * 1024,        # 1MB buffer
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

    # aria2c not available on Railway — using built-in parallel fragments

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    await loop.run_in_executor(None, _run)

    # Find the downloaded file
    if final_path[0] and os.path.exists(final_path[0]):
        return final_path[0]

    # Fallback: find newest file in TEMP_DIR matching job_id
    matches = [
        os.path.join(TEMP_DIR, f)
        for f in os.listdir(TEMP_DIR)
        if f.startswith(f"{job_id}_ytdlp")
    ]
    if matches:
        return max(matches, key=os.path.getmtime)

    raise RuntimeError("yt-dlp finished but output file not found.")


async def _safe_edit(msg, text):
    try:
        await msg.edit(text)
    except Exception:
        pass


# ── Direct URL download ────────────────────────────────────────────

async def _get_file_info(url: str) -> tuple[int, str]:
    """HEAD request to get file size and extension."""
    import aiohttp
    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": "Mozilla/5.0"}) as session:
        async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            ct    = resp.headers.get("Content-Type", "")
            ext   = Path(url.split("?")[0]).suffix.lower()
            if not ext:
                ct_map = {
                    "video/mp4": ".mp4", "video/x-matroska": ".mkv",
                    "video/webm": ".webm", "application/zip": ".zip",
                    "application/x-rar": ".rar", "application/pdf": ".pdf",
                }
                ext = ct_map.get(ct.split(";")[0].strip(), ".bin")
            return total, ext


async def _aria2c_download(url: str, dest: str, progress_msg=None, total: int = 0) -> None:
    """Download using aria2c with 16 parallel connections."""
    import shutil as _shutil
    aria2c = _shutil.which("aria2c")
    if not aria2c:
        raise RuntimeError("aria2c not found")

    dest_dir  = os.path.dirname(dest)
    dest_file = os.path.basename(dest)

    cmd = [
        aria2c,
        "--max-connection-per-server=16",
        "--min-split-size=1M",
        "--split=16",
        "--max-concurrent-downloads=16",
        "--file-allocation=none",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--dir", dest_dir,
        "--out", dest_file,
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    start_time  = time.time()
    last_update = 0.0

    async def _track():
        nonlocal last_update
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            now = time.time()
            if progress_msg and (now - last_update) >= 3:
                last_update = now
                downloaded = os.path.getsize(dest) if os.path.exists(dest) else 0
                elapsed    = max(now - start_time, 0.1)
                speed      = downloaded / elapsed
                speed_str  = f"{format_size(int(speed))}/s"
                if total > 0:
                    pct    = min(int(downloaded * 100 / total), 99)
                    filled = pct // 5
                    bar    = "█" * filled + "░" * (20 - filled)
                    remain = total - downloaded
                    eta    = int(remain / speed) if speed > 0 else 0
                    eta_str = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"
                    text   = (
                        f"🌐 <i>Downloading…</i> <b>{pct}%</b>\n"
                        f"<code>{bar}</code>\n"
                        f"📦 {format_size(downloaded)} / {format_size(total)}\n"
                        f"🚀 {speed_str}  ·  ⏱ {eta_str}"
                    )
                else:
                    text = f"🌐 <i>Downloading…</i>\n📦 {format_size(downloaded)}  ·  🚀 {speed_str}"
                try:
                    await progress_msg.edit(text)
                except Exception:
                    pass

    await asyncio.gather(_track(), proc.wait())
    if proc.returncode != 0:
        raise RuntimeError("aria2c download failed")


async def direct_download(url: str, job_id: str, progress_msg=None) -> str:
    """
    Download any direct file URL with parallel chunks via aria2c.
    Falls back to aiohttp if aria2c is not available.
    Returns local file path.
    """
    import aiohttp
    import aiofiles
    import shutil as _shutil

    total, ext = await _get_file_info(url)

    if total > MAX_DOWNLOAD_SIZE_BYTES:
        raise RuntimeError(f"File too large ({total / 1024**2:.0f} MB). Max is 2 GB.")

    dest = os.path.join(TEMP_DIR, f"{job_id}_direct{ext}")

    # Use aria2c if available — 16 parallel connections
    if _shutil.which("aria2c"):
        logger.info("Using aria2c for direct download")
        await _aria2c_download(url, dest, progress_msg=progress_msg, total=total)
        return dest

    # Parallel chunk download — splits file into N chunks downloaded simultaneously
    logger.info(f"Using parallel aiohttp for direct download ({total // 1024**2} MB)")
    CHUNK_COUNT   = 16           # parallel connections
    CHUNK_SIZE    = max(total // CHUNK_COUNT, 4 * 1024 * 1024) if total > 0 else 8 * 1024 * 1024

    connector  = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300, force_close=False)
    headers    = {"User-Agent": "Mozilla/5.0"}
    downloaded = 0
    start_time = time.time()
    last_update = [0.0]

    async def _update_progress():
        now = time.time()
        if not progress_msg or now - last_update[0] < 3:
            return
        last_update[0] = now
        elapsed   = max(now - start_time, 0.1)
        speed     = downloaded / elapsed
        speed_str = f"{format_size(int(speed))}/s"
        if total > 0:
            pct     = min(int(downloaded * 100 / total), 99)
            filled  = pct // 5
            bar     = "█" * filled + "░" * (20 - filled)
            remain  = total - downloaded
            eta     = int(remain / speed) if speed > 0 else 0
            eta_str = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"
            text    = (
                f"🌐 <i>Downloading…</i> <b>{pct}%</b>\n"
                f"<code>{bar}</code>\n"
                f"📦 {format_size(downloaded)} / {format_size(total)}\n"
                f"🚀 {speed_str}  ·  ⏱ {eta_str}"
            )
        else:
            text = f"🌐 <i>Downloading…</i>\n📦 {format_size(downloaded)}  ·  🚀 {speed_str}"
        try:
            await progress_msg.edit(text)
        except Exception:
            pass

    # Pre-allocate file
    async with aiofiles.open(dest, "wb") as fh:
        if total > 0:
            await fh.seek(total - 1)
            await fh.write(b"\0")

    async def _download_chunk(session, start: int, end: int):
        nonlocal downloaded
        chunk_headers = dict(headers)
        if total > 0:
            chunk_headers["Range"] = f"bytes={start}-{end}"
        async with session.get(
            url,
            headers=chunk_headers,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=3600, connect=30),
        ) as resp:
            if resp.status not in (200, 206):
                raise RuntimeError(f"Server returned HTTP {resp.status}")
            async with aiofiles.open(dest, "r+b") as fh:
                await fh.seek(start)
                async for data in resp.content.iter_chunked(1024 * 1024):
                    await fh.write(data)
                    downloaded += len(data)
                    await _update_progress()

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        if total > 0:
            # Split into chunks and download in parallel
            ranges  = [(i * CHUNK_SIZE, min((i + 1) * CHUNK_SIZE - 1, total - 1))
                       for i in range((total + CHUNK_SIZE - 1) // CHUNK_SIZE)]
            tasks   = [_download_chunk(session, s, e) for s, e in ranges]
            # Run in batches of CHUNK_COUNT to avoid overwhelming
            for i in range(0, len(tasks), CHUNK_COUNT):
                await asyncio.gather(*tasks[i:i + CHUNK_COUNT])
        else:
            # Unknown size — single stream
            await _download_chunk(session, 0, 0)

    return dest


# ── Magnet / torrent download ──────────────────────────────────────

async def magnet_download(source: str, job_id: str, progress_msg=None) -> str:
    """
    Download a magnet link OR a .torrent file path using libtorrent.
    Returns path to the downloaded file (largest file in torrent).
    """
    try:
        import libtorrent as lt
    except ImportError:
        raise RuntimeError(
            "❌ Magnet downloads require <b>libtorrent</b>.\n"
            "It is not installed on this server.\n\n"
            "Ask the bot admin to add <code>python-libtorrent</code> to the Dockerfile."
        )

    dest_dir = os.path.join(TEMP_DIR, f"{job_id}_torrent")
    os.makedirs(dest_dir, exist_ok=True)

    ses = lt.session()

    # Aggressive performance settings
    settings = {
        "active_downloads":              10,
        "active_seeds":                  5,
        "active_limit":                  15,
        "num_want":                      200,      # request more peers
        "connections_limit":             500,
        "upload_rate_limit":             0,        # unlimited upload
        "download_rate_limit":           0,        # unlimited download
        "unchoke_slots_limit":           8,
        "connection_speed":              500,      # connect to peers faster
        "peer_connect_timeout":          3,
        "request_timeout":               10,
        "max_out_request_queue":         1500,
        "whole_pieces_threshold":        20,
        "send_buffer_watermark":         3 * 1024 * 1024,
        "send_buffer_low_watermark":     512 * 1024,
        "recv_socket_buffer_size":       1024 * 1024,
        "send_socket_buffer_size":       1024 * 1024,
        "max_peer_recv_buffer_size":     5 * 1024 * 1024,
        "enable_dht":                    True,
        "enable_lsd":                    True,
        "enable_upnp":                   True,
        "enable_natpmp":                 True,
        "announce_to_all_tiers":         True,
        "announce_to_all_trackers":      True,
    }
    ses.apply_settings(settings)

    # Accept both magnet URIs and .torrent file paths
    if source.lower().startswith("magnet:"):
        params = lt.parse_magnet_uri(source)
        params.save_path = dest_dir
        handle = ses.add_torrent(params)
    else:
        # .torrent file path
        info   = lt.torrent_info(source)
        params = {"ti": info, "save_path": dest_dir}
        handle = ses.add_torrent(params)

    last_update = 0.0

    # Wait for metadata + download
    while not handle.is_seed():
        await asyncio.sleep(2)
        s   = handle.status()
        now = time.time()

        if progress_msg and (now - last_update) >= 3:
            last_update = now
            pct         = int(s.progress * 100)
            filled      = pct // 5
            bar         = "█" * filled + "░" * (20 - filled)
            speed       = s.download_rate
            speed_str   = f"{format_size(int(speed))}/s"
            peers       = s.num_peers
            state_map   = {
                lt.torrent_status.checking_files:         "🔍 Checking files",
                lt.torrent_status.downloading_metadata:   "🔎 Getting metadata",
                lt.torrent_status.downloading:            "📥 Downloading",
                lt.torrent_status.finished:               "✅ Finishing",
                lt.torrent_status.seeding:                "🌱 Seeding",
            }
            state_str = state_map.get(s.state, "⏳ Working")

            text = (
                f"🧲 <i>Downloading…</i> <b>{pct}%</b>\n"
                f"<code>{bar}</code>\n"
                f"{state_str}  ·  👥 {peers} peers\n"
                f"🚀 {speed_str}"
            )
            try:
                await progress_msg.edit(text)
            except Exception:
                pass

    # Find largest file (the main video/file)
    largest = max(
        (os.path.join(root, f) for root, _, files in os.walk(dest_dir) for f in files),
        key=os.path.getsize,
        default=None,
    )

    if not largest:
        raise RuntimeError("Torrent finished but no files found.")

    return largest
