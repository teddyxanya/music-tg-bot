"""
Main Entry Point for Telegram Music Bot & Downloader.
Initializes logging, configuration, starts webhook server and polling worker.
"""

import sys
import signal
import logging
import threading

from config import settings
from bot import MusicBot
from webhook import start_webhook_server

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

def main():
    setup_logging()
    logger = logging.getLogger("music_bot.main")
    logger.info("Starting Music Telegram Bot Service...")

    if not settings.bot_token:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN is not defined in .env! Exiting.")
        sys.exit(1)

    bot = MusicBot(settings)
    http_server = start_webhook_server(settings, bot)

    server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    server_thread.start()

    def shutdown_handler(signum, frame):
        logger.info(f"Received signal {signum}. Shutting down gracefully...")
        bot.stop()
        http_server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        bot.run_polling()
    except Exception as e:
        logger.error(f"Fatal error in polling loop: {e}")
        bot.stop()
        http_server.shutdown()
        sys.exit(1)

if __name__ == "__main__":
    main()
