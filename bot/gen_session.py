"""
Run this ONCE locally to generate your USER_SESSION string.
Copy the output and paste it into Railway as USER_SESSION env var.

Usage:
    python gen_session.py
"""

from pyrogram import Client
from config import API_ID, API_HASH

with Client(
    name="session_gen",
    api_id=API_ID,
    api_hash=API_HASH,
) as app:
    session = app.export_session_string()
    print("\n" + "="*60)
    print("YOUR SESSION STRING (copy everything between the lines):")
    print("="*60)
    print(session)
    print("="*60)
    print("\nPaste this as USER_SESSION in Railway environment variables.")
    print("Delete gen_session.py and session_gen.session after this.\n")
