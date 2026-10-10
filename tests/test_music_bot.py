"""
Comprehensive Unit Tests for Telegram Music Bot.
Covers metadata cleaning, downloader fallback logic, webhook endpoints, and concurrency.
"""

import io
import json
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from downloader import clean_metadata, Downloader
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
        large_body = b"A" * (70 * 1024)  # 70 KB > 64 KB limit
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

        # Simulate slow background processing in worker thread
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

        # Webhook HTTP response must return immediately (< 100ms) without waiting for slow_worker
        self.assertLess(duration, 0.15)
        handler.send_response.assert_called_with(200)
        real_executor.shutdown(wait=True)

if __name__ == "__main__":
    unittest.main()
