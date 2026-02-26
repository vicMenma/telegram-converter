"""
File Utilities
──────────────
Shared helpers used across handlers and processors:
  - Human-readable size formatting
  - Emoji icon lookup by file extension
  - Safe filename sanitisation
  - Temp file cleanup
"""

import os
import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Size formatting ────────────────────────────────────────────────

def format_size(num_bytes: int, suffix: str = "") -> str:
    """Return a human-readable file size string."""
    if num_bytes < 1024:
        return f"{num_bytes} B{suffix}"
    if num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.1f} KB{suffix}"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / 1024 ** 2:.1f} MB{suffix}"
    return f"{num_bytes / 1024 ** 3:.1f} GB{suffix}"


# ── Icon lookup ────────────────────────────────────────────────────

_EXT_ICONS: dict[str, str] = {
    "mp4": "🎬", "mkv": "🎬", "avi": "🎬",
    "mov": "🎬", "webm": "🎬", "flv": "🎬",
    "ts":  "🎬", "m4v": "🎬", "3gp": "🎬",
    "srt": "📝", "ass": "📝", "ssa": "📝",
    "vtt": "📝", "sub": "📝",
}

def file_icon(filename: str) -> str:
    """Return the best emoji icon for a given filename."""
    ext = Path(filename).suffix.lower().lstrip(".")
    return _EXT_ICONS.get(ext, "📁")


# ── Filename sanitisation ──────────────────────────────────────────

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def safe_filename(name: str, max_len: int = 200) -> str:
    """Strip illegal characters and enforce a maximum length."""
    name = _UNSAFE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "file"
    stem = Path(name).stem
    ext  = Path(name).suffix
    if len(name) > max_len:
        stem = stem[: max_len - len(ext)]
        name = stem + ext
    return name


def output_filename(original: str, suffix: str, ext: str = "mp4") -> str:
    """
    Build an output filename.
    Example: output_filename("my video.mp4", "subtitled") → "my video_subtitled.mp4"
    """
    stem = Path(safe_filename(original)).stem
    return f"{stem}_{suffix}.{ext}"


# ── Temp file cleanup ──────────────────────────────────────────────

def cleanup(*paths: str | None) -> None:
    """
    Delete one or more files silently.
    Accepts None values so uninitialised path variables are safe to pass.

    Usage:
        cleanup(video_path, sub_path, output_path)
    """
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
                logger.debug(f"Deleted temp file: {path}")
            except OSError as e:
                logger.warning(f"Could not delete {path}: {e}")
