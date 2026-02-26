"""
Leech Processor
───────────────
Handles three kinds of links:

  1. yt-dlp links  — YouTube, Twitter, Instagram, TikTok, etc.
  2. Direct links  — any http/https URL pointing to a file.
  3. Magnet links  — torrent magnet URIs via libtorrent.
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

# ── Browser headers ────────────────────────────────────────────────

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}

# ── Link type detection ────────────────────────────────────────────

MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)

YTDLP_DOMAINS = {
    "youtube.com", "youtu.be", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "facebook.com", "fb.watch",
    "twitch.tv", "vimeo.com", "dailymotion.com", "reddit.com",
    "streamable.com", "bilibili.com", "nicovideo.jp",
    "rumble.com", "odysee.com", "ok.ru",
}

# Services that require login — can't be downloaded directly
_BLOCKED_DOMAINS = {
    "seedr.cc", "alldebrid.com", "real-debrid.com",
    "debrid-link.fr", "premiumize.me", "1fichier.com",
    "mega.nz", "mediafire.com", "rapidgator.net",
}


def detect_link_type(url: str) -> str:
    """Return 'magnet', 'ytdlp', 'direct', or 'blocked'."""
    if MAGNET_RE.match(url):
        return "magnet"
    if ".m3u8" in url.lower():
        return "ytdlp"
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("rd.")
        if any(host == d or host.endswith("." + d) for d in YTDLP_DOMAINS):
            return "ytdlp"
        if any(host == d or host.endswith("." + d) for d in _BLOCKED_DOMAINS):
            return "blocked"
    except Exception:
        pass
    return "direct"


# ── yt-dlp ────────────────────────────────────────────────────────

async def get_formats(url: str):
    import yt_dlp
    formats = []
    seen = set()

    def _extract():
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False)

    info = await asyncio.get_event_loop().run_in_executor(None, _extract)
    title = info.get("title", "video")

    for f in info.get("formats", []):
        height = f.get("height")
        if not height or f.get("vcodec", "none") == "none":
            continue
        if height in seen:
            continue
        seen.add(height)
        filesize = f.get("filesize") or f.get("filesize_approx") or 0
        label = f"{height}p" + (f"  (~{format_size(filesize)})" if filesize else "")
        formats.append({
            "label":     label,
            "format_id": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
            "height":    height,
            "filesize":  filesize,
        })

    formats.sort(key=lambda x: x["height"], reverse=True)
    formats.insert(0, {
        "label": "⭐ Best quality",
        "format_id": "bestvideo+bestaudio/best",
        "height": 9999, "filesize": 0,
    })
    return formats, title


async def ytdlp_download(url: str, format_id: str, job_id: str, progress_msg=None) -> str:
    import yt_dlp
    output_template = os.path.join(TEMP_DIR, f"{job_id}_ytdlp.%(ext)s")
    last_update = [0.0]
    final_path  = [None]
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
            bar    = "█" * (pct // 5) + "░" * (20 - pct // 5)
            text   = (
                f"📥 <i>Downloading…</i> <b>{pct}%</b>\n"
                f"<code>{bar}</code>\n"
                f"📦 {format_size(downloaded)} / {format_size(total)}\n"
                f"🚀 {speed_str}  ·  ⏱ {eta_str}"
            )
        else:
            text = f"📥 <i>Downloading…</i>\n📦 {format_size(downloaded)}  ·  🚀 {speed_str}"
        asyncio.run_coroutine_threadsafe(_safe_edit(progress_msg, text), loop)

    def _run():
        with yt_dlp.YoutubeDL({
            "format": format_id,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 16,
            "http_chunk_size": 10 * 1024 * 1024,
            "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        }) as ydl:
            ydl.download([url])

    await loop.run_in_executor(None, _run)

    if final_path[0] and os.path.exists(final_path[0]):
        return final_path[0]
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
    """Probe URL for file size and extension. Never raises."""
    import aiohttp

    ct_map = {
        "video/mp4": ".mp4", "video/x-matroska": ".mkv",
        "video/x-msvideo": ".avi", "video/webm": ".webm",
        "video/quicktime": ".mov",
    }

    # Guess ext from URL
    url_path = url.split("?")[0].rstrip("/")
    ext = Path(url_path).suffix.lower()
    if not ext or len(ext) > 5:
        ext = ".mkv"

    def _parse(headers) -> tuple[int, str]:
        total = int(headers.get("Content-Length", 0) or 0)
        ct    = headers.get("Content-Type", "").split(";")[0].strip()
        return total, ct_map.get(ct, ext)

    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    timeout   = aiohttp.ClientTimeout(total=15, connect=10)

    try:
        async with aiohttp.ClientSession(connector=connector, headers=_BROWSER_HEADERS) as session:
            # Try HEAD
            try:
                async with session.head(url, allow_redirects=True, timeout=timeout) as r:
                    if r.status < 400:
                        total, fext = _parse(r.headers)
                        if total > 0:
                            return total, fext
            except Exception:
                pass

            # Try Range GET
            try:
                rh = {**_BROWSER_HEADERS, "Range": "bytes=0-0"}
                async with session.get(url, headers=rh, allow_redirects=True, timeout=timeout) as r:
                    if r.status in (200, 206):
                        cr = r.headers.get("Content-Range", "")
                        total = int(cr.split("/")[1]) if "/" in cr else int(r.headers.get("Content-Length", 0) or 0)
                        _, fext = _parse(r.headers)
                        return total, fext
            except Exception:
                pass
    except Exception:
        pass

    return 0, ext


async def direct_download(url: str, job_id: str, progress_msg=None) -> str:
    """
    Download a direct file URL.
    Tries parallel chunks first; falls back to single stream if server
    rejects range requests.
    """
    import aiohttp
    import aiofiles

    total, ext = await _get_file_info(url)
    if total > MAX_DOWNLOAD_SIZE_BYTES:
        raise RuntimeError(f"File too large ({total / 1024**2:.0f} MB). Max 2 GB.")

    dest        = os.path.join(TEMP_DIR, f"{job_id}_direct{ext}")
    start_time  = time.time()
    last_update = [0.0]
    downloaded  = 0

    async def _progress():
        now = time.time()
        if not progress_msg or now - last_update[0] < 3:
            return
        last_update[0] = now
        elapsed   = max(now - start_time, 0.1)
        speed     = downloaded / elapsed
        speed_str = f"{format_size(int(speed))}/s"
        if total > 0:
            pct     = min(int(downloaded * 100 / total), 99)
            bar     = "█" * (pct // 5) + "░" * (20 - pct // 5)
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

    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)

    async def _single(session):
        nonlocal downloaded
        async with session.get(
            url, headers=_BROWSER_HEADERS, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=3600, connect=30),
        ) as resp:
            if resp.status not in (200, 206):
                raise RuntimeError(f"Server returned HTTP {resp.status}")
            async with aiofiles.open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(2 * 1024 * 1024):
                    await fh.write(chunk)
                    downloaded += len(chunk)
                    await _progress()

    async def _parallel(session, n: int = 8):
        nonlocal downloaded
        chunk_size = max(total // n, 4 * 1024 * 1024)
        # Pre-allocate
        async with aiofiles.open(dest, "wb") as fh:
            await fh.seek(total - 1)
            await fh.write(b"\0")

        async def _chunk(start, end):
            nonlocal downloaded
            rh = {**_BROWSER_HEADERS, "Range": f"bytes={start}-{end}"}
            async with session.get(
                url, headers=rh, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=3600, connect=30),
            ) as resp:
                if resp.status == 206:
                    async with aiofiles.open(dest, "r+b") as fh:
                        await fh.seek(start)
                        async for data in resp.content.iter_chunked(1024 * 1024):
                            await fh.write(data)
                            downloaded += len(data)
                            await _progress()
                elif resp.status == 200:
                    raise ValueError("server_no_range")
                else:
                    raise RuntimeError(f"HTTP {resp.status}")

        ranges = [
            (i * chunk_size, min((i + 1) * chunk_size - 1, total - 1))
            for i in range((total + chunk_size - 1) // chunk_size)
        ]
        for i in range(0, len(ranges), n):
            await asyncio.gather(*[_chunk(s, e) for s, e in ranges[i:i + n]])

    async with aiohttp.ClientSession(connector=connector) as session:
        if total > 4 * 1024 * 1024:
            try:
                await _parallel(session)
                return dest
            except (ValueError, RuntimeError) as e:
                logger.warning(f"Parallel failed ({e}), falling back to single stream")
                downloaded = 0
                if os.path.exists(dest):
                    os.remove(dest)
        await _single(session)

    return dest


# ── Magnet / torrent ───────────────────────────────────────────────

async def magnet_download(source: str, job_id: str, progress_msg=None) -> str:
    try:
        import libtorrent as lt
    except ImportError:
        raise RuntimeError(
            "Magnet downloads require <b>libtorrent</b> — not installed on this server."
        )

    dest_dir = os.path.join(TEMP_DIR, f"{job_id}_torrent")
    os.makedirs(dest_dir, exist_ok=True)

    ses = lt.session()
    ses.apply_settings({
        "active_downloads": 10,
        "num_want": 200,
        "connections_limit": 500,
        "upload_rate_limit": 0,
        "download_rate_limit": 0,
        "connection_speed": 500,
        "peer_connect_timeout": 3,
        "request_timeout": 10,
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": True,
        "enable_natpmp": True,
        "announce_to_all_tiers": True,
        "announce_to_all_trackers": True,
    })

    if source.lower().startswith("magnet:"):
        params = lt.parse_magnet_uri(source)
        params.save_path = dest_dir
        handle = ses.add_torrent(params)
    else:
        info   = lt.torrent_info(source)
        handle = ses.add_torrent({"ti": info, "save_path": dest_dir})

    last_update = 0.0
    while not handle.is_seed():
        await asyncio.sleep(2)
        s   = handle.status()
        now = time.time()
        if progress_msg and (now - last_update) >= 3:
            last_update = now
            pct       = int(s.progress * 100)
            bar       = "█" * (pct // 5) + "░" * (20 - pct // 5)
            speed_str = f"{format_size(int(s.download_rate))}/s"
            state_map = {
                lt.torrent_status.downloading_metadata: "🔎 Getting metadata",
                lt.torrent_status.downloading:          "📥 Downloading",
                lt.torrent_status.finished:             "✅ Finishing",
            }
            state_str = state_map.get(s.state, "⏳ Working")
            try:
                await progress_msg.edit(
                    f"🧲 <i>Downloading…</i> <b>{pct}%</b>\n"
                    f"<code>{bar}</code>\n"
                    f"{state_str}  ·  👥 {s.num_peers} peers\n"
                    f"🚀 {speed_str}"
                )
            except Exception:
                pass

    largest = max(
        (os.path.join(r, f) for r, _, files in os.walk(dest_dir) for f in files),
        key=os.path.getsize, default=None,
    )
    if not largest:
        raise RuntimeError("Torrent finished but no files found.")
    return largest
