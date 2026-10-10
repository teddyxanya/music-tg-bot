"""
Music Downloader and Metadata Processor.
Handles audio downloading via YouTube (with Android/iOS client bypass) and SoundCloud fallback,
320kbps MP3 encoding, ID3 tag injection, and title/artist deduplication.
"""

import os
import re
import json
import time
import secrets
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    yt_dlp = None
    YT_DLP_AVAILABLE = False

try:
    import mutagen
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, APIC
    MUTAGEN_AVAILABLE = True
except ImportError:
    mutagen = None
    MUTAGEN_AVAILABLE = False

logger = logging.getLogger("music_bot.downloader")

# Pre-compiled regex for video title cleaning
JUNK_PATTERN = re.compile(
    r'\s*[\(\[][^\)\]]*(?:official|video|audio|visualizer|lyric|remaster|hq|hd|4k|1080p|clip|prod\.)[^\)\]]*[\)\]]',
    flags=re.IGNORECASE
)
DUPLICATE_PREFIX_PATTERN = re.compile(
    r'^(.*?)\s*[-–—:]\s*\1\s*[-–—:]\s*',
    flags=re.IGNORECASE
)

@dataclass
class TrackInfo:
    id: str
    title: str
    artist: str
    duration: int
    mp3_path: Path
    thumb_path: Optional[Path]
    source: str
    folder_path: Path

def clean_metadata(
    raw_title: str,
    raw_artist: str,
    hint_artist: Optional[str] = None,
    hint_title: Optional[str] = None,
    clean_query: str = ""
) -> Tuple[str, str]:
    """
    Cleans and deduplicates artist and song title.
    Removes YouTube video junk like (Official Video), [Audio], etc.
    Avoids duplicate artist occurrences (e.g. 'Moby - Moby - Natural Blues').
    """
    if hint_artist and hint_title:
        artist = hint_artist.strip()
        title = hint_title.strip()
        # If title starts with artist, strip it
        title = re.sub(rf"^{re.escape(artist)}\s*[-–—:]\s*", "", title, flags=re.IGNORECASE).strip()
    else:
        artist = raw_artist.strip() if raw_artist else ""
        title = raw_title.strip() if raw_title else ""

        # Remove video junk recursively until clean
        prev_title = None
        while prev_title != title:
            prev_title = title
            title = JUNK_PATTERN.sub('', title).strip()

        # If title is in format "Artist - Song", parse components
        if any(sep in title for sep in (" - ", " — ", " – ")):
            parts = re.split(r'\s*[-—–]\s*', title, maxsplit=1)
            p_artist, p_title = parts[0].strip(), parts[1].strip()
            # If artist is generic YouTube topic or matches parsed artist
            if not artist or artist.lower() in ("музыка", "topic") or "vevo" in artist.lower() or "records" in artist.lower() or artist.lower() == p_artist.lower():
                artist = p_artist
                title = p_title

        # Strip duplicated artist prefix if title still begins with artist
        if artist and title:
            title = re.sub(rf"^{re.escape(artist)}\s*[-–—:]\s*", "", title, flags=re.IGNORECASE).strip()

    # Final fallbacks
    if not artist or artist.lower() in ("музыка", "topic"):
        artist = "Неизвестен"
    if not title:
        title = clean_query or "Без названия"

    # Collapse any leftover duplicated prefixes (e.g. "Artist - Artist - Song")
    title = DUPLICATE_PREFIX_PATTERN.sub(r'\1 - ', title).strip()

    return artist, title

def apply_id3_tags(mp3_path: Path, artist: str, title: str, thumb_path: Optional[Path] = None):
    """Embeds ID3v2 tags (Title, Artist, Album, Cover Art) directly into the MP3 file."""
    if not MUTAGEN_AVAILABLE:
        return
    try:
        try:
            audio = EasyID3(str(mp3_path))
        except Exception:
            try:
                audio = mutagen.File(str(mp3_path), easy=True)
                if audio is not None:
                    audio.add_tags()
                else:
                    return
            except Exception:
                return

        audio['title'] = title
        audio['artist'] = artist
        audio['albumartist'] = artist
        audio.save(str(mp3_path))

        if thumb_path and thumb_path.exists():
            mime_type = "image/jpeg"
            if thumb_path.suffix.lower() == ".png":
                mime_type = "image/png"

            id3 = ID3(str(mp3_path))
            with open(thumb_path, 'rb') as img:
                id3.add(APIC(
                    encoding=3,
                    mime=mime_type,
                    type=3,  # Front cover
                    desc='Cover',
                    data=img.read()
                ))
            id3.save(v2_version=3)
    except Exception as e:
        logger.warning(f"Could not apply ID3 tags to {mp3_path}: {e}")

