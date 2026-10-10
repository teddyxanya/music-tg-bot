"""
Telegram Bot Controller.
Handles long-polling, callback queries, non-blocking ThreadPool execution,
audio previewing, channel publishing, connection pooling, and atomic locks.
"""

import os
import re
import json
import time
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional, Set
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from config import Settings
from downloader import Downloader, TrackInfo

logger = logging.getLogger("music_bot.controller")

# Telegram limits
MAX_TELEGRAM_AUDIO_BYTES = 49 * 1024 * 1024  # 49 MB
TRACK_ID_REGEX = re.compile(r"^[a-f0-9]{8}$")

class MusicBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.downloader = Downloader(settings.download_dir)
        self.executor = ThreadPoolExecutor(max_workers=settings.max_workers, thread_name_prefix="MusicWorker")
        self.running = False

        # Concurrency safety: atomic active tasks set to prevent duplicate publishes / race conditions
        self._lock = threading.Lock()
        self.active_track_actions: Set[str] = set()

        # Reusable requests session with connection pooling and retries
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(
            pool_connections=settings.max_workers + 4,
            pool_maxsize=settings.max_workers * 4,
            max_retries=retries
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _api_call(self, method: str, json_data: dict = None, files: dict = None, data: dict = None, timeout: int = 30) -> dict:
        url = f"https://api.telegram.org/bot{self.settings.bot_token}/{method}"
        try:
            if files:
                r = self.session.post(url, data=data, files=files, timeout=timeout)
            else:
                r = self.session.post(url, json=json_data, timeout=timeout)
            return r.json()
        except Exception as e:
            # Mask token in logs
            safe_err = str(e).replace(self.settings.bot_token, "BOT_TOKEN_REDACTED")
            logger.error(f"Telegram API call {method} failed: {safe_err}")
            return {"ok": False, "description": safe_err}

    def send_message(self, chat_id: str, text: str, reply_to_message_id: Optional[int] = None, parse_mode: str = "HTML") -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        return self._api_call("sendMessage", json_data=payload)

    def delete_message(self, chat_id: str, message_id: int):
        self._api_call("deleteMessage", json_data={"chat_id": chat_id, "message_id": message_id}, timeout=5)

    def edit_message(self, chat_id: str, message_id: int, text: str, parse_mode: str = "HTML"):
        self._api_call("editMessageText", json_data={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }, timeout=10)

    def cleanup_expired_pending(self):
        """Purges pending download directories older than configured expiration time."""
        now = time.time()
        try:
            for item in self.settings.download_dir.iterdir():
                if item.is_dir() and (now - item.stat().st_mtime) > self.settings.temp_expiration_sec:
                    # Do not delete if currently locked in action
                    with self._lock:
                        if item.name in self.active_track_actions:
                            continue
                    shutil.rmtree(item, ignore_errors=True)
                    logger.info(f"Cleaned up expired track folder: {item.name}")
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")

    def is_authorized(self, sender_id: str) -> bool:
        """Returns True if sender is authorized (owner or owner unset)."""
        if not self.settings.owner_id:
            return True
        return str(sender_id) == str(self.settings.owner_id)

    def process_search_and_preview(
        self,
        query: str,
        target_chat_id: Optional[str] = None,
        reply_to_msg_id: Optional[int] = None,
        hint_artist: Optional[str] = None,
        hint_title: Optional[str] = None
    ):
        """
        Executes search & download in thread pool, sends preview MP3 to user PM with action buttons.
        """
        chat_id = target_chat_id or self.settings.chat_id or self.settings.owner_id
        if not chat_id:
            logger.error("No valid chat_id available to send preview")
            return

        clean_query = query.strip()
        status_msg = self.send_message(
            chat_id,
            f"🔎 <b>Ищу и скачиваю трек:</b>\n<code>{clean_query}</code>...",
            reply_to_message_id=reply_to_msg_id
        )
        status_msg_id = status_msg.get("result", {}).get("message_id") if status_msg.get("ok") else None

        try:
            track = self.downloader.download_track(clean_query, hint_artist=hint_artist, hint_title=hint_title)

            # Check Telegram file size limit
            file_size = track.mp3_path.stat().st_size
            if file_size > MAX_TELEGRAM_AUDIO_BYTES:
                raise Exception(f"Файл трека слишком большой ({file_size // (1024*1024)} МБ). Лимит Telegram — 50 МБ.")

            # Metadata for callback button handling
            track_meta = {
                "id": track.id,
                "title": track.title,
                "artist": track.artist,
                "duration": track.duration,
                "mp3_path": str(track.mp3_path),
                "thumb_path": str(track.thumb_path) if track.thumb_path else None,
                "chat_id": chat_id,
                "status_msg_id": status_msg_id,
                "source": track.source,
                "folder_path": str(track.folder_path)
            }
            (track.folder_path / "meta.json").write_text(json.dumps(track_meta, ensure_ascii=False, indent=2), encoding="utf-8")

            m, s = divmod(track.duration, 60)
            dur_str = f"{m}:{s:02d}"

            source_icon = "🎥 YouTube" if track.source == "youtube" else "☁️ SoundCloud"
            caption = (
                f"🎧 <b>Найден трек ({source_icon}):</b>\n"
                f"🎵 <b>{track.artist} — {track.title}</b>\n"
                f"⏱ Длительность: <code>{dur_str}</code>\n\n"
                f"Опубликовать в канале «Свалка треков»?"
            )
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Закинуть в канал", "callback_data": f"pub:{track.id}"},
                        {"text": "❌ Отмена", "callback_data": f"del:{track.id}"}
                    ]
                ]
            }

            # Safely open files with context managers to prevent Windows file descriptor leaks
            upload_res = None
            with open(track.mp3_path, 'rb') as audio_file:
                files = {'audio': (track.mp3_path.name, audio_file, 'audio/mpeg')}
                data = {
                    'chat_id': str(chat_id),
                    'title': track.title,
                    'performer': track.artist,
                    'duration': track.duration,
                    'caption': caption,
                    'parse_mode': 'HTML',
                    'reply_markup': json.dumps(reply_markup)
                }

                if track.thumb_path and track.thumb_path.exists():
                    with open(track.thumb_path, 'rb') as thumb_file:
                        files['thumbnail'] = (track.thumb_path.name, thumb_file, 'image/jpeg')
                        upload_res = self._api_call("sendAudio", files=files, data=data, timeout=90)
                else:
                    upload_res = self._api_call("sendAudio", files=files, data=data, timeout=90)

            # Delete searching... message once preview is delivered
            if status_msg_id:
                self.delete_message(chat_id, status_msg_id)

            if not upload_res.get("ok"):
                logger.error(f"Failed to send audio preview: {upload_res}")
                self.send_message(chat_id, f"❌ Ошибка отправки аудио в Telegram: {upload_res.get('description')}")

        except Exception as e:
            logger.error(f"Error processing music '{clean_query}': {e}")
            fail_text = f"❌ Не удалось найти или скачать трек: <i>{clean_query}</i>\nОшибка: {e}"
            if status_msg_id:
                self.edit_message(chat_id, status_msg_id, fail_text)
            else:
                self.send_message(chat_id, fail_text)

    def handle_callback_query(self, cb: dict):
        """Validates track_id, checks authorization, prevents race conditions, dispatches task."""
        cb_id = cb["id"]
        cb_data = cb.get("data", "")
        msg = cb.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        msg_id = msg.get("message_id")
        from_user_id = str(cb.get("from", {}).get("id", ""))

        # Authorization check
        if not self.is_authorized(from_user_id):
            self._api_call("answerCallbackQuery", json_data={
                "callback_query_id": cb_id,
                "text": "⛔ У вас нет доступа к управлению этим ботом",
                "show_alert": True
            })
            return

        if not cb_data or ":" not in cb_data:
            return

        action, track_id = cb_data.split(":", 1)

        # 1. Path Traversal & format validation
        if not TRACK_ID_REGEX.fullmatch(track_id):
            logger.warning(f"Potential path traversal or invalid track_id: {track_id}")
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "⚠️ Ошибка ID трека"})
            return

        base_download_dir = self.settings.download_dir.resolve()
        track_dir = (base_download_dir / track_id).resolve()
        if not track_dir.is_relative_to(base_download_dir):
            logger.warning(f"Path traversal detected: {track_dir}")
            return

        meta_path = track_dir / "meta.json"

        # 2. Race condition protection: ensure only ONE action per track runs at any moment
        with self._lock:
            if track_id in self.active_track_actions:
                self._api_call("answerCallbackQuery", json_data={
                    "callback_query_id": cb_id,
                    "text": "⏳ Запрос уже обрабатывается...",
                    "show_alert": False
                })
                return
            self.active_track_actions.add(track_id)

        if action == "del":
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "❌ Добавление отменено"})
            self.executor.submit(self._task_cancel_track, track_id, track_dir, meta_path, chat_id, msg_id)
            return

        if action == "pub":
            if not meta_path.exists():
                with self._lock:
                    self.active_track_actions.discard(track_id)
                self._api_call("answerCallbackQuery", json_data={
                    "callback_query_id": cb_id,
                    "text": "⚠️ Трек уже опубликован или удалён",
                    "show_alert": True
                })
                self._api_call("editMessageCaption", json_data={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "caption": "⚠️ <b>Срок ожидания истёк или трек уже удалён.</b>",
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": []}
                })
                return

            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "🚀 Публикую в канал..."})
            self.executor.submit(self._task_publish_track, track_id, track_dir, meta_path, chat_id, msg_id)

    def _task_cancel_track(self, track_id: str, track_dir: Path, meta_path: Path, chat_id: str, msg_id: int):
        try:
            title = "трек"
            artist = ""
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    title = meta.get("title", "")
                    artist = meta.get("artist", "")
                except Exception:
                    pass

            shutil.rmtree(track_dir, ignore_errors=True)
            self.delete_message(chat_id, msg_id)
            name_str = f"{artist} — {title}" if artist else title
            self.send_message(chat_id, f"❌ <b>Отменено:</b> <s>{name_str}</s>\n<i>Временный MP3 удалён.</i>")
        finally:
            with self._lock:
                self.active_track_actions.discard(track_id)

    def _task_publish_track(self, track_id: str, track_dir: Path, meta_path: Path, chat_id: str, msg_id: int):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = meta["title"]
            artist = meta["artist"]
            duration = meta.get("duration", 0)
            mp3_path = Path(meta["mp3_path"])
            thumb_path = Path(meta["thumb_path"]) if meta.get("thumb_path") else None

            if not self.settings.channel_id:
                raise ValueError("MUSIC_CHANNEL_ID не задан в .env")

            # Check file size limit before channel upload
            file_size = mp3_path.stat().st_size
            if file_size > MAX_TELEGRAM_AUDIO_BYTES:
                raise Exception(f"Файл слишком большой ({file_size // (1024*1024)} МБ) для отправки в Telegram.")

            # Upload MP3 directly to music channel with safe context managers
            res = None
            with open(mp3_path, 'rb') as audio_file:
                files = {'audio': (mp3_path.name, audio_file, 'audio/mpeg')}
                caption = f"🎵 <b>{artist} — {title}</b>\n\n#музыка"
                data = {
                    'chat_id': self.settings.channel_id,
                    'title': title,
                    'performer': artist,
                    'duration': duration,
                    'caption': caption,
                    'parse_mode': 'HTML'
                }

                if thumb_path and thumb_path.exists():
                    with open(thumb_path, 'rb') as thumb_file:
                        files['thumbnail'] = (thumb_path.name, thumb_file, 'image/jpeg')
                        res = self._api_call("sendAudio", files=files, data=data, timeout=90)
                else:
                    res = self._api_call("sendAudio", files=files, data=data, timeout=90)

            if not res.get("ok"):
                raise Exception(res.get("description", "Неизвестная ошибка Telegram"))

            channel_msg_id = res['result']['message_id']
            chat_obj = res['result'].get('chat', {})
            chat_uname = chat_obj.get('username')

            if chat_uname:
                channel_link = f"https://t.me/{chat_uname}/{channel_msg_id}"
            else:
                # Remove only the leading -100 prefix safely
                c_id_clean = re.sub(r'^-100', '', str(self.settings.channel_id)).lstrip('-')
                channel_link = f"https://t.me/c/{c_id_clean}/{channel_msg_id}"

            # Edit preview message in PM to confirm publishing
            self._api_call("editMessageCaption", json_data={
                "chat_id": chat_id,
                "message_id": msg_id,
                "caption": (
                    f"✅ <b>Опубликовано в «Свалка треков»!</b>\n\n"
                    f"🎵 <b>{artist} — {title}</b>\n"
                    f"🔗 <a href=\"{channel_link}\">Перейти к треку в канале</a>"
                ),
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []}
            })

            # Cleanup temp folder
            shutil.rmtree(track_dir, ignore_errors=True)
            logger.info(f"Published track {artist} - {title} to channel {self.settings.channel_id}")

        except Exception as e:
            logger.error(f"Error publishing track: {e}")
            self.send_message(chat_id, f"❌ Ошибка публикации трека в канал: {e}")
        finally:
            with self._lock:
                self.active_track_actions.discard(track_id)

    def handle_message(self, msg: dict):
        chat_id = str(msg["chat"]["id"])
        raw_text = msg.get("text", "").strip()
        sender_id = str(msg.get("from", {}).get("id", ""))

        # Authorization check
        if not self.is_authorized(sender_id):
            logger.warning(f"Unauthorized message attempt from user {sender_id}")
            self.send_message(chat_id, "⛔ У вас нет доступа к управлению этим ботом.")
            return

        # Auto-bind default chat_id if not configured
        if not self.settings.chat_id and not chat_id.startswith("-"):
            self.settings.chat_id = chat_id
            logger.info(f"Auto-bound default chat ID: {chat_id}")

        parts = raw_text.split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:] if len(parts) > 1 else []

        if cmd in ("/start", "/help"):
            help_text = (
                "🎵 <b>Telegram Music Bot & Downloader</b>\n\n"
                "Отправьте боту любое название трека или исполнителя (или используйте <code>/music &lt;запрос&gt;</code>).\n\n"
                "• Поиск на YouTube с обходом 403 / 18+ и автопереходом на SoundCloud\n"
                "• MP3 320 kbps + обложка + ID3 теги\n"
                "• Предпросмотр перед отправкой с кнопками подтверждения\n"
                "• Интеграция с Apple Командами и Shazam через Webhook API"
            )
            self.send_message(chat_id, help_text)
            return

        if cmd == "/status":
            num_workers = self.settings.max_workers
            disk_items = len(list(self.settings.download_dir.glob("*")))
            with self._lock:
                active_count = len(self.active_track_actions)
            status_text = (
                "📊 <b>Статус Music Bot</b>\n\n"
                f"⚙️ Воркеров: <code>{num_workers}</code> (активных задач: <code>{active_count}</code>)\n"
                f"📁 Временных папок: <code>{disk_items}</code>\n"
                f"📢 Канал: <code>{self.settings.channel_id or 'Не настроен'}</code>\n"
                f"🌐 Webhook порт: <code>{self.settings.port}</code>"
            )
            self.send_message(chat_id, status_text)
            return

        if cmd in ("/music", "/song", "/track", "/m"):
            query = " ".join(args).strip()
            if not query:
                self.send_message(chat_id, "⚠️ Укажите название трека:\n<code>/music 2Pac Hail Mary</code>")
            else:
                self.executor.submit(
                    self.process_search_and_preview,
                    query,
                    chat_id,
                    msg.get("message_id")
                )
            return

        # Plain text in private chat
        if not cmd.startswith("/") and not chat_id.startswith("-"):
            query = raw_text.strip()
            if query:
                self.executor.submit(
                    self.process_search_and_preview,
                    query,
                    chat_id,
                    msg.get("message_id")
                )

    def run_polling(self):
        """Main long-polling loop with self-healing and webhook conflict resolution."""
        logger.info("Starting Telegram Bot polling loop...")
        self.running = True
        offset = 0

        # Clear any active webhook so getUpdates works
        self._api_call("deleteWebhook", timeout=10)

        last_cleanup = time.time()

        while self.running:
            try:
                # Periodic temporary cleanup every 15 minutes
                if time.time() - last_cleanup > 900:
                    self.cleanup_expired_pending()
                    last_cleanup = time.time()

                r = self.session.get(
                    f"https://api.telegram.org/bot{self.settings.bot_token}/getUpdates?offset={offset}&timeout=20",
                    timeout=30
                )

                if r.status_code == 409:
                    logger.warning("Conflict: Webhook or other instance active. Deleting webhook and waiting...")
                    self._api_call("deleteWebhook", timeout=10)
                    time.sleep(3)
                    continue

                if r.status_code != 200:
                    time.sleep(2)
                    continue

                data = r.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    cb = update.get("callback_query")
                    if cb:
                        self.handle_callback_query(cb)
                        continue

                    msg = update.get("message")
                    if msg:
                        self.handle_message(msg)

            except Exception as e:
                logger.error(f"Polling loop exception: {e}")
                time.sleep(3)

    def stop(self):
        logger.info("Stopping MusicBot...")
        self.running = False
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.session.close()
