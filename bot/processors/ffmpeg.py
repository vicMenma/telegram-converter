"""
FFmpeg Processor
────────────────
burn_subtitles   — hardcode subtitles into video frames
change_resolution — re-encode to target resolution
download_url      — download video from a URL
"""

import os
import re
import asyncio
import shutil
import logging
import aiohttp
import aiofiles
from pathlib import Path
from config import (
    TEMP_DIR,
    FFMPEG_VIDEO_CODEC, FFMPEG_AUDIO_CODEC,
    FFMPEG_PRESET, FFMPEG_CRF,
    MAX_DOWNLOAD_SIZE_BYTES,
)

logger = logging.getLogger(__name__)

VIDEO_URL_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v", ".3gp"}


# ── FFmpeg / FFprobe paths ─────────────────────────────────────────

def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    win = r"C:\ffmpeg\bin\ffmpeg.exe"
    if os.path.exists(win):
        return win
    raise RuntimeError("FFmpeg not found. Install from https://ffmpeg.org/download.html")

def _ffprobe() -> str:
    path = shutil.which("ffprobe")
    if path:
        return path
    win = r"C:\ffmpeg\bin\ffprobe.exe"
    if os.path.exists(win):
        return win
    return _ffmpeg().replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")


# ── Duration probe ─────────────────────────────────────────────────

async def _get_duration(video_path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        _ffprobe(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except Exception:
        return 0.0


# ── FFmpeg runner with progress ────────────────────────────────────

async def _run_with_progress(cmd: list[str], duration: float, progress_cb=None) -> None:
    import time
    full_cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    logger.info("FFmpeg: %s", " ".join(full_cmd))

    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    start_time  = time.time()
    last_report = 0.0
    out_time_us = 0
    speed_str   = "..."

    async def read_progress():
        nonlocal out_time_us, speed_str, last_report
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text.startswith("out_time_us="):
                try:
                    out_time_us = int(text.split("=")[1])
                except ValueError:
                    pass
            elif text.startswith("speed="):
                speed_str = text.split("=")[1].strip()
            elif text in ("progress=continue", "progress=end"):
                now = time.time()
                if progress_cb and duration > 0 and (now - last_report) >= 3:
                    last_report = now
                    pct = min(int((out_time_us / 1_000_000) / duration * 100), 99)
                    remaining = duration - (out_time_us / 1_000_000)
                    try:
                        multiplier = float(speed_str.replace("x", "").strip())
                        eta_secs = int(remaining / multiplier) if multiplier > 0 else 0
                    except Exception:
                        eta_secs = 0
                    eta_str = f"{eta_secs // 60}m {eta_secs % 60}s" if eta_secs > 60 else f"{eta_secs}s"
                    await progress_cb(pct, speed_str, eta_str)

    await read_progress()
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        tail = stderr.decode(errors="replace")[-800:]
        raise RuntimeError(f"FFmpeg error (code {proc.returncode}):\n{tail}")

    if progress_cb:
        await progress_cb(100, speed_str, "0s")


# ── Subtitle burning ───────────────────────────────────────────────

async def burn_subtitles(video_path: str, subtitle_path: str, progress_cb=None) -> str:
    """
    Burn subtitles into video.

    Windows-compatible strategy:
    - Convert subtitle to SRT (not ASS) — avoids the ass= filter entirely
    - Place the SRT file in the SAME directory as the video
    - Use just the filename (no path) in the subtitles= filter
    - Run FFmpeg with cwd set to that directory
    This completely sidesteps all Windows path/drive-letter escaping issues.
    """
    ffmpeg = _ffmpeg()
    stem   = Path(video_path).stem
    output = os.path.join(TEMP_DIR, f"{stem}_subtitled.mp4")

    sub_ext = Path(subtitle_path).suffix.lower()

    # ── Convert subtitle to SRT in TEMP_DIR ───────────────────────
    # SRT is the safest format — no path issues, universally supported
    srt_path = os.path.join(TEMP_DIR, "subtitle_burn.srt")

    if sub_ext == ".srt":
        shutil.copy2(subtitle_path, srt_path)
    else:
        # Convert ASS/VTT/SUB → SRT
        conv = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", subtitle_path, srt_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await conv.communicate()
        if conv.returncode != 0:
            # Fallback: just copy as-is and hope FFmpeg handles it
            shutil.copy2(subtitle_path, srt_path)

    # ── Use filename only — run FFmpeg from TEMP_DIR ───────────────
    # By setting cwd=TEMP_DIR and using just "subtitle_burn.srt",
    # FFmpeg never has to deal with a Windows absolute path in the filter.
    sub_filter = (
        "subtitles=subtitle_burn.srt"
        ":force_style='FontName=Arial,FontSize=24,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=1'"
    )

    duration = await _get_duration(video_path)

    # Use absolute paths for input/output but cwd trick for subtitle
    full_cmd = [
        ffmpeg, "-progress", "pipe:1", "-nostats",
        "-y",
        "-i", os.path.abspath(video_path),
        "-vf", sub_filter,
        "-c:v", FFMPEG_VIDEO_CODEC,
        "-preset", FFMPEG_PRESET,
        "-crf", FFMPEG_CRF,
        "-c:a", FFMPEG_AUDIO_CODEC,
        "-b:a", "192k",
        "-movflags", "+faststart",
        os.path.abspath(output),
    ]

    logger.info("FFmpeg (cwd=%s): %s", TEMP_DIR, " ".join(full_cmd))

    import time
    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=TEMP_DIR,   # ← key: FFmpeg finds subtitle_burn.srt here
    )

    start_time  = time.time()
    last_report = 0.0
    out_time_us = 0
    speed_str   = "..."

    async def read_progress():
        nonlocal out_time_us, speed_str, last_report
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text.startswith("out_time_us="):
                try:
                    out_time_us = int(text.split("=")[1])
                except ValueError:
                    pass
            elif text.startswith("speed="):
                speed_str = text.split("=")[1].strip()
            elif text in ("progress=continue", "progress=end"):
                now = time.time()
                if progress_cb and duration > 0 and (now - last_report) >= 3:
                    last_report = now
                    pct = min(int((out_time_us / 1_000_000) / duration * 100), 99)
                    remaining = duration - (out_time_us / 1_000_000)
                    try:
                        multiplier = float(speed_str.replace("x", "").strip())
                        eta_secs = int(remaining / multiplier) if multiplier > 0 else 0
                    except Exception:
                        eta_secs = 0
                    eta_str = f"{eta_secs // 60}m {eta_secs % 60}s" if eta_secs > 60 else f"{eta_secs}s"
                    await progress_cb(pct, speed_str, eta_str)

    await read_progress()
    _, stderr = await proc.communicate()

    # Cleanup SRT
    try:
        os.remove(srt_path)
    except Exception:
        pass

    if proc.returncode != 0:
        tail = stderr.decode(errors="replace")[-800:]
        raise RuntimeError(f"FFmpeg subtitle burn failed:\n{tail}")

    if progress_cb:
        await progress_cb(100, speed_str, "0s")

    return output


# ── Resolution conversion ──────────────────────────────────────────

async def change_resolution(video_path: str, scale: str, progress_cb=None) -> str:
    ffmpeg   = _ffmpeg()
    w, h     = scale.split(":")
    stem     = Path(video_path).stem
    output   = os.path.join(TEMP_DIR, f"{stem}_{w}x{h}.mp4")
    duration = await _get_duration(video_path)

    scale_filter = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    )

    await _run_with_progress([
        ffmpeg, "-y",
        "-i", video_path,
        "-vf", scale_filter,
        "-c:v", FFMPEG_VIDEO_CODEC,
        "-preset", FFMPEG_PRESET,
        "-crf", FFMPEG_CRF,
        "-c:a", "copy",
        "-movflags", "+faststart",
        output,
    ], duration, progress_cb)

    return output


