"""
Video Studio Bot — entry point
"""

import logging
import asyncio
from client import app
from user_client import get_user_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

import handlers.start
import handlers.workflow
import handlers.leech
import handlers.settings
import handlers.features


async def main():
    # Start user client if session string is configured
    user = get_user_client()
    if user:
        try:
            await user.start()
            logging.info("✅ User account connected — fast uploads enabled")
        except Exception as e:
            logging.warning(f"⚠️ User account failed to connect: {e} — falling back to bot uploads")

    # Start bot
    await app.start()
    logging.info("✅ Bot started")

    await asyncio.get_event_loop().create_future()  # run forever


if __name__ == "__main__":
    logging.info("Bot starting...")
    app.run(main())
