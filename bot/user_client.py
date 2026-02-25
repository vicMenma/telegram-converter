"""
User account client for fast uploads/downloads.
Uses the same API_ID/API_HASH as the bot but logs in as a real user.
This bypasses Telegram's bot upload speed limits.

Session string is stored in USER_SESSION env var so Railway doesn't
lose it on redeploy.
"""

from pyrogram import Client
from config import API_ID, API_HASH
import os

_user_app = None


def get_user_client() -> Client | None:
    """Return the user client if a session string is configured."""
    global _user_app
    if _user_app is not None:
        return _user_app

    session_string = os.getenv("USER_SESSION", "").strip()
    if not session_string:
        return None

    _user_app = Client(
        name="user_account",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        workers=8,
        max_concurrent_transmissions=10,
    )
    return _user_app