# ── URL Downloader ─────────────────────────────────────────────────

async def download_url(url: str, job_id: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=600)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Server returned HTTP {resp.status}.\n"
                    "Make sure the URL is a direct link to a video file."
                )
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_SIZE_BYTES:
                raise RuntimeError(
                    f"File too large ({int(content_length) / 1024**2:.0f} MB). Max is 2 GB."
                )

            ext  = _ext_from_url(url) or _ext_from_content_type(resp.headers.get("Content-Type", "")) or ".mp4"
            dest = os.path.join(TEMP_DIR, f"{job_id}_downloaded{ext}")

            downloaded = 0
            async with aiofiles.open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_SIZE_BYTES:
                        raise RuntimeError("Download exceeded 2 GB limit.")
                    await fh.write(chunk)

            logger.info(f"Downloaded {downloaded / 1024**2:.1f} MB → {dest}")
            return dest


def _ext_from_url(url: str) -> str:
    path = url.split("?")[0].split("#")[0]
    ext  = Path(path).suffix.lower()
    return ext if ext in VIDEO_URL_EXTENSIONS else ""


def _ext_from_content_type(content_type: str) -> str:
    ct_map = {
        "video/mp4": ".mp4", "video/x-msvideo": ".avi",
        "video/quicktime": ".mov", "video/webm": ".webm",
        "video/x-matroska": ".mkv", "video/x-flv": ".flv",
    }
    return ct_map.get(content_type.split(";")[0].strip(), "")
