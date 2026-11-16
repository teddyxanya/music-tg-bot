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
    default_channel_id: Optional[str]
    shazam_secret: str
    host: str
    port: int
    max_workers: int
    temp_expiration_sec: int
    cache_expiration_days: int
    download_dir: Path

    @property
    def channel_id(self) -> Optional[str]:
        """Backward compatibility alias for default_channel_id."""
        return self.default_channel_id

    @classmethod
    def load(cls) -> "Settings":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
        
        # Default channel can be provided via DEFAULT_MUSIC_CHANNEL_ID or legacy MUSIC_CHANNEL_ID
        default_channel_id = (
            os.getenv("DEFAULT_MUSIC_CHANNEL_ID", "").strip() or
            os.getenv("MUSIC_CHANNEL_ID", "").strip() or
            None
        )
        
        shazam_secret = os.getenv("SHAZAM_SECRET_TOKEN", "").strip()
        host = os.getenv("WEBHOOK_HOST", "127.0.0.1").strip()
        
        try:
            port = int(os.getenv("WEBHOOK_PORT", "8082"))
        except ValueError:
            port = 8082

        try:
            max_workers = int(os.getenv("MAX_WORKERS", "4"))
        except ValueError:
            max_workers = 4

        # Configurable temp files retention (seconds or hours)
        temp_exp = os.getenv("TEMP_EXPIRATION_SECONDS", "").strip()
        if temp_exp:
            try:
                temp_expiration_sec = int(temp_exp)
            except ValueError:
                temp_expiration_sec = 86400
        else:
            hours_str = os.getenv("CACHE_EXPIRATION_HOURS", "24").strip()
            try:
                temp_expiration_sec = int(hours_str) * 3600
            except ValueError:
                temp_expiration_sec = 86400

        # Configurable database track cache retention (days, 0 = keep forever)
        try:
            cache_expiration_days = int(os.getenv("CACHE_EXPIRATION_DAYS", "30").strip())
        except ValueError:
            cache_expiration_days = 30

        download_dir = BASE_DIR / "downloads"

        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
            default_channel_id=default_channel_id,
            shazam_secret=shazam_secret,
            host=host,
            port=port,
            max_workers=max_workers,
            temp_expiration_sec=temp_expiration_sec,
            cache_expiration_days=cache_expiration_days,
            download_dir=download_dir
        )

# Global settings instance
settings = Settings.load()
