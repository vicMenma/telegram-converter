"""
Video Studio Bot — entry point
"""

import logging
from client import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Import handlers so their decorators register with the app
import handlers.start
import handlers.workflow

if __name__ == "__main__":
    logging.info("Bot starting...")
    app.run()
