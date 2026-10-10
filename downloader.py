"""
Music Downloader and Audio/Metadata Processor.
Features:
- YouTube download with Android/iOS client bypass and SoundCloud fallback
- EBU R128 loudness normalization (-14 LUFS) via FFmpeg loudnorm filter
- Full cover art recovery: square 1:1 RGB conversion (600x600 ID3 APIC & 320x320 Telegram thumbnail)
- Automatic genre hashtag extraction via iTunes API
- Lyrics retrieval via LRCLIB API
- Alternative versions lookup (top-3 results)
- Direct streaming link parsing (Spotify, Yandex Music, Apple Music, SoundCloud, YouTube)
"""

import os
import re
import json
import time
import secrets
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict, Any

import requests

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    yt_dlp = None
    YT_DLP_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False

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

# Common genre mappings
GENRE_MAP = {
    "hip-hop": "#hiphop #rap",
    "hip-hop/rap": "#hiphop #rap",
    "rap": "#rap #hiphop",
    "rock": "#rock",
    "hard rock": "#rock #hardrock",
    "alternative": "#alternative #rock",
    "pop": "#pop",
    "dance": "#dance #electronic",
    "electronic": "#electronic",
    "house": "#house #electronic",
    "techno": "#techno #electronic",
    "r&b": "#rnb",
    "r&b/soul": "#rnb #soul",
    "soul": "#soul",
    "metal": "#metal",
    "heavy metal": "#metal",
    "punk": "#punk #rock",
    "jazz": "#jazz",
    "blues": "#blues",
    "classical": "#classical",
    "reggae": "#reggae",
    "soundtrack": "#soundtrack",
    "phonk": "#phonk",
    "ambient": "#ambient",
    "indie": "#indie",
    "country": "#country",
    "disco": "#disco",
    "funk": "#funk"
}

@dataclass
class TrackInfo:
    id: str
    title: str
    artist: str
    duration: int
    mp3_path: Path
    cover_path: Optional[Path]
    thumb_path: Optional[Path]
    source: str
    folder_path: Path
    genre: str = "#музыка"

def clean_metadata(
    raw_title: str,
    raw_artist: str,
    hint_artist: Optional[str] = None,
    hint_title: Optional[str] = None,
    clean_query: str = ""
) -> Tuple[str, str]:
    """
    Cleans and deduplicates artist and song title.
    Removes YouTube junk like (Official Video), [Audio], etc.
    Avoids duplicate artist prefixes.
    """
    artist = (hint_artist or raw_artist or "").strip()
    title = (hint_title or raw_title or "").strip()

    if hint_artist and hint_title:
        title = re.sub(rf"^{re.escape(artist)}\s*[-–—:]\s*", "", title, flags=re.IGNORECASE).strip()
    else:
        prev_title = None
        while prev_title != title:
            prev_title = title
            title = JUNK_PATTERN.sub('', title).strip()

        if any(sep in title for sep in (" - ", " — ", " – ")):
            parts = re.split(r'\s*[-—–]\s*', title, maxsplit=1)
            p_artist, p_title = parts[0].strip(), parts[1].strip()
            if not artist or artist.lower() in ("музыка", "topic") or "vevo" in artist.lower() or "records" in artist.lower() or artist.lower() == p_artist.lower():
                artist = p_artist
                title = p_title

        if artist and title:
            title = re.sub(rf"^{re.escape(artist)}\s*[-–—:]\s*", "", title, flags=re.IGNORECASE).strip()

    if not artist or artist.lower() in ("музыка", "topic"):
        artist = "Неизвестен"
    if not title:
        title = clean_query or "Без названия"

    title = DUPLICATE_PREFIX_PATTERN.sub(r'\1 - ', title).strip()
    return artist, title

