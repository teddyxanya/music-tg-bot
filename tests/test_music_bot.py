"""
Comprehensive Unit Tests for Telegram Music Bot.
Covers metadata cleaning, downloader logic, cover art processing,
SQLite caching, genre extraction, lyrics fetching, streaming link resolution,
webhook endpoints, and concurrency.
"""

import io
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from database import Database, normalize_key
from downloader import (
    clean_metadata,
    process_cover_art,
    extract_genre,
    fetch_lyrics,
    resolve_music_link,
    Downloader
)
from webhook import WebhookHandler
from bot import MusicBot

class TestMetadataCleaning(unittest.TestCase):
    def test_deduplicate_artist_in_title(self):
        artist, title = clean_metadata(
            raw_title="Moby - Natural Blues",
            raw_artist="Moby",
            hint_artist="Moby",
            hint_title="Moby - Natural Blues"
        )
        self.assertEqual(artist, "Moby")
        self.assertEqual(title, "Natural Blues")

    def test_strip_youtube_video_junk(self):
        artist, title = clean_metadata(
            raw_title="Hail Mary (Official Video) [Remastered 4K]",
            raw_artist="2Pac"
        )
        self.assertEqual(artist, "2Pac")
        self.assertEqual(title, "Hail Mary")

    def test_special_characters_in_artist(self):
        artist, title = clean_metadata(
            raw_title="Paris (Official Video)",
            raw_artist="$uicideboy$",
            hint_artist="$uicideboy$",
            hint_title="$uicideboy$ - Paris"
        )
        self.assertEqual(artist, "$uicideboy$")
        self.assertEqual(title, "Paris")

    def test_hyphen_split_format(self):
        artist, title = clean_metadata(
            raw_title="The Weeknd - Blinding Lights (Official Audio)",
            raw_artist="TheWeekndVEVO"
        )
        self.assertEqual(artist, "The Weeknd")
        self.assertEqual(title, "Blinding Lights")

    def test_em_dash_separator(self):
        artist, title = clean_metadata(
            raw_title="Eminem — Without Me",
            raw_artist=""
        )
        self.assertEqual(artist, "Eminem")
        self.assertEqual(title, "Without Me")

    def test_duplicate_artist_prefix(self):
        artist, title = clean_metadata(
            raw_title="Big Sean - Big Sean - I Don't Fuck With You",
            raw_artist="Big Sean"
        )
        self.assertEqual(artist, "Big Sean")
        self.assertEqual(title, "I Don't Fuck With You")

    def test_empty_metadata_fallbacks(self):
        artist, title = clean_metadata("", "", clean_query="Unknown Search")
        self.assertEqual(artist, "Неизвестен")
        self.assertEqual(title, "Unknown Search")

