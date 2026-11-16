"""
Database storage and caching layer for Music Bot.
Manages SQLite track cache (file_id) for instant 0.1s replay,
tag history, channel publishing records, user channel preferences, and cache retention in WAL mode.
"""

import time
import secrets
import sqlite3
import logging
import contextlib
import unicodedata
import re
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("music_bot.database")

CLEAN_QUERY_REGEX = re.compile(r'[^a-zA-Z0-9\u0400-\u04FF\s]')

def normalize_key(text: str) -> str:
    """Normalizes query or artist+title string for fuzzy matching (supports accents and diacritics)."""
    if not text:
        return ""
    nfkd = unicodedata.normalize('NFKD', text)
    cleaned = CLEAN_QUERY_REGEX.sub(' ', nfkd.lower())
    return " ".join(cleaned.split())

class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self):
        """Initializes tables, indexes, and activates WAL mode for concurrency."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")

            # Track cache table for instant 0.1s audio replay
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS track_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query_clean TEXT UNIQUE NOT NULL,
                    artist TEXT NOT NULL,
                    title TEXT NOT NULL,
                    duration INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    channel_msg_id INTEGER,
                    channel_link TEXT,
                    genre TEXT,
                    created_at REAL NOT NULL
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cache_query ON track_cache(query_clean);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cache_artist_title ON track_cache(artist, title);")

            # Channels where bot is administrator, mapped to the user who added/manages it
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    channel_title TEXT NOT NULL,
                    channel_username TEXT,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(user_id, channel_id)
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_channels_uid ON user_channels(user_id);")

            # Active target channel for each user
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id TEXT PRIMARY KEY,
                    target_channel TEXT NOT NULL,
                    target_channel_title TEXT,
                    updated_at REAL NOT NULL
                );
            """)

            # Personal Shazam / Apple Shortcuts tokens for each user
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_shazam_tokens (
                    user_id TEXT PRIMARY KEY,
                    token TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_shazam_token ON user_shazam_tokens(token);")
            conn.commit()
            logger.info(f"Database initialized in WAL mode at {self.db_path}")

    # Track cache methods
    def get_cached_track(
        self,
        query: str,
        hint_artist: Optional[str] = None,
        hint_title: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        norm_q = normalize_key(query)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if norm_q:
                cursor.execute("SELECT * FROM track_cache WHERE query_clean = ? LIMIT 1", (norm_q,))
                row = cursor.fetchone()
                if row:
                    return dict(row)

            if hint_artist and hint_title:
                norm_combo = normalize_key(f"{hint_artist} {hint_title}")
                cursor.execute("SELECT * FROM track_cache WHERE query_clean = ? LIMIT 1", (norm_combo,))
                row = cursor.fetchone()
                if row:
                    return dict(row)

            if norm_q:
                cursor.execute(
                    "SELECT * FROM track_cache WHERE LOWER(artist || ' ' || title) = ? OR LOWER(title || ' ' || artist) = ? LIMIT 1",
                    (norm_q, norm_q)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)

        return None

    def get_track_by_id(self, track_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM track_cache WHERE id = ? LIMIT 1", (track_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def count_cached_tracks(self) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM track_cache")
            return cursor.fetchone()[0]

    def save_cached_track(
        self,
        query: str,
        artist: str,
        title: str,
        duration: int,
        file_id: str,
        channel_msg_id: Optional[int] = None,
        channel_link: Optional[str] = None,
        genre: Optional[str] = None
    ):
        norm_q = normalize_key(query)
        combo_q = normalize_key(f"{artist} {title}")
        now = time.time()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            if norm_q:
                cursor.execute("""
                    INSERT INTO track_cache (query_clean, artist, title, duration, file_id, channel_msg_id, channel_link, genre, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(query_clean) DO UPDATE SET
                        file_id = excluded.file_id,
                        channel_msg_id = COALESCE(excluded.channel_msg_id, track_cache.channel_msg_id),
                        channel_link = COALESCE(excluded.channel_link, track_cache.channel_link),
                        genre = COALESCE(excluded.genre, track_cache.genre),
                        created_at = excluded.created_at
                """, (norm_q, artist, title, duration, file_id, channel_msg_id, channel_link, genre, now))

            if combo_q and combo_q != norm_q:
                cursor.execute("""
                    INSERT INTO track_cache (query_clean, artist, title, duration, file_id, channel_msg_id, channel_link, genre, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(query_clean) DO UPDATE SET
                        file_id = excluded.file_id,
                        channel_msg_id = COALESCE(excluded.channel_msg_id, track_cache.channel_msg_id),
                        channel_link = COALESCE(excluded.channel_link, track_cache.channel_link),
                        genre = COALESCE(excluded.genre, track_cache.genre),
                        created_at = excluded.created_at
                """, (combo_q, artist, title, duration, file_id, channel_msg_id, channel_link, genre, now))

            conn.commit()

    def update_channel_info(self, cached_id: int, channel_msg_id: int, channel_link: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE track_cache SET channel_msg_id = ?, channel_link = ? WHERE id = ?",
                (channel_msg_id, channel_link, cached_id)
            )
            conn.commit()

    def update_cached_tags(self, file_id: str, new_artist: str, new_title: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE track_cache SET artist = ?, title = ? WHERE file_id = ?",
                (new_artist, new_title, file_id)
            )
            conn.commit()

    # User channels & settings methods
    def save_user_channel(
        self,
        user_id: str,
        channel_id: str,
        channel_title: str,
        channel_username: Optional[str] = None,
        status: str = "administrator"
    ):
        """Saves or updates a channel where the user added the bot as administrator."""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_channels (user_id, channel_id, channel_title, channel_username, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, channel_id) DO UPDATE SET
                    channel_title = excluded.channel_title,
                    channel_username = COALESCE(excluded.channel_username, user_channels.channel_username),
                    status = excluded.status,
                    updated_at = excluded.updated_at
            """, (str(user_id), str(channel_id), channel_title.strip(), channel_username, status, now))
            conn.commit()

    def get_user_channels(self, user_id: str) -> List[Dict[str, Any]]:
        """Returns all channels where this user has added the bot as administrator."""
        if not user_id:
            return []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM user_channels WHERE user_id = ? AND status IN ('administrator', 'creator') ORDER BY updated_at DESC",
                (str(user_id),)
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_channel_by_id(self, user_id: str, channel_id: str) -> Optional[Dict[str, Any]]:
        """Gets a single channel info for a user."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM user_channels WHERE user_id = ? AND channel_id = ? LIMIT 1",
                (str(user_id), str(channel_id))
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def remove_user_channel(self, user_id: str, channel_id: str) -> bool:
        """Removes a channel from user's managed channels and clears active setting if matched."""
        if not user_id or not channel_id:
            return False
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_channels WHERE user_id = ? AND channel_id = ?",
                (str(user_id), str(channel_id))
            )
            cursor.execute(
                "DELETE FROM user_settings WHERE user_id = ? AND target_channel = ?",
                (str(user_id), str(channel_id))
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_active_channel(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Returns the user's currently selected active target channel."""
        if not user_id:
            return None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_settings WHERE user_id = ? LIMIT 1", (str(user_id),))
            row = cursor.fetchone()
            if row:
                return dict(row)

            # Auto-fallback to the only connected channel if user has exactly one
            cursor.execute(
                "SELECT * FROM user_channels WHERE user_id = ? AND status IN ('administrator', 'creator') LIMIT 2",
                (str(user_id),)
            )
            chs = cursor.fetchall()
            if len(chs) == 1:
                single = dict(chs[0])
                self.set_active_channel(user_id, single["channel_id"], single["channel_title"])
                return {
                    "target_channel": single["channel_id"],
                    "target_channel_title": single["channel_title"]
                }

            return None

    def set_active_channel(self, user_id: str, target_channel: str, title: Optional[str] = None):
        """Sets or updates the active target channel for a user."""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_settings (user_id, target_channel, target_channel_title, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    target_channel = excluded.target_channel,
                    target_channel_title = excluded.target_channel_title,
                    updated_at = excluded.updated_at
            """, (str(user_id), str(target_channel).strip(), title.strip() if title else None, now))
            conn.commit()

    def clear_active_channel(self, user_id: str) -> bool:
        """Clears the active channel setting for a user."""
        if not user_id:
            return False
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_settings WHERE user_id = ?", (str(user_id),))
            conn.commit()
            return cursor.rowcount > 0

    def cleanup_old_cache(self, max_age_seconds: float) -> int:
        """Removes track cache entries older than max_age_seconds."""
        if max_age_seconds <= 0:
            return 0
        cutoff = time.time() - max_age_seconds
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM track_cache WHERE created_at < ?", (cutoff,))
            deleted = cursor.rowcount
            conn.commit()
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} expired track cache records older than {max_age_seconds}s")
            return deleted

    # Personal Shazam / Apple Shortcuts token management
    def get_or_create_shazam_token(self, user_id: str, default_token: Optional[str] = None) -> str:
        """Returns existing secret token for user, or generates a new 32-char hex token."""
        if not user_id:
            return ""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT token FROM user_shazam_tokens WHERE user_id = ? LIMIT 1", (str(user_id),))
            row = cursor.fetchone()
            if row:
                return row["token"]

            new_token = default_token or secrets.token_hex(16)
            now = time.time()
            cursor.execute("""
                INSERT INTO user_shazam_tokens (user_id, token, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET token = excluded.token
            """, (str(user_id), new_token, now))
            conn.commit()
            return new_token

    def regenerate_shazam_token(self, user_id: str) -> str:
        """Generates and updates a fresh secret token for the user."""
        if not user_id:
            return ""
        new_token = secrets.token_hex(16)
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_shazam_tokens (user_id, token, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    token = excluded.token,
                    created_at = excluded.created_at
            """, (str(user_id), new_token, now))
            conn.commit()
            return new_token

    def get_user_by_shazam_token(self, token: str) -> Optional[str]:
        """Looks up user_id associated with the personal Shazam token."""
        if not token:
            return None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM user_shazam_tokens WHERE token = ? LIMIT 1",
                (str(token).strip(),)
            )
            row = cursor.fetchone()
            return str(row["user_id"]) if row else None
