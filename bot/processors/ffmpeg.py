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
from utils.file_utils import format_size
from config import (
    TEMP_DIR,
    FFMPEG_VIDEO_CODEC, FFMPEG_AUDIO_CODEC,
    FFMPEG_PRESET, FFMPEG_CRF, FFMPEG_THREADS,
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


async def _get_video_info(video_path: str) -> dict:
    """Get width, height, bitrate, codec of a video."""
    proc = await asyncio.create_subprocess_exec(
        _ffprobe(), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,bit_rate",
        "-show_entries", "format=bit_rate,size",
        "-of", "json",
        video_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        import json
        data    = json.loads(out.decode())
        stream  = data.get("streams", [{}])[0]
        fmt     = data.get("format", {})
        width   = int(stream.get("width", 0))
        height  = int(stream.get("height", 0))
        codec   = stream.get("codec_name", "")
        # bitrate in bits/s — prefer stream bitrate, fall back to format
        bitrate = int(stream.get("bit_rate") or fmt.get("bit_rate") or 0)
        size    = int(fmt.get("size", 0))
        return {"width": width, "height": height, "codec": codec,
                "bitrate": bitrate, "size": size}
    except Exception:
        return {"width": 0, "height": 0, "codec": "", "bitrate": 0, "size": 0}


async def _normalize_for_burn(video_path: str, job_id: str, progress_cb=None) -> str:
    """
    If the video has a very high bitrate or is >1080p, quickly re-encode
    it to a leaner intermediate before subtitle burning.
    This is faster overall because the burn step encodes fewer bits.

    Returns original path if normalization not needed, else new path.
    """
    ffmpeg = _ffmpeg()
    info   = await _get_video_info(video_path)
    width, height, bitrate = info["width"], info["height"], info["bitrate"]

    codec  = info["codec"]

    # Normalize if: >1080p, >5Mbps, or HEVC/AV1 codec (expensive to decode+encode)
    is_heavy_codec = codec in ("hevc", "h265", "av1", "vp9")
    needs_norm = (height > 1080) or (bitrate > 5_000_000 and bitrate != 0) or is_heavy_codec

    if not needs_norm:
        return video_path

    # Target: 720p max, CRF 28 — good enough for subtitle preview, much faster burn
    target_h   = min(height, 720)
    target_w   = -2   # keep aspect ratio
    norm_path  = os.path.join(TEMP_DIR, f"{job_id}_norm.mp4")

    logger.info(f"Normalizing {codec} {width}x{height} {bitrate//1000}kbps → 720p CRF28 before burn")

    if progress_cb:
        await progress_cb(0, "…", "…")

    norm_cmd = [
        ffmpeg, "-y",
        "-hwaccel", "auto",
        "-i", video_path,
        "-vf", f"scale={target_w}:{target_h}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",             # lower quality but much faster burn
        "-threads", "0",
        "-c:a", "copy",
        norm_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *norm_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    if proc.returncode == 0 and os.path.exists(norm_path):
        return norm_path
    return video_path   # fallback to original if norm failed


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

_ENCODER_CACHE: tuple[str, list] | None = None

def _pick_encoder(preset: str = None, crf: int = None) -> tuple[str, list]:
    """
    Pick the fastest available video encoder.
    Priority: h264_nvenc (NVIDIA) > h264_vaapi (Intel/AMD) > libx264
    Returns (encoder_name, extra_args_list)
    preset/crf override user settings when provided.
    """
    global _ENCODER_CACHE
    # Only use cache when no overrides
    if preset is None and crf is None and _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    _preset = preset or FFMPEG_PRESET
    _crf    = str(crf) if crf is not None else FFMPEG_CRF
    import shutil, subprocess
    ffmpeg = _ffmpeg()

    # Test NVIDIA NVENC
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "lavfi", "-i", "nullsrc",
             "-t", "0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=5
        )
        if r.returncode == 0:
            logger.info("Using NVIDIA NVENC encoder")
            _ENCODER_CACHE = ("h264_nvenc", ["-preset", "p1", "-tune", "ll", "-rc", "vbr", "-cq", "23"])
            return _ENCODER_CACHE
    except Exception:
        pass

    # Test VAAPI (Intel/AMD on Linux)
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-vaapi_device", "/dev/dri/renderD128",
             "-f", "lavfi", "-i", "nullsrc",
             "-t", "0.1", "-vf", "format=nv12,hwupload",
             "-c:v", "h264_vaapi", "-f", "null", "-"],
            capture_output=True, timeout=5
        )
        if r.returncode == 0:
            logger.info("Using VAAPI hardware encoder")
            _ENCODER_CACHE = ("h264_vaapi", ["-vaapi_device", "/dev/dri/renderD128",
                                  "-vf", "format=nv12,hwupload",
                                  "-qp", "23"])
            return _ENCODER_CACHE
    except Exception:
        pass

    # Fallback: libx264
    logger.info(f"Using libx264 {_preset} CRF={_crf} (software)")
    result = ("libx264", [
        "-preset", _preset,
        "-tune", "zerolatency",
        "-crf", _crf,
        "-threads", "0",
        "-x264-params", "nal-hrd=cbr:force-cfr=1:ref=1:bframes=0:weightp=0:subme=0:me=dia:trellis=0:8x8dct=0:fast-pskip=1",
    ])
    if preset is None and crf is None:
        _ENCODER_CACHE = result
    return result


