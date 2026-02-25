"""
Video Studio Bot — entry point
"""

import logging
from client import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

import handlers.start
import handlers.workflow
import handlers.leech

if __name__ == "__main__":
    logging.info("Bot starting...")
    app.run()
