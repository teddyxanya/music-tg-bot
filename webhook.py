"""
Webhook HTTP Server for Apple Shortcuts and Shazam Integration.
Provides non-blocking endpoints for /shazam and /music.
"""

import re
import json
import hmac
import logging
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional, Tuple

from config import Settings
from bot import MusicBot
from downloader import clean_metadata

logger = logging.getLogger("music_bot.webhook")
MAX_REQUEST_BODY_BYTES = 64 * 1024  # 64 KB protection against memory exhaustion

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class WebhookHandler(BaseHTTPRequestHandler):
    settings: Settings
    bot: MusicBot

    def log_message(self, format, *args):
        # Redirect default BaseHTTPRequestHandler logging to standard logger
        logger.debug("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))

    def _send_json(self, status_code: int, data: dict):
        response_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def _extract_auth_token(self, qs: dict) -> str:
        token = (
            qs.get("token", [""])[0] or
            qs.get("key", [""])[0] or
            self.headers.get("X-Auth-Token", "") or
            self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        )
        return token

    def _validate_auth(self, token: str) -> bool:
        if not self.settings.shazam_secret:
            logger.error("SHAZAM_SECRET_TOKEN is not configured! Denying webhook request.")
            return False
        return hmac.compare_digest(token or "", self.settings.shazam_secret)

    def _parse_params(self, qs: dict) -> Tuple[str, str, str]:
        q = (qs.get("q", [""])[0] or qs.get("track", [""])[0] or qs.get("query", [""])[0]).strip()
        artist = (qs.get("artist", [""])[0] or qs.get("performer", [""])[0] or qs.get("author", [""])[0]).strip()
        title = (qs.get("title", [""])[0] or qs.get("song", [""])[0] or qs.get("name", [""])[0]).strip()

        # Deduplicate and clean metadata
        clean_art, clean_tit = clean_metadata("", "", hint_artist=artist or None, hint_title=title or None, clean_query=q)

        if not q:
            if clean_art and clean_tit and clean_art != "Неизвестен" and clean_tit != "Без названия":
                q = f"{clean_art} - {clean_tit}"
            elif clean_art and clean_art != "Неизвестен":
                q = clean_art
            elif clean_tit and clean_tit != "Без названия":
                q = clean_tit
            elif artist or title:
                q = f"{artist} {title}".strip()

        return q, clean_art if clean_art != "Неизвестен" else artist, clean_tit if clean_tit != "Без названия" else title

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "music-tg-bot"})
            return

        if path in ("/shazam", "/music"):
            token = self._extract_auth_token(qs)
            if not self._validate_auth(token):
                logger.warning(f"Unauthorized GET /shazam from {self.client_address[0]}")
                self._send_json(403, {"ok": False, "error": "Forbidden: invalid or missing token"})
                return

            q, artist, title = self._parse_params(qs)
            if not q:
                self._send_json(400, {"ok": False, "error": "Missing ?q= or ?artist=&title= parameter"})
                return

            # Dispatch task asynchronously to bot thread pool
            self.bot.executor.submit(
                self.bot.process_search_and_preview,
                q,
                hint_artist=artist if artist else None,
                hint_title=title if title else None
            )

            self._send_json(200, {
                "ok": True,
                "status": "processing",
                "query": q,
                "artist": artist,
                "title": title,
                "message": "Track search dispatched. Check Telegram for confirmation preview."
            })
            return

        self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        url_qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/shazam", "/music"):
            content_len = int(self.headers.get('Content-Length', 0))
            if content_len > MAX_REQUEST_BODY_BYTES:
                self._send_json(413, {"ok": False, "error": "Payload Too Large (max 64 KB)"})
                return

            post_body = self.rfile.read(content_len).decode('utf-8', errors='ignore') if content_len > 0 else ""

            body_dict = {}
            if post_body:
                try:
                    body_dict = json.loads(post_body)
                except Exception:
                    body_dict = urllib.parse.parse_qs(post_body)

            # Determine token
            token = self._extract_auth_token(url_qs)
            if not token and isinstance(body_dict, dict):
                token = body_dict.get("token") or body_dict.get("key")
                if isinstance(token, list):
                    token = token[0] if token else ""

            if not self._validate_auth(token):
                logger.warning(f"Unauthorized POST /shazam from {self.client_address[0]}")
                self._send_json(403, {"ok": False, "error": "Forbidden: invalid or missing token"})
                return

            # Combine URL query and body params
            q = ""
            artist = url_qs.get("artist", [""])[0] or url_qs.get("performer", [""])[0]
            title = url_qs.get("title", [""])[0] or url_qs.get("song", [""])[0] or url_qs.get("name", [""])[0]

            if isinstance(body_dict, dict):
                raw_q = body_dict.get("query") or body_dict.get("track") or body_dict.get("q")
                if isinstance(raw_q, list):
                    raw_q = raw_q[0] if raw_q else ""
                q = str(raw_q or "").strip()

                raw_art = body_dict.get("artist") or body_dict.get("performer") or ""
                if isinstance(raw_art, list):
                    raw_art = raw_art[0] if raw_art else ""
                artist = artist or str(raw_art or "").strip()

                raw_tit = body_dict.get("title") or body_dict.get("song") or body_dict.get("name") or ""
                if isinstance(raw_tit, list):
                    raw_tit = raw_tit[0] if raw_tit else ""
                title = title or str(raw_tit or "").strip()

            if not q:
                q = url_qs.get("q", [""])[0] or url_qs.get("track", [""])[0] or url_qs.get("query", [""])[0]

            clean_art, clean_tit = clean_metadata("", "", hint_artist=artist or None, hint_title=title or None, clean_query=q)

            if not q:
                if clean_art and clean_tit and clean_art != "Неизвестен" and clean_tit != "Без названия":
                    q = f"{clean_art} - {clean_tit}"
                elif clean_art and clean_art != "Неизвестен":
                    q = clean_art
                elif clean_tit and clean_tit != "Без названия":
                    q = clean_tit
                elif artist or title:
                    q = f"{artist} {title}".strip()

            if not q:
                self._send_json(400, {"ok": False, "error": "Missing query or artist/title"})
                return

            # Dispatch task asynchronously
            self.bot.executor.submit(
                self.bot.process_search_and_preview,
                q,
                hint_artist=clean_art if clean_art != "Неизвестен" else None,
                hint_title=clean_tit if clean_tit != "Без названия" else None
            )

            self._send_json(200, {
                "ok": True,
                "status": "processing",
                "query": q,
                "artist": clean_art,
                "title": clean_tit,
                "message": "Track search dispatched. Check Telegram for confirmation preview."
            })
            return

        self._send_json(404, {"error": "Not Found"})

def start_webhook_server(settings: Settings, bot: MusicBot) -> ThreadedHTTPServer:
    handler_class = type("BoundWebhookHandler", (WebhookHandler,), {
        "settings": settings,
        "bot": bot
    })
    server = ThreadedHTTPServer((settings.host, settings.port), handler_class)
    logger.info(f"Webhook HTTP server listening on http://{settings.host}:{settings.port}")
    return server