async def burn_subtitles(video_path: str, subtitle_path: str, progress_cb=None, uid: int = 0) -> str:
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

    if sub_ext in (".srt", ".txt"):  # .txt is plain SRT without the extension
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

    # ── Pre-normalize if high bitrate/resolution ─────────────────
    # Normalizing to ≤1080p + ≤8Mbps before burning is faster overall
    # because the subtitle burn step has fewer bits to encode.
    stem    = Path(video_path).stem
    norm_path = await _normalize_for_burn(video_path, stem)
    actual_input = norm_path  # may be same as video_path if no norm needed

    duration = await _get_duration(actual_input)

    # Use absolute paths for input/output but cwd trick for subtitle
    _user_preset = _user_crf = None
    if uid:
        try:
            from utils.settings import get as _uget
            _user_preset = _uget(uid, "preset")
            _user_crf    = _uget(uid, "crf")
        except Exception:
            pass
    encoder, enc_opts = _pick_encoder(preset=_user_preset, crf=_user_crf)
    full_cmd = [
        ffmpeg, "-progress", "pipe:1", "-nostats",
        "-y",
        "-hwaccel", "auto",               # GPU-accelerated decode if available
        "-i", os.path.abspath(actual_input),
        "-vf", sub_filter,
        "-c:v", encoder,
        *enc_opts,
        "-pix_fmt", "yuv420p",            # avoid pixel format conversion overhead
        "-g", "60",                        # keyframe every 60 frames — less overhead
        "-sc_threshold", "0",             # disable scene change detection
        "-c:a", "copy",
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

    # Cleanup normalized intermediate if created
    if norm_path != video_path and os.path.exists(norm_path):
        try:
            os.remove(norm_path)
        except Exception:
            pass

    if proc.returncode != 0:
        tail = stderr.decode(errors="replace")[-800:]
        raise RuntimeError(f"FFmpeg subtitle burn failed:\n{tail}")

    if progress_cb:
        await progress_cb(100, speed_str, "0s")

    return output


# ── Resolution conversion ──────────────────────────────────────────

async def change_resolution(video_path: str, scale: str, progress_cb=None, uid: int = 0) -> str:
    ffmpeg   = _ffmpeg()
    w, h     = scale.split(":")
    stem     = Path(video_path).stem
    output   = os.path.join(TEMP_DIR, f"{stem}_{w}x{h}.mp4")
    duration = await _get_duration(video_path)

    scale_filter = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    )

    encoder, enc_opts = _pick_encoder()
    await _run_with_progress([
        ffmpeg, "-y",
        "-hwaccel", "auto",
        "-i", video_path,
        "-vf", scale_filter,
        "-c:v", encoder,
        *enc_opts,
        "-pix_fmt", "yuv420p",
        "-g", "60",
        "-sc_threshold", "0",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output,
    ], duration, progress_cb)

    return output


# ── URL Downloader ─────────────────────────────────────────────────

async def download_url(url: str, job_id: str, progress_msg=None) -> str:
    """
    Download a video from a URL with live progress updates.
    progress_msg: a Pyrogram Message object to edit with progress.
    """
    import time as _time
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
            total = int(content_length) if content_length else 0

            if total and total > MAX_DOWNLOAD_SIZE_BYTES:
                raise RuntimeError(
                    f"File too large ({total / 1024**2:.0f} MB). Max is 2 GB."
                )

            ext  = _ext_from_url(url) or _ext_from_content_type(resp.headers.get("Content-Type", "")) or ".mp4"
            dest = os.path.join(TEMP_DIR, f"{job_id}_downloaded{ext}")

            downloaded  = 0
            start_time  = _time.time()
            last_update = 0.0

            async with aiofiles.open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_SIZE_BYTES:
                        raise RuntimeError("Download exceeded 2 GB limit.")
                    await fh.write(chunk)

                    # Update progress every 3 seconds
                    now = _time.time()
                    if progress_msg and (now - last_update) >= 3:
                        last_update = now
                        elapsed    = max(now - start_time, 0.1)
                        speed      = downloaded / elapsed
                        speed_str  = f"{format_size(int(speed))}/s"

                        if total > 0:
                            pct     = min(int(downloaded * 100 / total), 99)
                            filled  = pct // 5
                            bar     = "█" * filled + "░" * (20 - filled)
                            remain  = total - downloaded
                            eta     = int(remain / speed) if speed > 0 else 0
                            eta_str = f"{eta // 60}m {eta % 60}s" if eta > 60 else f"{eta}s"
                            text = (
                                f"🌐 **Downloading from URL…**\n\n"
                                f"`{bar}`\n"
                                f"**{pct}%** — {format_size(downloaded)} / {format_size(total)}\n"
                                f"🚀 {speed_str} · ⏱ ETA {eta_str}"
                            )
                        else:
                            text = (
                                f"🌐 **Downloading from URL…**\n\n"
                                f"📥 {format_size(downloaded)} downloaded\n"
                                f"🚀 {speed_str}"
                            )
                        try:
                            await progress_msg.edit(text)
                        except Exception:
                            pass

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