class TestDatabaseCache(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test_bot.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_retrieve_cached_track(self):
        self.db.save_cached_track(
            query="2Pac - Hail Mary",
            artist="2Pac",
            title="Hail Mary",
            duration=312,
            file_id="CQACAgIAAxkBAAI_test_file_id",
            genre="#музыка #hiphop"
        )

        # Lookup by exact query
        found = self.db.get_cached_track("2Pac - Hail Mary")
        self.assertIsNotNone(found)
        self.assertEqual(found["file_id"], "CQACAgIAAxkBAAI_test_file_id")
        self.assertEqual(found["artist"], "2Pac")
        self.assertEqual(found["title"], "Hail Mary")

        # Lookup by fuzzy artist + title
        found_fuzzy = self.db.get_cached_track("2pac hail mary")
        self.assertIsNotNone(found_fuzzy)
        self.assertEqual(found_fuzzy["file_id"], "CQACAgIAAxkBAAI_test_file_id")

    def test_update_cached_tags(self):
        self.db.save_cached_track(
            query="Eminem - Without Me",
            artist="Eminem",
            title="Without Me",
            duration=290,
            file_id="FILE_ID_123"
        )
        self.db.update_cached_tags("FILE_ID_123", "Marshall Mathers", "Without Me (Edited)")
        updated = self.db.get_cached_track("Eminem - Without Me")
        self.assertEqual(updated["artist"], "Marshall Mathers")
        self.assertEqual(updated["title"], "Without Me (Edited)")

class TestCoverArtProcessor(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_crop_and_resize_cover_and_thumb(self):
        from PIL import Image
        # Create a rectangular dummy test image (e.g. 1280x720 16:9 YouTube thumbnail)
        raw_img = self.test_dir / "sample_thumb.jpg"
        im = Image.new("RGB", (1280, 720), color=(255, 0, 0))
        im.save(raw_img)

        cover_path, thumb_path = process_cover_art(self.test_dir)
        self.assertIsNotNone(cover_path)
        self.assertIsNotNone(thumb_path)
        self.assertTrue(cover_path.exists())
        self.assertTrue(thumb_path.exists())

        # Verify cover dimensions (600x600 square)
        with Image.open(cover_path) as c_im:
            self.assertEqual(c_im.size, (600, 600))

        # Verify thumbnail dimensions (320x320 square, <= 200KB)
        with Image.open(thumb_path) as t_im:
            self.assertEqual(t_im.size, (320, 320))
        self.assertLess(thumb_path.stat().st_size, 200 * 1024)

class TestGenreExtraction(unittest.TestCase):
    @patch("requests.get")
    def test_extract_genre_mapped(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "resultCount": 1,
            "results": [{"primaryGenreName": "Hip-Hop/Rap"}]
        }
        mock_get.return_value = mock_resp

        genre = extract_genre("2Pac", "Hail Mary")
        self.assertIn("#hiphop", genre)
        self.assertIn("#rap", genre)

    @patch("requests.get")
    def test_extract_genre_fallback(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        genre = extract_genre("Unknown", "Track")
        self.assertEqual(genre, "#музыка")

class TestLyricsExtraction(unittest.TestCase):
    @patch("requests.get")
    def test_fetch_lyrics_plain(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "plainLyrics": "Makaveli in this\nKilluminati"
        }
        mock_get.return_value = mock_resp

        lyrics = fetch_lyrics("2Pac", "Hail Mary")
        self.assertIsNotNone(lyrics)
        self.assertIn("Makaveli", lyrics)

    @patch("requests.get")
    def test_fetch_lyrics_synced_strips_timestamps(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "plainLyrics": None,
            "syncedLyrics": "[00:15.50] Line one\n[00:20.12] Line two"
        }
        mock_get.return_value = mock_resp

        lyrics = fetch_lyrics("Artist", "Song")
        self.assertIsNotNone(lyrics)
        self.assertNotIn("[00:15.50]", lyrics)
        self.assertIn("Line one", lyrics)

class TestLinkResolution(unittest.TestCase):
    def test_youtube_link_resolution(self):
        res = resolve_music_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertTrue(res["is_link"])
        self.assertEqual(res["source"], "youtube")
        self.assertIn("dQw4w9WgXcQ", res["query"])

    def test_youtube_short_link(self):
        res = resolve_music_link("https://youtu.be/dQw4w9WgXcQ")
        self.assertTrue(res["is_link"])
        self.assertEqual(res["source"], "youtube")

    def test_soundcloud_link(self):
        res = resolve_music_link("https://soundcloud.com/artist/track-name")
        self.assertTrue(res["is_link"])
        self.assertEqual(res["source"], "soundcloud")

    def test_plain_text_not_link(self):
        res = resolve_music_link("Queen - Bohemian Rhapsody")
        self.assertFalse(res["is_link"])
        self.assertEqual(res["source"], "text")

class TestConfig(unittest.TestCase):
    def test_settings_load_defaults(self):
        settings = Settings.load()
        self.assertIsInstance(settings.port, int)
        self.assertIsInstance(settings.max_workers, int)
        self.assertGreaterEqual(settings.max_workers, 1)

class TestWebhookEndpoints(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.load()
        self.settings.shazam_secret = "test_secret_123"
        self.mock_bot = MagicMock(spec=MusicBot)
        self.mock_bot.executor = MagicMock()

    def _create_handler(self, method: str, path: str, headers: dict = None, body: bytes = b""):
        handler = WebhookHandler.__new__(WebhookHandler)
        handler.settings = self.settings
        handler.bot = self.mock_bot
        handler.command = method
        handler.path = path
        handler.headers = headers or {}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 12345)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_health_check_endpoint(self):
        handler = self._create_handler("GET", "/health")
        handler.do_GET()
        handler.send_response.assert_called_with(200)

    def test_get_shazam_unauthorized(self):
        handler = self._create_handler("GET", "/shazam?q=test_query")
        handler.do_GET()
        handler.send_response.assert_called_with(403)

    def test_get_shazam_authorized(self):
        handler = self._create_handler("GET", "/shazam?q=Moby+-+Natural+Blues&token=test_secret_123")
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        self.mock_bot.executor.submit.assert_called_once()

    def test_post_json_payload_authorized(self):
        payload = json.dumps({
            "token": "test_secret_123",
            "artist": "2Pac",
            "title": "Hail Mary"
        }).encode("utf-8")
        headers = {"Content-Length": str(len(payload))}
        handler = self._create_handler("POST", "/shazam", headers=headers, body=payload)
        handler.do_POST()
        handler.send_response.assert_called_with(200)
        self.mock_bot.executor.submit.assert_called_once()

    def test_post_payload_too_large(self):
        large_body = b"A" * (70 * 1024)
        headers = {"Content-Length": str(len(large_body))}
        handler = self._create_handler("POST", "/shazam", headers=headers, body=large_body)
        handler.do_POST()
        handler.send_response.assert_called_with(413)

    def test_missing_params_returns_400(self):
        handler = self._create_handler("GET", "/shazam?token=test_secret_123")
        handler.do_GET()
        handler.send_response.assert_called_with(400)

class TestConcurrencyAndNonBlocking(unittest.TestCase):
    def test_webhook_non_blocking_dispatch(self):
        from concurrent.futures import ThreadPoolExecutor
        real_executor = ThreadPoolExecutor(max_workers=2)
        mock_bot = MagicMock()
        mock_bot.executor = real_executor

        def slow_worker(*args, **kwargs):
            time.sleep(0.5)

        mock_bot.process_search_and_preview = slow_worker

        settings = Settings.load()
        settings.shazam_secret = "test_secret_123"

        handler = WebhookHandler.__new__(WebhookHandler)
        handler.settings = settings
        handler.bot = mock_bot
        handler.client_address = ("127.0.0.1", 12345)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = io.BytesIO()

        start_time = time.time()
        handler.path = "/shazam?q=test+track&token=test_secret_123"
        handler.do_GET()
        duration = time.time() - start_time

        self.assertLess(duration, 0.15)
        handler.send_response.assert_called_with(200)
        real_executor.shutdown(wait=True)

if __name__ == "__main__":
    unittest.main()
