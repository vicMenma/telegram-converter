"""
Per-user persistent settings stored in memory.
Falls back to defaults if not set.
"""

from typing import Any

DEFAULTS: dict[str, Any] = {
    "upload_type":   "video",
    "preset":        "fast",
    "crf":           23,
    "default_res":   "source",
    "notify_done":   True,
    "auto_forward":  False,
    "channel_ids":   [],           # list of channel IDs/usernames
}

_STORE: dict[int, dict[str, Any]] = {}


def get(uid: int, key: str) -> Any:
    val = _STORE.get(uid, {}).get(key, DEFAULTS[key])
    # backward compat: old single channel_id string
    if key == "channel_ids" and isinstance(val, str):
        return [val] if val else []
    return val


def set(uid: int, key: str, value: Any) -> None:
    if uid not in _STORE:
        _STORE[uid] = {}
    _STORE[uid][key] = value


def get_all(uid: int) -> dict[str, Any]:
    base = dict(DEFAULTS)
    base.update(_STORE.get(uid, {}))
    # backward compat
    if isinstance(base.get("channel_ids"), str):
        base["channel_ids"] = [base["channel_ids"]] if base["channel_ids"] else []
    return base


def reset(uid: int) -> None:
    _STORE.pop(uid, None)