def process_cover_art(track_dir: Path, entry: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Locates or downloads thumbnail image, crops to square 1:1, and generates:
    1. cover.jpg: 600x600 square JPEG for ID3 APIC frame (in-player artwork)
    2. thumb.jpg: 320x320 square JPEG (<200 KB) for Telegram sendAudio thumbnail
    """
    if not PIL_AVAILABLE:
        # Fallback to existing jpg if PIL is missing
        jpgs = [f for f in track_dir.glob("*.jpg") if f.name not in ("cover.jpg", "thumb.jpg")]
        if jpgs:
            return jpgs[0], jpgs[0]
        return None, None

    # Step 1: Look for any image file on disk (yt-dlp may write .webp, .jpg, .png, etc.)
    img_candidates = [
        f for f in track_dir.glob("*.*")
        if f.suffix.lower() in ('.jpg', '.jpeg', '.webp', '.png', '.jfif', '.bmp')
        and f.name not in ('cover.jpg', 'thumb.jpg')
    ]
    raw_img_path = img_candidates[0] if img_candidates else None

    # Step 2: If no image on disk, fetch from entry thumbnail URL
    if not raw_img_path and entry:
        thumb_url = entry.get('thumbnail')
        if not thumb_url and entry.get('thumbnails'):
            thumb_url = entry['thumbnails'][-1].get('url')
        if thumb_url:
            try:
                r = requests.get(thumb_url, timeout=10)
                if r.status_code == 200 and len(r.content) > 100:
                    raw_img_path = track_dir / "raw_downloaded_thumb"
                    raw_img_path.write_bytes(r.content)
            except Exception as e:
                logger.warning(f"Failed to download thumbnail from {thumb_url}: {e}")

    if not raw_img_path or not raw_img_path.exists():
        return None, None

    try:
        with Image.open(raw_img_path) as raw_im:
            im = raw_im.convert("RGB")
            w, h = im.size
            min_dim = min(w, h)
            left = (w - min_dim) // 2
            top = (h - min_dim) // 2
            im_square = im.crop((left, top, left + min_dim, top + min_dim))

            # 1. High-resolution cover for ID3 tag (600x600)
            cover_path = track_dir / "cover.jpg"
            im_cover = im_square.resize((600, 600), Image.Resampling.LANCZOS)
            im_cover.save(cover_path, "JPEG", quality=92)

            # 2. Compact thumbnail for Telegram sendAudio (<200KB, 320x320)
            thumb_path = track_dir / "thumb.jpg"
            im_thumb = im_square.resize((320, 320), Image.Resampling.LANCZOS)
            im_thumb.save(thumb_path, "JPEG", quality=85)

        # Remove raw non-standard image files to keep directory clean
        for f in img_candidates:
            if f not in (cover_path, thumb_path):
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
        if raw_img_path not in (cover_path, thumb_path):
            try:
                raw_img_path.unlink(missing_ok=True)
            except Exception:
                pass

        return cover_path, thumb_path
    except Exception as e:
        logger.warning(f"Error processing cover art in {track_dir}: {e}")
        return None, None

def apply_id3_tags(mp3_path: Path, artist: str, title: str, cover_path: Optional[Path] = None):
    """Embeds ID3v2.3 tags (Title, Artist, Album, Cover Art) directly into the MP3 file."""
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

        # Embed APIC frame (Front Cover)
        if cover_path and cover_path.exists():
            id3 = ID3(str(mp3_path))
            id3.delall('APIC')
            with open(cover_path, 'rb') as img:
                id3.add(APIC(
                    encoding=3,  # UTF-8
                    mime='image/jpeg',
                    type=3,      # 3 = Front cover
                    desc='Cover',
                    data=img.read()
                ))
            id3.save(v2_version=3)
    except Exception as e:
        logger.warning(f"Could not apply ID3 tags to {mp3_path}: {e}")

def extract_genre(artist: str, title: str) -> str:
    """
    Fetches track genre from iTunes API and converts to clean hashtags.
    Falls back to #музыка if unavailable.
    """
    try:
        query = f"{artist} {title}".strip()
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": query, "entity": "song", "limit": 1},
            timeout=4
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("resultCount", 0) > 0:
                raw_genre = data["results"][0].get("primaryGenreName", "").lower().strip()
                if raw_genre in GENRE_MAP:
                    return f"#музыка {GENRE_MAP[raw_genre]}"
                # Fuzzy match in GENRE_MAP
                for key, tag in GENRE_MAP.items():
                    if key in raw_genre:
                        return f"#музыка {tag}"
                clean_tag = re.sub(r'[^a-zA-Z0-9\u0400-\u04FF]', '', raw_genre)
                if clean_tag:
                    return f"#музыка #{clean_tag}"
    except Exception as e:
        logger.debug(f"Genre extraction failed for {artist} - {title}: {e}")
    return "#музыка"

def fetch_lyrics(artist: str, title: str) -> Optional[str]:
    """
    Fetches track lyrics from LRCLIB API.
    Returns plain text lyrics or None.
    """
    try:
        # 1. Exact match get endpoint
        resp = requests.get(
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title},
            headers={"User-Agent": "MusicTelegramBot/2.0"},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            lyrics = data.get("plainLyrics") or data.get("syncedLyrics")
            if lyrics:
                # Strip timestamps if synced lyrics returned
                clean_text = re.sub(r'\[\d{2}:\d{2}\.\d{2,3}\]\s*', '', lyrics).strip()
                return clean_text

        # 2. Search endpoint fallback
        search_resp = requests.get(
            "https://lrclib.net/api/search",
            params={"q": f"{artist} {title}"},
            headers={"User-Agent": "MusicTelegramBot/2.0"},
            timeout=5
        )
        if search_resp.status_code == 200:
            results = search_resp.json()
            if results and isinstance(results, list):
                lyrics = results[0].get("plainLyrics") or results[0].get("syncedLyrics")
                if lyrics:
                    clean_text = re.sub(r'\[\d{2}:\d{2}\.\d{2,3}\]\s*', '', lyrics).strip()
                    return clean_text
    except Exception as e:
        logger.warning(f"Error fetching lyrics for {artist} - {title}: {e}")
    return None

def resolve_music_link(text: str) -> Dict[str, Any]:
    """
    Detects if the input is a direct link (YouTube, SoundCloud, Spotify, Yandex Music, Apple Music).
    Extracts metadata or returns query/url for direct processing.
    """
    text = text.strip()
    result = {
        "is_link": False,
        "query": text,
        "source": "text",
        "is_playlist": False,
        "artist": "",
        "title": "",
        "playlist_items": []
    }

    # 1. YouTube & YouTube Music
    yt_match = re.search(r'(?:https?://)?(?:www\.|music\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})', text)
    if yt_match:
        result["is_link"] = True
        result["source"] = "youtube"
        result["query"] = f"https://www.youtube.com/watch?v={yt_match.group(1)}"
        return result

    # 1.1 YouTube Playlist
    yt_pl = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', text)
    if yt_pl and ("youtube.com" in text or "youtu.be" in text):
        result["is_link"] = True
        result["source"] = "youtube_playlist"
        result["is_playlist"] = True
        result["query"] = text
        return result

    # 2. SoundCloud
    if "soundcloud.com/" in text:
        result["is_link"] = True
        result["source"] = "soundcloud"
        result["query"] = text
        return result

    # 3. Spotify Track
    sp_track = re.search(r'open\.spotify\.com/track/([a-zA-Z0-9]+)', text)
    if sp_track:
        result["is_link"] = True
        result["source"] = "spotify"
        try:
            oe = requests.get(
                f"https://open.spotify.com/oembed?url={text}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5
            ).json()
            title = oe.get("title", "")
            if title:
                result["query"] = title
                result["title"] = title
        except Exception as e:
            logger.warning(f"Spotify oEmbed error: {e}")
        return result

    # 3.1 Spotify Album / Playlist
    if "open.spotify.com/album/" in text or "open.spotify.com/playlist/" in text:
        result["is_link"] = True
        result["source"] = "spotify_playlist"
        result["is_playlist"] = True
        try:
            oe = requests.get(
                f"https://open.spotify.com/oembed?url={text}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5
            ).json()
            result["query"] = oe.get("title", "Spotify Playlist")
        except Exception:
            pass
        return result

    # 4. Yandex Music Track
    ya_track = re.search(r'music\.yandex\.(?:ru|com)/(?:album/\d+/)?track/(\d+)', text)
    if ya_track:
        track_id = ya_track.group(1)
        result["is_link"] = True
        result["source"] = "yandex"
        try:
            resp = requests.get(
                f"https://api.music.yandex.net/tracks/{track_id}",
                headers={"User-Agent": "Yandex-Music-API"},
                timeout=5
            )
            if resp.status_code == 200:
                t = resp.json().get("result", [{}])[0]
                artists = [a.get("name") for a in t.get("artists", [])]
                artist_str = ", ".join(filter(None, artists))
                title_str = t.get("title", "")
                result["artist"] = artist_str
                result["title"] = title_str
                result["query"] = f"{artist_str} - {title_str}".strip(" -")
        except Exception as e:
            logger.warning(f"Yandex Music API error: {e}")
        return result

    # 5. Apple Music
    if "music.apple.com" in text:
        result["is_link"] = True
        result["source"] = "apple"
        try:
            resp = requests.get(
                text,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=5
            )
            m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', resp.text)
            if m:
                raw = m.group(1).replace(" on Apple Music", "").replace(" в Apple Music", "").strip()
                if " by " in raw:
                    parts = raw.split(" by ", 1)
                    result["title"] = parts[0].strip()
                    result["artist"] = parts[1].strip()
                    result["query"] = f"{parts[1].strip()} - {parts[0].strip()}"
                else:
                    result["query"] = raw
        except Exception as e:
            logger.warning(f"Apple Music parser error: {e}")
        return result

    return result

class Downloader:
    def __init__(self, download_base_dir: Path):
        self.download_base_dir = Path(download_base_dir)
        self.download_base_dir.mkdir(parents=True, exist_ok=True)

    def search_versions(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        """
        Searches YouTube for top alternative versions (e.g. remix, live, acoustic, original).
        Returns a list of dicts with title, duration, uploader, video_id, url.
        """
        if not YT_DLP_AVAILABLE:
            return []

        clean_q = query.strip()
        opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios'],
                    'player_skip': ['webpage', 'configs']
                }
            }
        }

        versions = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit + 3}:{clean_q}", download=False)
                entries = info.get('entries', []) if info else []
                for entry in entries:
                    if not entry:
                        continue
                    v_id = entry.get('id')
                    raw_title = entry.get('title', '')
                    raw_uploader = entry.get('uploader', '')
                    dur = int(entry.get('duration') or 0)
                    artist, title = clean_metadata(raw_title, raw_uploader, clean_query=clean_q)

                    versions.append({
                        "id": v_id,
                        "title": title,
                        "artist": artist,
                        "duration": dur,
                        "url": f"https://www.youtube.com/watch?v={v_id}",
                        "raw_title": raw_title
                    })
                    if len(versions) >= limit:
                        break
        except Exception as e:
            logger.warning(f"Error searching versions for '{clean_q}': {e}")

        return versions

    def extract_playlist_tracks(self, playlist_url: str, max_tracks: int = 15) -> List[str]:
        """Extracts up to max_tracks search queries from a playlist URL."""
        if not YT_DLP_AVAILABLE:
            return []
        opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
        }
        track_queries = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(playlist_url, download=False)
                entries = info.get('entries', []) if info else []
                for entry in entries[:max_tracks]:
                    if not entry:
                        continue
                    t = entry.get('title')
                    if t:
                        track_queries.append(t)
        except Exception as e:
            logger.warning(f"Error extracting playlist {playlist_url}: {e}")
        return track_queries

    def download_track(
        self,
        query: str,
        hint_artist: Optional[str] = None,
        hint_title: Optional[str] = None,
        direct_url: Optional[str] = None
    ) -> TrackInfo:
        """
        Downloads track with EBU R128 loudness normalization (-14 LUFS),
        high-fidelity cover art processing (600x600 ID3 APIC & 320x320 thumbnail),
        ID3 tag injection, and genre detection.
        """
        clean_query = query.strip()
        if not clean_query and not direct_url:
            raise ValueError("Query or direct_url cannot be empty")

        if not YT_DLP_AVAILABLE:
            raise RuntimeError("yt-dlp is not installed")

        track_id = secrets.token_hex(4)
        track_dir = self.download_base_dir / track_id
        track_dir.mkdir(parents=True, exist_ok=True)

        target_url = direct_url if direct_url else clean_query

        # yt-dlp config with EBU R128 loudness normalization and thumbnail generation
        common_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(track_dir / '%(title)s.%(ext)s'),
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320'
                },
                {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
                {'key': 'EmbedThumbnail'},
                {'key': 'FFmpegMetadata'},
            ],
            'postprocessor_args': {
                'FFmpegExtractAudio': ['-af', 'loudnorm=I=-14:TP=-1.5:LRA=11']
            },
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

        search_query = target_url if target_url.startswith("http") else f"ytsearch1:{target_url}"

        # Strategy A: YouTube (with Android/iOS client bypass)
        logger.info(f"Downloading from YouTube: '{search_query}' (id: {track_id})")
        try:
            with yt_dlp.YoutubeDL(yt_opts) as ydl:
                info = ydl.extract_info(search_query, download=True)
                if 'entries' in info and info['entries']:
                    entry = info['entries'][0]
                elif info:
                    entry = info
        except Exception as e:
            last_error = e
            logger.warning(f"YouTube download failed for '{target_url}': {e}. Falling back to SoundCloud...")

        # Strategy B: Fallback to SoundCloud (scsearch1)
        if not entry:
            source = "soundcloud"
            sc_query = target_url if target_url.startswith("http") else f"scsearch1:{target_url}"
            logger.info(f"Downloading from SoundCloud fallback: '{sc_query}' (id: {track_id})")
            sc_opts = dict(common_opts)
            try:
                with yt_dlp.YoutubeDL(sc_opts) as ydl:
                    info = ydl.extract_info(sc_query, download=True)
                    if 'entries' in info and info['entries']:
                        entry = info['entries'][0]
                    elif info:
                        entry = info
            except Exception as e:
                logger.error(f"SoundCloud fallback also failed for '{target_url}': {e}")
                raise Exception(f"Не удалось скачать трек ни с YouTube, ни с SoundCloud. Ошибка: {last_error or e}")

        if not entry:
            raise Exception("Не удалось найти подходящий аудиопоток.")

        # Extract and clean metadata
        raw_title = entry.get('track') or entry.get('title') or clean_query
        raw_artist = entry.get('artist') or entry.get('uploader') or ''
        artist, title = clean_metadata(raw_title, raw_artist, hint_artist, hint_title, clean_query)
        duration = int(entry.get('duration') or 0)

        # Locate converted MP3
        mp3_files = list(track_dir.glob("*.mp3"))
        if not mp3_files:
            raise FileNotFoundError("MP3 файл не найден после конвертации FFmpeg")
        mp3_path = mp3_files[0]

        # Process and recover cover art (600x600 for ID3 tag, 320x320 for Telegram thumbnail)
        cover_path, thumb_path = process_cover_art(track_dir, entry)

        # Embed tags and cover into MP3 file
        apply_id3_tags(mp3_path, artist, title, cover_path)

        # Detect genre
        genre = extract_genre(artist, title)

        # Save metadata JSON for recovery / bot state
        meta = {
            "id": track_id,
            "query": clean_query,
            "title": title,
            "artist": artist,
            "duration": duration,
            "mp3_path": str(mp3_path),
            "cover_path": str(cover_path) if cover_path else None,
            "thumb_path": str(thumb_path) if thumb_path else None,
            "source": source,
            "genre": genre,
            "created_at": time.time()
        }
        (track_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return TrackInfo(
            id=track_id,
            title=title,
            artist=artist,
            duration=duration,
            mp3_path=mp3_path,
            cover_path=cover_path,
            thumb_path=thumb_path,
            source=source,
            folder_path=track_dir,
            genre=genre
        )