class Downloader:
    def __init__(self, download_base_dir: Path):
        self.download_base_dir = Path(download_base_dir)
        self.download_base_dir.mkdir(parents=True, exist_ok=True)

    def download_track(
        self,
        query: str,
        hint_artist: Optional[str] = None,
        hint_title: Optional[str] = None
    ) -> TrackInfo:
        """
        Downloads a single track by searching YouTube (with Android/iOS client bypass).
        Falls back to SoundCloud if YouTube fails or restricts the stream.
        """
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Query cannot be empty")

        if not YT_DLP_AVAILABLE:
            raise RuntimeError("yt-dlp is not installed")

        track_id = secrets.token_hex(4)
        track_dir = self.download_base_dir / track_id
        track_dir.mkdir(parents=True, exist_ok=True)

        # Base yt-dlp configuration with FFmpeg audio extraction and thumbnail converter
        common_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(track_dir / '%(title)s.%(ext)s'),
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'},
                {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
                {'key': 'EmbedThumbnail'},
                {'key': 'FFmpegMetadata'},
            ],
            'writethumbnail': True,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
        }

        yt_opts = dict(common_opts)
        yt_opts['extractor_args'] = {
            'youtube': {
                'player_client': ['android', 'ios'],
                'player_skip': ['webpage', 'configs']
            }
        }

        entry = None
        source = "youtube"
        last_error = None

        # Strategy A: YouTube single track search (ytsearch1)
        logger.info(f"Searching YouTube for: '{clean_query}' (id: {track_id})")
        try:
            with yt_dlp.YoutubeDL(yt_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{clean_query}", download=True)
                if 'entries' in info and info['entries']:
                    entry = info['entries'][0]
                elif info:
                    entry = info
        except Exception as e:
            last_error = e
            logger.warning(f"YouTube download failed for '{clean_query}': {e}. Falling back to SoundCloud...")

        # Strategy B: Fallback to SoundCloud (scsearch1)
        if not entry:
            source = "soundcloud"
            logger.info(f"Searching SoundCloud fallback for: '{clean_query}' (id: {track_id})")
            sc_opts = dict(common_opts)
            try:
                with yt_dlp.YoutubeDL(sc_opts) as ydl:
                    info = ydl.extract_info(f"scsearch1:{clean_query}", download=True)
                    if 'entries' in info and info['entries']:
                        entry = info['entries'][0]
                    elif info:
                        entry = info
            except Exception as e:
                logger.error(f"SoundCloud fallback also failed for '{clean_query}': {e}")
                raise Exception(f"Не удалось скачать трек ни с YouTube, ни с SoundCloud. Ошибка: {last_error or e}")

        if not entry:
            raise Exception("Не удалось найти подходящий аудиопоток.")

        # Extract and clean metadata
        raw_title = entry.get('track') or entry.get('title') or clean_query
        raw_artist = entry.get('artist') or entry.get('uploader') or ''
        artist, title = clean_metadata(raw_title, raw_artist, hint_artist, hint_title, clean_query)
        duration = int(entry.get('duration') or 0)

        # Locate downloaded MP3
        mp3_files = list(track_dir.glob("*.mp3"))
        if not mp3_files:
            raise FileNotFoundError("MP3 файл не найден после конвертации FFmpeg")
        mp3_path = mp3_files[0]

        # Locate thumbnail (prefer .jpg/.jpeg)
        thumb_files = [
            f for f in track_dir.glob("*.*")
            if f.suffix.lower() in ('.jpg', '.jpeg', '.png') and f.suffix.lower() != '.mp3'
        ]
        thumb_path = thumb_files[0] if thumb_files else None

        # Apply ID3 tags and cover art
        apply_id3_tags(mp3_path, artist, title, thumb_path)

        # Save metadata JSON for recovery / bot state
        meta = {
            "id": track_id,
            "query": clean_query,
            "title": title,
            "artist": artist,
            "duration": duration,
            "mp3_path": str(mp3_path),
            "thumb_path": str(thumb_path) if thumb_path else None,
            "source": source,
            "created_at": time.time()
        }
        (track_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return TrackInfo(
            id=track_id,
            title=title,
            artist=artist,
            duration=duration,
            mp3_path=mp3_path,
            thumb_path=thumb_path,
            source=source,
            folder_path=track_dir
        )
