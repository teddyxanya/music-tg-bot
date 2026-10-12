"""
Telegram Bot Controller.
Handles long-polling, callback queries, non-blocking ThreadPool execution,
instant SQLite file_id caching, audio previewing, channel publishing with genre tags,
alternative versions selector, tag editing, lyrics display, and direct link parsing.
"""

import os
import re
import json
import time
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional, Set, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from config import Settings
from database import Database
from downloader import (
    Downloader,
    TrackInfo,
    apply_id3_tags,
    clean_metadata,
    fetch_lyrics,
    resolve_music_link
)

logger = logging.getLogger("music_bot.controller")

# Telegram limits
MAX_TELEGRAM_AUDIO_BYTES = 49 * 1024 * 1024  # 49 MB
TRACK_ID_REGEX = re.compile(r"^[a-f0-9]{8}$")

class MusicBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.downloader = Downloader(settings.download_dir)
        db_path = settings.download_dir.parent / "data" / "bot.db"
        self.db = Database(db_path)
        self.executor = ThreadPoolExecutor(max_workers=settings.max_workers, thread_name_prefix="MusicWorker")
        self.running = False

        # Concurrency safety: atomic active tasks set to prevent duplicate publishes / race conditions
        self._lock = threading.Lock()
        self.active_track_actions: Set[str] = set()
        self.user_states: Dict[str, Dict[str, Any]] = {}

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
            safe_err = str(e).replace(self.settings.bot_token, "BOT_TOKEN_REDACTED")
            logger.error(f"Telegram API call {method} failed: {safe_err}")
            return {"ok": False, "description": safe_err}

    def send_message(self, chat_id: str, text: str, reply_to_message_id: Optional[int] = None, parse_mode: str = "HTML", reply_markup: Optional[dict] = None) -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._api_call("sendMessage", json_data=payload)

    def delete_message(self, chat_id: str, message_id: int):
        self._api_call("deleteMessage", json_data={"chat_id": chat_id, "message_id": message_id}, timeout=5)

    def edit_message(self, chat_id: str, message_id: int, text: str, parse_mode: str = "HTML", reply_markup: Optional[dict] = None):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._api_call("editMessageText", json_data=payload, timeout=10)

    def cleanup_expired_pending(self):
        """Purges pending download directories older than configured expiration time."""
        now = time.time()
        try:
            for item in self.settings.download_dir.iterdir():
                if item.is_dir() and (now - item.stat().st_mtime) > self.settings.temp_expiration_sec:
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

    def forward_to_sub_server(self, cmd: str, args: List[str], sender_id: str = "", chat_id: str = "") -> Optional[dict]:
        """Forwards proxy/subscription commands to the local e0f sub server daemon."""
        try:
            url = f"{self.settings.sub_server_url.rstrip('/')}/api/tg_command"
            payload = {
                "cmd": cmd,
                "args": args,
                "sender_user_id": sender_id,
                "chat_id": chat_id
            }
            resp = requests.post(url, json=payload, timeout=25)
            if resp.status_code == 200:
                return resp.json()
            logger.error(f"Sub-server returned HTTP {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.warning(f"Could not connect to sub-server ({self.settings.sub_server_url}): {e}")
        return None

    def process_search_and_preview(
        self,
        query: str,
        target_chat_id: Optional[str] = None,
        reply_to_msg_id: Optional[int] = None,
        hint_artist: Optional[str] = None,
        hint_title: Optional[str] = None,
        direct_url: Optional[str] = None
    ):
        """
        Executes search & download in thread pool, checks instant file_id cache,
        sends preview MP3 with interactive buttons (Publish, Cancel, Versions, Lyrics, Edit tags).
        Falls back to fresh download if cached file_id fails.
        """
        chat_id = target_chat_id or self.settings.chat_id or self.settings.owner_id
        if not chat_id:
            logger.error("No valid chat_id available to send preview")
            return

        clean_query = query.strip()

        # Step 1: Check instant SQLite cache (file_id) for instant 0.1s replay
        if not direct_url:
            cached = self.db.get_cached_track(clean_query, hint_artist, hint_title)
            if cached and cached.get("file_id"):
                logger.info(f"Instant cache hit for '{clean_query}': file_id {cached['file_id']}")
                m, s = divmod(cached["duration"], 60)
                dur_str = f"{m}:{s:02d}"
                caption = (
                    f"⚡ <b>Мгновенно из кэша (0.1 сек):</b>\n"
                    f"🎵 <b>{cached['artist']} — {cached['title']}</b>\n"
                    f"⏱ Длительность: <code>{dur_str}</code>\n"
                    f"🏷 Жанр: {cached.get('genre') or '#музыка'}\n\n"
                    f"Опубликовать в канале «Свалка треков»?"
                )
                reply_markup = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Закинуть в канал", "callback_data": f"pubcached:{cached['id']}"},
                            {"text": "📝 Текст песни", "callback_data": f"clyr:{cached['id']}"}
                        ]
                    ]
                }
                send_res = self._api_call("sendAudio", json_data={
                    "chat_id": str(chat_id),
                    "audio": cached["file_id"],
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup
                })
                if send_res.get("ok"):
                    return
                logger.warning(f"Cached file_id expired/invalid for '{clean_query}'. Falling back to fresh download...")

        # Step 2: Inform user about search & download
        status_msg = self.send_message(
            chat_id,
            f"🔎 <b>Ищу и скачиваю трек:</b>\n<code>{clean_query}</code>...",
            reply_to_message_id=reply_to_msg_id
        )
        status_msg_id = status_msg.get("result", {}).get("message_id") if status_msg.get("ok") else None

        try:
            track = self.downloader.download_track(
                clean_query,
                hint_artist=hint_artist,
                hint_title=hint_title,
                direct_url=direct_url
            )

            # Check Telegram file size limit
            file_size = track.mp3_path.stat().st_size
            if file_size > MAX_TELEGRAM_AUDIO_BYTES:
                raise Exception(f"Файл трека слишком большой ({file_size // (1024*1024)} МБ). Лимит Telegram — 50 МБ.")

            m, s = divmod(track.duration, 60)
            dur_str = f"{m}:{s:02d}"

            source_icon = "🎥 YouTube" if track.source == "youtube" else "☁️ SoundCloud"
            caption = (
                f"🎧 <b>Найден трек ({source_icon}):</b>\n"
                f"🎵 <b>{track.artist} — {track.title}</b>\n"
                f"⏱ Длительность: <code>{dur_str}</code>\n"
                f"🏷 Жанр: {track.genre}\n\n"
                f"Опубликовать в канале «Свалка треков»?"
            )

            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Закинуть в канал", "callback_data": f"pub:{track.id}"},
                        {"text": "❌ Отмена", "callback_data": f"del:{track.id}"}
                    ],
                    [
                        {"text": "🔀 Другие версии", "callback_data": f"alt:{track.id}"},
                        {"text": "📝 Текст песни", "callback_data": f"lyr:{track.id}"}
                    ],
                    [
                        {"text": "✏️ Изменить теги", "callback_data": f"edit:{track.id}"}
                    ]
                ]
            }

            # Metadata for callback button handling
            track_meta = {
                "id": track.id,
                "title": track.title,
                "artist": track.artist,
                "duration": track.duration,
                "mp3_path": str(track.mp3_path),
                "cover_path": str(track.cover_path) if track.cover_path else None,
                "thumb_path": str(track.thumb_path) if track.thumb_path else None,
                "chat_id": chat_id,
                "status_msg_id": status_msg_id,
                "source": track.source,
                "genre": track.genre,
                "query": clean_query,
                "folder_path": str(track.folder_path)
            }
            (track.folder_path / "meta.json").write_text(json.dumps(track_meta, ensure_ascii=False, indent=2), encoding="utf-8")

            # Upload audio with thumbnail
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

            # Delete searching... status message once delivered
            if status_msg_id:
                self.delete_message(chat_id, status_msg_id)

            if upload_res.get("ok"):
                # Save uploaded file_id into meta.json and SQLite cache
                result_audio = upload_res.get("result", {}).get("audio", {})
                file_id = result_audio.get("file_id")
                if file_id:
                    track_meta["file_id"] = file_id
                    (track.folder_path / "meta.json").write_text(json.dumps(track_meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    self.db.save_cached_track(
                        clean_query,
                        track.artist,
                        track.title,
                        track.duration,
                        file_id,
                        genre=track.genre
                    )
            else:
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

        if not self.is_authorized(from_user_id):
            self._api_call("answerCallbackQuery", json_data={
                "callback_query_id": cb_id,
                "text": "⛔ У вас нет доступа к управлению этим ботом",
                "show_alert": True
            })
            return

        if not cb_data or ":" not in cb_data:
            return

        action, param = cb_data.split(":", 1)

        # 1. Dismiss notification
        if action == "dismiss":
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id})
            self.delete_message(chat_id, msg_id)
            return

        # 2. Cached track actions (race-condition protected)
        if action == "pubcached":
            if not param.isdigit():
                return
            cache_action_key = f"cache_{param}"
            with self._lock:
                if cache_action_key in self.active_track_actions:
                    self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "⏳ Уже публикуется..."})
                    return
                self.active_track_actions.add(cache_action_key)
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "🚀 Публикую из кэша..."})
            self.executor.submit(self._task_publish_cached, int(param), chat_id, msg_id, cache_action_key)
            return

        if action == "clyr":
            if not param.isdigit():
                return
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "🔍 Ищу текст песни..."})
            self.executor.submit(self._task_show_cached_lyrics, int(param), chat_id)
            return

        # 3. Get alternative version by video ID
        if action == "getalt":
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "⏳ Загружаю выбранную версию..."})
            video_url = f"https://www.youtube.com/watch?v={param}"
            self.executor.submit(self.process_search_and_preview, video_url, chat_id, None, None, None, video_url)
            return

        track_id = param

        # 4. Path traversal & format validation for local track folder
        if not TRACK_ID_REGEX.fullmatch(track_id):
            logger.warning(f"Invalid track_id: {track_id}")
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "⚠️ Ошибка ID трека"})
            return

        base_download_dir = self.settings.download_dir.resolve()
        track_dir = (base_download_dir / track_id).resolve()
        if not track_dir.is_relative_to(base_download_dir):
            logger.warning(f"Path traversal detected: {track_dir}")
            return

        meta_path = track_dir / "meta.json"

        # Race condition protection: ensure only ONE action per track runs at any moment
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
            return

        if action == "alt":
            with self._lock:
                self.active_track_actions.discard(track_id)
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "🔀 Ищу другие версии..."})
            self.executor.submit(self._task_show_versions, track_id, meta_path, chat_id, msg_id)
            return

        if action == "lyr":
            with self._lock:
                self.active_track_actions.discard(track_id)
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "📝 Ищу слова песни..."})
            self.executor.submit(self._task_show_lyrics, track_id, meta_path, chat_id)
            return

        if action == "edit":
            with self._lock:
                self.active_track_actions.discard(track_id)
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id})
            self.user_states[chat_id] = {
                "action": "edit_tags",
                "track_id": track_id,
                "msg_id": msg_id
            }
            self.send_message(
                chat_id,
                "✏️ <b>Редактирование тегов:</b>\n"
                "Отправьте новые теги в ответном сообщении в формате:\n"
                "<code>Исполнитель - Название</code>\n\n"
                "<i>Например: Eminem - Without Me</i>"
            )
            return

        with self._lock:
            self.active_track_actions.discard(track_id)

    def _task_show_versions(self, track_id: str, meta_path: Path, chat_id: str, msg_id: int):
        try:
            if not meta_path.exists():
                self.send_message(chat_id, "⚠️ Метаданные трека не найдены.")
                return

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            query = f"{meta['artist']} {meta['title']}"
            versions = self.downloader.search_versions(query, limit=3)

            if not versions:
                self.send_message(chat_id, f"⚠️ Других версий для <b>{query}</b> не найдено.")
                return

            buttons = []
            for idx, v in enumerate(versions, 1):
                m, s = divmod(v["duration"], 60)
                dur_str = f"{m}:{s:02d}"
                short_title = f"{v['artist']} - {v['title']}"
                if len(short_title) > 35:
                    short_title = short_title[:32] + "..."
                buttons.append([
                    {"text": f"▶️ {idx}. {short_title} ({dur_str})", "callback_data": f"getalt:{v['id']}"}
                ])
            buttons.append([{"text": "❌ Закрыть список", "callback_data": "dismiss:0"}])

            self.send_message(
                chat_id,
                f"🔀 <b>Найдены другие версии трека:</b>\n<i>{meta['artist']} — {meta['title']}</i>\n\n"
                "Нажмите на нужную версию для загрузки:",
                reply_markup={"inline_keyboard": buttons}
            )
        except Exception as e:
            logger.error(f"Error showing versions: {e}")
            self.send_message(chat_id, f"❌ Ошибка поиска версий: {e}")

    def _task_show_lyrics(self, track_id: str, meta_path: Path, chat_id: str):
        try:
            if not meta_path.exists():
                return
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            artist = meta["artist"]
            title = meta["title"]

            lyrics = fetch_lyrics(artist, title)
            if not lyrics:
                self.send_message(chat_id, f"📝 Текст песни для <b>{artist} — {title}</b> не найден в базе.")
                return

            if len(lyrics) > 3800:
                lyrics = lyrics[:3800] + "\n\n... (текст сокращён)"

            text = (
                f"📝 <b>Текст песни:</b>\n"
                f"🎵 <b>{artist} — {title}</b>\n\n"
                f"{lyrics}"
            )
            reply_markup = {
                "inline_keyboard": [[{"text": "❌ Закрыть текст", "callback_data": "dismiss:0"}]]
            }
            self.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Error fetching lyrics: {e}")
            self.send_message(chat_id, f"❌ Ошибка получения текста песни: {e}")

    def _task_show_cached_lyrics(self, cached_id: int, chat_id: str):
        try:
            row = self.db.get_track_by_id(cached_id)
            if not row:
                return
            artist, title = row["artist"], row["title"]
            lyrics = fetch_lyrics(artist, title)
            if not lyrics:
                self.send_message(chat_id, f"📝 Текст песни для <b>{artist} — {title}</b> не найден в базе.")
                return

            if len(lyrics) > 3800:
                lyrics = lyrics[:3800] + "\n\n... (текст сокращён)"

            text = (
                f"📝 <b>Текст песни:</b>\n"
                f"🎵 <b>{artist} — {title}</b>\n\n"
                f"{lyrics}"
            )
            reply_markup = {
                "inline_keyboard": [[{"text": "❌ Закрыть текст", "callback_data": "dismiss:0"}]]
            }
            self.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Error showing cached lyrics: {e}")

    def _task_publish_cached(self, cached_id: int, chat_id: str, msg_id: int, cache_action_key: str):
        try:
            row = self.db.get_track_by_id(cached_id)
            if not row:
                self.send_message(chat_id, "⚠️ Запись в кэше не найдена.")
                return

            artist = row["artist"]
            title = row["title"]
            duration = row["duration"]
            file_id = row["file_id"]
            genre = row["genre"] or "#музыка"

            if not self.settings.channel_id:
                raise ValueError("MUSIC_CHANNEL_ID не задан в .env")

            caption = f"🎵 <b>{artist} — {title}</b>\n\n{genre}"
            res = self._api_call("sendAudio", json_data={
                "chat_id": str(self.settings.channel_id),
                "audio": file_id,
                "title": title,
                "performer": artist,
                "duration": duration,
                "caption": caption,
                "parse_mode": "HTML"
            })

            if not res.get("ok"):
                raise Exception(res.get("description", "Неизвестная ошибка Telegram"))

            channel_msg_id = res['result']['message_id']
            chat_obj = res['result'].get('chat', {})
            chat_uname = chat_obj.get('username')

            if chat_uname:
                channel_link = f"https://t.me/{chat_uname}/{channel_msg_id}"
            else:
                c_id_clean = re.sub(r'^-100', '', str(self.settings.channel_id)).lstrip('-')
                channel_link = f"https://t.me/c/{c_id_clean}/{channel_msg_id}"

            # Update channel link in database
            self.db.update_channel_info(cached_id, channel_msg_id, channel_link)

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
        except Exception as e:
            logger.error(f"Error publishing cached track: {e}")
            self.send_message(chat_id, f"❌ Ошибка публикации трека из кэша: {e}")
        finally:
            with self._lock:
                self.active_track_actions.discard(cache_action_key)

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
            genre = meta.get("genre") or "#музыка"

            if not self.settings.channel_id:
                raise ValueError("MUSIC_CHANNEL_ID не задан в .env")

            # Check file size limit before channel upload
            file_size = mp3_path.stat().st_size
            if file_size > MAX_TELEGRAM_AUDIO_BYTES:
                raise Exception(f"Файл слишком большой ({file_size // (1024*1024)} МБ) для отправки в Telegram.")

            # Upload MP3 directly to music channel with genre hashtags
            res = None
            with open(mp3_path, 'rb') as audio_file:
                files = {'audio': (mp3_path.name, audio_file, 'audio/mpeg')}
                caption = f"🎵 <b>{artist} — {title}</b>\n\n{genre}"
                data = {
                    'chat_id': str(self.settings.channel_id),
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
            file_id = res['result']['audio']['file_id']
            chat_obj = res['result'].get('chat', {})
            chat_uname = chat_obj.get('username')

            if chat_uname:
                channel_link = f"https://t.me/{chat_uname}/{channel_msg_id}"
            else:
                c_id_clean = re.sub(r'^-100', '', str(self.settings.channel_id)).lstrip('-')
                channel_link = f"https://t.me/c/{c_id_clean}/{channel_msg_id}"

            # Save / update in SQLite cache
            self.db.save_cached_track(
                meta.get("query", f"{artist} {title}"),
                artist,
                title,
                duration,
                file_id,
                channel_msg_id=channel_msg_id,
                channel_link=channel_link,
                genre=genre
            )

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

        if not self.is_authorized(sender_id):
            logger.warning(f"Unauthorized message attempt from user {sender_id}")
            self.send_message(chat_id, "⛔ У вас нет доступа к управлению этим ботом.")
            return

        # Auto-bind default chat_id if not configured
        if not self.settings.chat_id and not chat_id.startswith("-"):
            self.settings.chat_id = chat_id
            logger.info(f"Auto-bound default chat ID: {chat_id}")

        # Check conversational states (tag editing) asynchronously
        if chat_id in self.user_states:
            state = self.user_states.pop(chat_id)
            if state.get("action") == "edit_tags":
                self.executor.submit(self._task_handle_edit_tags, chat_id, raw_text, state)
                return

        parts = raw_text.split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:] if len(parts) > 1 else []

        if cmd in ("/start", "/help"):
            help_text = (
                "🤖 <b>Единый бот: Прокси-чекер & Музыка</b>\n\n"
                "🌐 <b>Управление прокси и подписками (Gemini):</b>\n"
                "• 📊 <code>/status</code> — Общий статус серверов, кэша и Gemini\n"
                "• 📋 <code>/profiles</code> — Список всех профилей и подписок Shadowrocket\n"
                "• ➕ <code>/newprofile &lt;имя&gt;</code> — Создать профиль и выделить AWG-серверы\n"
                "• ➖ <code>/delprofile &lt;имя&gt;</code> — Удалить профиль и освободить слоты\n"
                "• 🔄 <code>/sync</code> — Принудительная синхронизация с e0f.cx\n"
                "• 🔎 <code>/check</code> — Запуск теста Gemini серверов (Web + API)\n\n"
                "🎵 <b>Музыкальный плеер и загрузчик:</b>\n"
                "• Отправьте название трека или ссылку (YouTube, SoundCloud, Spotify, Apple, Яндекс)\n"
                "• <code>/music &lt;название&gt;</code> — Скачать MP3 (320 kbps, обложка 1:1, EBU R128)\n"
                "• <code>/album &lt;ссылка&gt;</code> — Пакетное скачивание плейлиста или альбома\n"
                "• <code>/lyrics &lt;трек&gt;</code> — Поиск и показ слов песни\n"
                "• <code>/mstatus</code> — Детальный статус только музыкального модуля"
            )
            self.send_message(chat_id, help_text)
            return

        if cmd == "/status":
            num_workers = self.settings.max_workers
            disk_items = len(list(self.settings.download_dir.glob("*")))
            with self._lock:
                active_count = len(self.active_track_actions)
            cache_count = self.db.count_cached_tracks()

            music_block = (
                "🎵 <b>Статус Music Bot:</b>\n"
                f"⚙️ Воркеров: <code>{num_workers}</code> (активных задач: <code>{active_count}</code>)\n"
                f"⚡ Треков в кэше: <code>{cache_count}</code>\n"
                f"📁 Временных файлов: <code>{disk_items}</code>\n"
                f"📢 Канал: <code>{self.settings.channel_id or 'Не настроен'}</code>"
            )

            sub_res = self.forward_to_sub_server("/status", [], sender_id, chat_id)
            if sub_res and sub_res.get("text"):
                status_text = f"{sub_res['text']}\n\n━━━━━━━━━━━━━━━━━━━━\n{music_block}"
            else:
                status_text = f"📊 <b>Статус Gemini Checker</b>\n⚠️ Сервис недоступен (порт 8088)\n\n━━━━━━━━━━━━━━━━━━━━\n{music_block}"

            self.send_message(chat_id, status_text)
            return

        if cmd == "/mstatus":
            num_workers = self.settings.max_workers
            disk_items = len(list(self.settings.download_dir.glob("*")))
            with self._lock:
                active_count = len(self.active_track_actions)
            cache_count = self.db.count_cached_tracks()

            status_text = (
                "📊 <b>Статус Music Bot</b>\n\n"
                f"⚙️ Воркеров: <code>{num_workers}</code> (активных задач: <code>{active_count}</code>)\n"
                f"⚡ Треков в кэше: <code>{cache_count}</code>\n"
                f"📁 Временных папок: <code>{disk_items}</code>\n"
                f"📢 Канал: <code>{self.settings.channel_id or 'Не настроен'}</code>\n"
                f"🌐 Webhook порт: <code>{self.settings.port}</code>"
            )
            self.send_message(chat_id, status_text)
            return

        PROXY_COMMANDS = {
            "/profiles", "/list",
            "/newprofile", "/add", "/create",
            "/delprofile", "/delete", "/del",
            "/check", "/sync"
        }
        if cmd in PROXY_COMMANDS:
            if cmd in ("/newprofile", "/add", "/create") and args:
                self.send_message(chat_id, f"⏳ Создаю профиль <b>«{args[0]}»</b> и выделяю AWG-серверы...")
            elif cmd == "/sync":
                self.send_message(chat_id, "⏳ Синхронизация всех профилей...")

            res = self.forward_to_sub_server(cmd, args, sender_id, chat_id)
            if res and res.get("text"):
                self.send_message(chat_id, res["text"])
            else:
                self.send_message(
                    chat_id,
                    "⚠️ <b>Сервис прокси-чекера недоступен</b> (порт 8088).\n"
                    "Проверьте работу службы <code>e0f-sub.service</code> на сервере."
                )
            return

        if cmd in ("/album", "/playlist"):
            link = " ".join(args).strip()
            if not link:
                self.send_message(chat_id, "⚠️ Укажите ссылку на плейлист или альбом:\n<code>/album https://youtube.com/playlist?list=...</code>")
                return
            self.executor.submit(self._task_handle_playlist, chat_id, link)
            return

        if cmd in ("/lyrics", "/text"):
            query = " ".join(args).strip()
            if not query:
                self.send_message(chat_id, "⚠️ Укажите исполнителя и название:\n<code>/lyrics 2Pac Hail Mary</code>")
                return
            self.executor.submit(self._task_command_lyrics, chat_id, query)
            return

        if cmd in ("/music", "/song", "/track", "/m"):
            query = " ".join(args).strip()
            if not query:
                self.send_message(chat_id, "⚠️ Укажите название трека:\n<code>/music 2Pac Hail Mary</code>")
            else:
                self.executor.submit(self._task_dispatch_query_or_link, chat_id, query, msg.get("message_id"))
            return

        # Plain text in private chat
        if not cmd.startswith("/") and not chat_id.startswith("-"):
            query = raw_text.strip()
            if query:
                self.executor.submit(self._task_dispatch_query_or_link, chat_id, query, msg.get("message_id"))

    def _task_dispatch_query_or_link(self, chat_id: str, text: str, reply_to_msg_id: Optional[int] = None):
        """Asynchronously analyzes text for streaming links, playlists, or track query."""
        parsed = resolve_music_link(text)
        if parsed["is_link"]:
            if parsed["is_playlist"]:
                self._task_handle_playlist(chat_id, parsed["query"])
                return
            direct_url = parsed["query"] if parsed["source"] in ("youtube", "soundcloud") else None
            search_query = parsed["query"]
            self.process_search_and_preview(
                search_query,
                chat_id,
                reply_to_msg_id,
                hint_artist=parsed.get("artist"),
                hint_title=parsed.get("title"),
                direct_url=direct_url
            )
        else:
            self.process_search_and_preview(
                text,
                chat_id,
                reply_to_msg_id
            )

    def _task_handle_playlist(self, chat_id: str, playlist_url: str):
        """Extracts and queues batch download of playlist tracks in background worker."""
        self.send_message(chat_id, f"📂 <b>Анализирую плейлист:</b>\n<code>{playlist_url}</code>...")
        tracks = self.downloader.extract_playlist_tracks(playlist_url, max_tracks=10)
        if not tracks:
            self.send_message(chat_id, "❌ Не удалось извлечь треки из плейлиста или плейлист пуст.")
            return

        self.send_message(
            chat_id,
            f"✅ <b>Найдено {len(tracks)} треков в плейлисте.</b>\nНачинаю последовательную загрузку..."
        )
        for t_query in tracks:
            self.process_search_and_preview(t_query, chat_id)

    def _task_command_lyrics(self, chat_id: str, query: str):
        """Fetches lyrics for query."""
        parts = query.split(" - ", 1)
        if len(parts) == 2:
            artist, title = parts[0].strip(), parts[1].strip()
        else:
            artist, title = "", query.strip()
        lyrics = fetch_lyrics(artist, title)
        if not lyrics:
            self.send_message(chat_id, f"📝 Текст песни для <code>{query}</code> не найден.")
            return
        if len(lyrics) > 3800:
            lyrics = lyrics[:3800] + "\n\n... (текст сокращён)"
        text = f"📝 <b>Текст песни:</b>\n<code>{query}</code>\n\n{lyrics}"
        self.send_message(chat_id, text, reply_markup={"inline_keyboard": [[{"text": "❌ Закрыть", "callback_data": "dismiss:0"}]]})

    def _task_handle_edit_tags(self, chat_id: str, raw_text: str, state: dict):
        """Processes user reply to update MP3 tags in background thread."""
        track_id = state.get("track_id")
        msg_id = state.get("msg_id")

        if not TRACK_ID_REGEX.fullmatch(track_id):
            self.send_message(chat_id, "⚠️ Ошибка ID трека.")
            return

        base_download_dir = self.settings.download_dir.resolve()
        track_dir = (base_download_dir / track_id).resolve()
        meta_path = track_dir / "meta.json"

        if not meta_path.exists():
            self.send_message(chat_id, "⚠️ Файлы трека не найдены или были удалены.")
            return

        parts = re.split(r'\s*[-—–]\s*', raw_text, maxsplit=1)
        if len(parts) < 2:
            self.send_message(chat_id, "⚠️ Формат не распознан. Укажите через дефис:\n<code>Исполнитель - Название</code>")
            return

        new_artist, new_title = parts[0].strip(), parts[1].strip()

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["artist"] = new_artist
            meta["title"] = new_title
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

            mp3_path = Path(meta["mp3_path"])
            cover_path = Path(meta["cover_path"]) if meta.get("cover_path") else None
            apply_id3_tags(mp3_path, new_artist, new_title, cover_path)

            # Update cache if file_id exists
            if meta.get("file_id"):
                self.db.update_cached_tags(meta["file_id"], new_artist, new_title)

            # Update caption of preview message
            m, s = divmod(meta.get("duration", 0), 60)
            dur_str = f"{m}:{s:02d}"
            caption = (
                f"🎧 <b>Найден трек (теги обновлены):</b>\n"
                f"🎵 <b>{new_artist} — {new_title}</b>\n"
                f"⏱ Длительность: <code>{dur_str}</code>\n"
                f"🏷 Жанр: {meta.get('genre') or '#музыка'}\n\n"
                f"Опубликовать в канале «Свалка треков»?"
            )
            self._api_call("editMessageCaption", json_data={
                "chat_id": chat_id,
                "message_id": msg_id,
                "caption": caption,
                "parse_mode": "HTML"
            })

            self.send_message(chat_id, f"✅ <b>Теги успешно изменены:</b>\n🎵 <b>{new_artist} — {new_title}</b>")
        except Exception as e:
            logger.error(f"Error editing tags: {e}")
            self.send_message(chat_id, f"❌ Ошибка обновления тегов: {e}")

    def run_polling(self):
        """Main long-polling loop with self-healing and webhook conflict resolution."""
        logger.info("Starting Telegram Bot polling loop...")
        self.running = True
        offset = 0

        self._api_call("deleteWebhook", timeout=10)
        last_cleanup = time.time()

        while self.running:
            try:
                if time.time() - last_cleanup > 900:
                    self.cleanup_expired_pending()
                    last_cleanup = time.time()

                r = self.session.get(
                    f"https://api.telegram.org/bot{self.settings.bot_token}/getUpdates?offset={offset}&timeout=20",
                    timeout=30
                )

                if r.status_code == 409:
                    logger.warning("Conflict: Webhook active. Deleting webhook...")
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
