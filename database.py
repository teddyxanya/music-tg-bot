"""
Database storage and caching layer for Music Bot.
Manages SQLite track cache (file_id) for instant 0.1s replay,
tag history, channel publishing records, user channel preferences, and cache retention in WAL mode.
"""

import time
import sqlite3
import logging
import contextlib
import unicodedata
import re
from pathlib import Path
from typing import Optional, Dict, Any

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

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id TEXT PRIMARY KEY,
                    target_channel TEXT NOT NULL,
                    target_channel_title TEXT,
                    updated_at REAL NOT NULL
                );
            """)
            conn.commit()
            logger.info(f"Database initialized in WAL mode at {self.db_path}")

    def get_cached_track(
        self,
        query: str,
        hint_artist: Optional[str] = None,
        hint_title: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Looks up a track in the cache by normalized query or artist + title."""
        norm_q = normalize_key(query)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. Direct query match
            if norm_q:
                cursor.execute(
                    "SELECT * FROM track_cache WHERE query_clean = ? LIMIT 1",
                    (norm_q,)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)

            # 2. Artist + Title match
            if hint_artist and hint_title:
                norm_combo = normalize_key(f"{hint_artist} {hint_title}")
                cursor.execute(
                    "SELECT * FROM track_cache WHERE query_clean = ? LIMIT 1",
                    (norm_combo,)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)

            # 3. Fuzzy search in title/artist
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
        """Fetches a cached track record by its primary key ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM track_cache WHERE id = ? LIMIT 1", (track_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def count_cached_tracks(self) -> int:
        """Returns total number of cached tracks."""
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
        """Caches a newly sent/uploaded track."""
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
        """Updates channel publication link and message ID for a cached track."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE track_cache SET channel_msg_id = ?, channel_link = ? WHERE id = ?",
                (channel_msg_id, channel_link, cached_id)
            )
            conn.commit()

    def update_cached_tags(self, file_id: str, new_artist: str, new_title: str):
        """Updates artist and title metadata for an existing cached track."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE track_cache SET artist = ?, title = ? WHERE file_id = ?",
                (new_artist, new_title, file_id)
            )
            conn.commit()

    def get_user_channel(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Returns the user's custom publishing channel settings, if configured."""
        if not user_id:
            return None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_settings WHERE user_id = ? LIMIT 1", (str(user_id),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def set_user_channel(self, user_id: str, target_channel: str, title: Optional[str] = None):
        """Sets or updates the target channel for a user."""
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

    def clear_user_channel(self, user_id: str) -> bool:
        """Removes the custom channel setting for a user."""
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
