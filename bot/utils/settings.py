"""
Per-user persistent settings stored in memory.
Falls back to defaults if not set.
"""

from typing import Any

# Defaults for every setting
DEFAULTS: dict[str, Any] = {
    "upload_type":   "video",       # "video" | "document"
    "preset":        "fast",      # ffmpeg preset
    "crf":           23,            # ffmpeg CRF quality (0-51, lower = better)
    "default_res":   "source",      # default resolution: "source" | "360" | "480" | "720" | "1080"
    "notify_done":   True,          # ping user when done
    "auto_forward":  False,         # auto-forward to channel without asking
    "channel_id":    "",            # channel ID or @username to forward to
}

_STORE: dict[int, dict[str, Any]] = {}


def get(uid: int, key: str) -> Any:
    return _STORE.get(uid, {}).get(key, DEFAULTS[key])


def set(uid: int, key: str, value: Any) -> None:
    if uid not in _STORE:
        _STORE[uid] = {}
    _STORE[uid][key] = value


def get_all(uid: int) -> dict[str, Any]:
    base = dict(DEFAULTS)
    base.update(_STORE.get(uid, {}))
    return base


def reset(uid: int) -> None:
    _STORE.pop(uid, None)
