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


async def _start_webhook_server():
    """Start FastAPI webhook server on port 8080."""
    try:
        import uvicorn
        from config import ADMIN_ID, CLOUDCONVERT_WEBHOOK_SECRET
        import webhook_server
        webhook_server.init(app, ADMIN_ID, CLOUDCONVERT_WEBHOOK_SECRET)

        config = uvicorn.Config(
            webhook_server.app,
            host="0.0.0.0",
            port=8080,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        asyncio.create_task(server.serve())
        logging.info("✅ Webhook server started on port 8080")
    except ImportError:
        logging.warning("⚠️ uvicorn not installed — webhook server disabled. Run: pip install uvicorn fastapi")
    except Exception as e:
        logging.warning(f"⚠️ Webhook server failed to start: {e}")


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

    # Start webhook server
    await _start_webhook_server()

    await asyncio.get_event_loop().create_future()  # run forever


if __name__ == "__main__":
    logging.info("Bot starting...")
    app.run(main())
