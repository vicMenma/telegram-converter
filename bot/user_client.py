"""
User account client for fast uploads/downloads.
"""

from pyrogram import Client, enums
from config import API_ID, API_HASH
import os

_user_app = None


def get_user_client() -> Client | None:
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
        workers=32,
        max_concurrent_transmissions=20,
        parse_mode=enums.ParseMode.HTML,
    )
    return _user_app
