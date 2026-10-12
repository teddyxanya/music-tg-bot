"""
Configuration module for Telegram Music Bot.
Loads settings from .env file or environment variables with strict type validation.
"""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

# Load .env file using python-dotenv if available, or fallback to simple parser
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))

@dataclass
class Settings:
    bot_token: str
    chat_id: Optional[str]
    owner_id: Optional[str]
    channel_id: Optional[str]
    shazam_secret: str
    host: str
    port: int
    max_workers: int
    temp_expiration_sec: int
    download_dir: Path
    sub_server_url: str = "http://127.0.0.1:8088"

    @classmethod
    def load(cls) -> "Settings":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
        owner_id = os.getenv("TELEGRAM_OWNER_ID", "").strip() or None
        channel_id = os.getenv("MUSIC_CHANNEL_ID", "").strip() or None
        shazam_secret = os.getenv("SHAZAM_SECRET_TOKEN", "").strip()
        host = os.getenv("WEBHOOK_HOST", "127.0.0.1").strip()
        sub_server_url = os.getenv("SUB_SERVER_URL", "http://127.0.0.1:8088").strip()
        
        try:
            port = int(os.getenv("WEBHOOK_PORT", "8082"))
        except ValueError:
            port = 8082

        try:
            max_workers = int(os.getenv("MAX_WORKERS", "4"))
        except ValueError:
            max_workers = 4

        try:
            temp_expiration_sec = int(os.getenv("TEMP_EXPIRATION_SECONDS", "7200"))
        except ValueError:
            temp_expiration_sec = 7200

        download_dir = BASE_DIR / "downloads"

        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
            owner_id=owner_id,
            channel_id=channel_id,
            shazam_secret=shazam_secret,
            host=host,
            port=port,
            max_workers=max_workers,
            temp_expiration_sec=temp_expiration_sec,
            download_dir=download_dir,
            sub_server_url=sub_server_url
        )

# Global settings instance
settings = Settings.load()
