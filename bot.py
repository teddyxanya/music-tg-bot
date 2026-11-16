"""
Telegram Bot Controller.
Handles long-polling, callback queries, non-blocking ThreadPool execution,
instant SQLite file_id caching, audio previewing, multi-user channel publishing with genre tags,
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
from typing import Optional, Set, Dict, Any, List, Tuple
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

_TOKEN_RE = re.compile(r'\d{8,11}:[A-Za-z0-9_-]{35}')

def _mask_token(text: str) -> str:
    """Replace any Telegram bot token in a string with '***' before logging."""
    return _TOKEN_RE.sub('bot***REDACTED***', str(text))

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
        self.active_searches: Set[str] = set()
        self.user_states: Dict[str, Dict[str, Any]] = {}

        # Reusable requests session with connection pooling and retries
        self.session = requests.Session()
        if self.settings.telegram_proxy:
            self.session.proxies = {
                "http": self.settings.telegram_proxy,
                "https": self.settings.telegram_proxy
            }
            logger.info(f"Using Telegram API proxy: {self.settings.telegram_proxy}")

        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(
            pool_connections=settings.max_workers + 4,
            pool_maxsize=settings.max_workers * 4,
            max_retries=retries
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Authenticate bot info for rights verification in channels/groups
        me = self._api_call("getMe")
        if me.get("ok"):
            self.bot_id = str(me["result"]["id"])
            self.bot_username = me["result"].get("username", "")
            logger.info(f"Bot authenticated as @{self.bot_username} (ID: {self.bot_id})")
        else:
            self.bot_id = None
            self.bot_username = ""
            logger.warning(f"Could not fetch getMe info: {me}")


    def get_deep_link_add_bot(self) -> str:
        """Returns deep-link to add bot as admin with post_messages permission in 1 click."""
        bot_uname = self.bot_username or "musxan_bot"
        return f"https://t.me/{bot_uname}?startchannel=botadmin&admin=post_messages+edit_messages"

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
        res = self._api_call("sendMessage", json_data=payload)
        if not res.get("ok") and reply_to_message_id:
            # Fallback if reply target message was deleted or not found
            payload.pop("reply_to_message_id", None)
            res = self._api_call("sendMessage", json_data=payload)
        return res

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
        """Purges pending download directories older than configured expiration time and expired DB cache entries."""
        now = time.time()
        try:
            for item in self.settings.download_dir.iterdir():
                if item.is_dir() and (now - item.stat().st_mtime) > self.settings.temp_expiration_sec:
                    with self._lock:
                        if item.name in self.active_track_actions:
                            continue
                    shutil.rmtree(item, ignore_errors=True)
                    logger.info(f"Cleaned up expired track folder: {item.name}")

            if self.settings.cache_expiration_days > 0:
                self.db.cleanup_old_cache(self.settings.cache_expiration_days * 86400)
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")

    def is_authorized(self, sender_id: str) -> bool:
        """Returns True. Access control is managed via Telegram bot settings."""
        return True

    def get_user_target_channel(self, user_id: Optional[str] = None, chat_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Resolves the active target channel for the requesting user.
        Strictly per-user: NO default channel fallback, no channel leakage.
        """
        uid = str(user_id or chat_id or "")
        if not uid:
            return None, None
        active = self.db.get_active_channel(uid)
        if active and active.get("target_channel"):
            return str(active["target_channel"]), active.get("target_channel_title") or "Канал"
        return None, None

    def show_channels_menu(self, chat_id: str, user_id: str, edit_msg_id: Optional[int] = None):
        """Displays interactive list of channels where the user has added the bot as administrator."""
        channels = self.db.get_user_channels(user_id)
        active = self.db.get_active_channel(user_id)
        active_id = str(active["target_channel"]) if active else None

        buttons = []
        for ch in channels:
            cid = str(ch["channel_id"])
            ctitle = ch["channel_title"]
            is_active = (cid == active_id)
            prefix = "✅ " if is_active else "▫️ "
            buttons.append([
                {"text": f"{prefix}{ctitle}", "callback_data": f"ch_select:{cid}"},
                {"text": "🗑️ Отвязать", "callback_data": f"ch_del:{cid}"}
            ])

        buttons.append([
            {"text": "➕ Добавить бота в канал", "url": self.get_deep_link_add_bot()}
        ])

        if channels:
            cur_name = active.get("target_channel_title", "Не выбран") if active else "Не выбран"
            text = (
                "📢 <b>Управление вашими каналами</b>\n\n"
                f"🎯 <b>Активный канал:</b> <b>{cur_name}</b>\n\n"
                "• Нажмите на канал в списке, чтобы сделать его активным для публикации музыки.\n"
                "• Нажмите <b>«➕ Добавить бота в канал»</b>, чтобы подключить новый канал — он появится в этом списке автоматически!"
            )
        else:
            text = (
                "📢 <b>У вас пока нет подключенных каналов</b>\n\n"
                "Чтобы публиковать музыку в ваш канал или группу:\n\n"
                "1. Нажмите кнопку <b>«➕ Добавить бота в канал»</b> ниже.\n"
                "2. Выберите ваш канал — Telegram сразу предложит добавить бота администратором.\n"
                "3. После добавления канал <b>автоматически подтянется</b> сюда без необходимости искать его ID или ссылку!\n\n"
                "<i>(Также можно отправить ID или @username вручную: <code>/setchannel @username</code>)</i>"
            )

        reply_markup = {"inline_keyboard": buttons}
        if edit_msg_id:
            self.edit_message(chat_id, edit_msg_id, text, reply_markup=reply_markup)
        else:
            self.send_message(chat_id, text, reply_markup=reply_markup)

    def show_shazam_info(self, chat_id: str, user_id: str, edit_msg_id: Optional[int] = None):
        """Displays user's personal iOS Shazam Shortcuts webhook URL and setup instructions."""
        # For owner, preserve legacy secret if not set yet
        default_token = self.settings.shazam_secret if user_id == str(self.settings.chat_id or "78740357") else None
        token = self.db.get_or_create_shazam_token(user_id, default_token=default_token)
        base_url = self.settings.shazam_base_url
        endpoint_url = f"{base_url}/shazam"
        get_link_url = f"{endpoint_url}?token={token}&artist=Распознавание музыки (Артист)&title=Распознавание музыки (Название)"

        text = (
            "📱 <b>Интеграция с Apple Быстрые команды (iOS Shazam)</b>\n\n"
            "Распознавайте треки через Shazam на iPhone/Apple Watch и отправляйте их боту в 1 тап!\n\n"
            "⚙️ <b>Параметры для действия «Получить содержимое URL» (POST JSON):</b>\n\n"
            f"• <b>URL:</b> <code>{endpoint_url}</code>\n"
            "• <b>Метод:</b> <code>POST</code>\n"
            "• <b>Тело запроса:</b> <code>JSON</code>\n"
            f"• <b>Ваш личный токен:</b> <code>{token}</code>\n"
            "<i>(нажмите на токен или URL выше, чтобы скопировать)</i>\n\n"
            "📋 <b>Пошаговая настройка в приложении «Команды» (Shortcuts):</b>\n"
            "1. Откройте приложение <b>«Команды»</b> на iPhone и нажмите <b>«+»</b> (Создать).\n"
            "2. Добавьте действие <b>«Распознать песню через Shazam»</b>.\n"
            "3. Добавьте действие <b>«Получить содержимое URL»</b> и настройте его:\n"
            f"   • В поле URL вставьте: <code>{endpoint_url}</code>\n"
            "   • <b>Метод:</b> выберите <b>POST</b>\n"
            "   • <b>Тело запроса:</b> выберите <b>JSON</b>\n"
            "   • Нажмите <b>«Добавить новое поле»</b> и создайте 3 поля:\n"
            f"     1) <code>token</code>  ➔  Текст: <code>{token}</code>\n"
            "     2) <code>artist</code> ➔  переменная Shazam: <b>«Артист»</b>\n"
            "     3) <code>title</code>  ➔  переменная Shazam: <b>«Название»</b>\n"
            "4. <b>Готово!</b> Добавьте команду в Виджеты, на экран блокировки или Action Button на iPhone.\n\n"
            "<i>(Также поддерживается простой GET-запрос одной строкой:</i>\n"
            f"<code>{get_link_url}</code><i>)</i>"
        )

        reply_markup = {
            "inline_keyboard": [
                [{"text": "🔄 Сгенерировать новый токен", "callback_data": "shazam:new_token"}],
                [{"text": "📋 Мои каналы", "callback_data": "channels:list"}]
            ]
        }

        if edit_msg_id:
            self.edit_message(chat_id, edit_msg_id, text, reply_markup=reply_markup)
        else:
            self.send_message(chat_id, text, reply_markup=reply_markup)

    def handle_my_chat_member(self, mcm: dict):
        """
        Catches real-time Telegram updates when the bot is added to a channel/group,
        promoted to administrator, or removed.
        """
        try:
            chat = mcm.get("chat", {})
            from_user = mcm.get("from", {})
            new_member = mcm.get("new_chat_member", {})

            chat_id = str(chat.get("id", ""))
            chat_title = chat.get("title") or chat.get("username") or "Канал"
            chat_username = chat.get("username")
            user_id = str(from_user.get("id", ""))
            new_status = new_member.get("status", "")

            logger.info(f"my_chat_member update: user={user_id}, chat={chat_id} ({chat_title}), status={new_status}")

            if not user_id or not chat_id:
                return

            if new_status in ("administrator", "creator"):
                can_post = new_member.get("can_post_messages", True)
                self.db.save_user_channel(
                    user_id=user_id,
                    channel_id=chat_id,
                    channel_title=chat_title,
                    channel_username=chat_username,
                    status="administrator"
                )
                self.db.set_active_channel(user_id, chat_id, chat_title)

                msg_text = (
                    f"🎉 <b>Канал успешно подключен!</b>\n\n"
                    f"📢 <b>{chat_title}</b> (<code>{chat_id}</code>)\n"
                    f"• Статус: <b>Администратор</b>\n\n"
                    f"✅ Канал установлен как <b>активный</b> для отправки музыки.\n"
                    f"Теперь при нажатии «Закинуть в канал» треки будут отправляться именно сюда!"
                )
                if not can_post:
                    msg_text += "\n\n⚠️ <i>Внимание: отключено право на публикацию сообщений. Включите его в настройках администратора канала, чтобы бот мог отправлять аудио.</i>"

                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "📋 Мои каналы", "callback_data": "channels:list"}]
                    ]
                }
                self.send_message(user_id, msg_text, reply_markup=reply_markup)

            elif new_status in ("left", "kicked"):
                self.db.remove_user_channel(user_id, chat_id)
                self.send_message(
                    user_id,
                    f"ℹ️ Бот был удален из канала «<b>{chat_title}</b>». Привязка канала удалена."
                )

            elif new_status == "member":
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "👑 Назначить администратором", "url": self.get_deep_link_add_bot()}]
                    ]
                }
                self.send_message(
                    user_id,
                    f"⚠️ <b>Бот добавлен в «{chat_title}», но без прав администратора!</b>\n\n"
                    "Чтобы бот мог публиковать музыку, назначьте его <b>Администратором</b> с правом «Публикация сообщений».\n\n"
                    "Нажмите кнопку ниже, чтобы обновить права бота:",
                    reply_markup=reply_markup
                )
        except Exception as e:
            logger.error(f"Error handling my_chat_member: {e}")

    def _handle_shared_chat(self, chat_id: str, user_id: str, shared_id: str):
        """Handles Telegram Bot API chat_shared event from KeyboardButtonRequestChat."""
        chat_info = self._api_call("getChat", json_data={"chat_id": shared_id})
        if not chat_info.get("ok"):
            self.send_message(chat_id, f"❌ Не удалось получить информацию о канале <code>{shared_id}</code>.")
            return

        res = chat_info.get("result", {})
        title = res.get("title") or res.get("username") or str(shared_id)
        username = res.get("username")

        # Check admin rights
        if self.bot_id:
            member_info = self._api_call("getChatMember", json_data={"chat_id": shared_id, "user_id": self.bot_id})
            if member_info.get("ok"):
                status = member_info.get("result", {}).get("status", "")
                if status not in ("administrator", "creator"):
                    reply_markup = {
                        "inline_keyboard": [
                            [{"text": "👑 Назначить администратором", "url": self.get_deep_link_add_bot()}]
                        ]
                    }
                    self.send_message(
                        chat_id,
                        f"⚠️ В канале «<b>{title}</b>» бот не является администратором.\n"
                        "Назначьте бота администратором с правом публикации сообщений.",
                        reply_markup=reply_markup
                    )
                    return

        self.db.save_user_channel(user_id, shared_id, title, username, "administrator")
        self.db.set_active_channel(user_id, shared_id, title)
        self.send_message(
            chat_id,
            f"✅ <b>Канал подключен и выбран активным:</b>\n\n"
            f"📢 <b>{title}</b> (<code>{shared_id}</code>)\n"
            "Теперь найденная музыка будет публиковаться сюда!"
        )

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
        chat_id = target_chat_id or self.settings.chat_id
        if not chat_id:
            logger.error("No valid chat_id available to send preview")
            return

        clean_query = query.strip()
        norm_key = re.sub(r'[^a-zA-Z0-9а-яА-ЯёЁ]', '', clean_query.lower())
        search_lock_key = f"{chat_id}:{norm_key}"

        with self._lock:
            if search_lock_key in self.active_searches:
                logger.info(f"Deduplicating duplicate in-flight search '{clean_query}' for chat {chat_id}")
                self.send_message(
                    chat_id,
                    f"⏳ <b>Трек уже скачивается:</b>\n<code>{clean_query}</code>\n<i>Пожалуйста, подождите пару секунд...</i>",
                    reply_to_message_id=reply_to_msg_id
                )
                return
            self.active_searches.add(search_lock_key)

        stop_action = threading.Event()
        def _keep_chat_action():
            while not stop_action.is_set():
                try:
                    self._api_call("sendChatAction", json_data={"chat_id": str(chat_id), "action": "upload_voice"}, timeout=5)
                except Exception:
                    pass
                stop_action.wait(4.0)

        action_thread = threading.Thread(target=_keep_chat_action, daemon=True)
        action_thread.start()

        status_msg_id = None
        try:
            # Step 1: Check instant SQLite cache (file_id) for instant 0.1s replay
            if not direct_url:
                cached = self.db.get_cached_track(clean_query, hint_artist, hint_title)
                if cached and cached.get("file_id"):
                    logger.info(f"Instant cache hit for '{clean_query}': file_id {cached['file_id']}")
                    m, s = divmod(cached["duration"], 60)
                    dur_str = f"{m}:{s:02d}"
                    _, ch_title = self.get_user_target_channel(chat_id)
                    ch_prompt = f"в канал «{ch_title}»" if ch_title and ch_title != "Канал" else "в канал"
                    caption = (
                        f"⚡ <b>Мгновенно из кэша (0.1 сек):</b>\n"
                        f"🎵 <b>{cached['artist']} — {cached['title']}</b>\n"
                        f"⏱ Длительность: <code>{dur_str}</code>\n"
                        f"🏷 Жанр: {cached.get('genre') or '#музыка'}\n\n"
                        f"Опубликовать {ch_prompt}?"
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
                _, ch_title = self.get_user_target_channel(chat_id)
                ch_prompt = f"в канал «{ch_title}»" if ch_title and ch_title != "Канал" else "в канал"
                caption = (
                    f"🎧 <b>Найден трек ({source_icon}):</b>\n"
                    f"🎵 <b>{track.artist} — {track.title}</b>\n"
                    f"⏱ Длительность: <code>{dur_str}</code>\n"
                    f"🏷 Жанр: {track.genre}\n\n"
                    f"Опубликовать {ch_prompt}?"
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

        finally:
            stop_action.set()
            with self._lock:
                self.active_searches.discard(search_lock_key)

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

        # Channel management callbacks
        if action == "ch_select":
            ch = self.db.get_channel_by_id(from_user_id, param)
            if ch:
                self.db.set_active_channel(from_user_id, param, ch["channel_title"])
                self._api_call("answerCallbackQuery", json_data={
                    "callback_query_id": cb_id,
                    "text": f"✅ Выбран: {ch['channel_title']}"
                })
                self.show_channels_menu(chat_id, from_user_id, edit_msg_id=msg_id)
            else:
                self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "Канал не найден"})
            return

        if action == "ch_del":
            self.db.remove_user_channel(from_user_id, param)
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "🗑️ Канал отвязан"})
            self.show_channels_menu(chat_id, from_user_id, edit_msg_id=msg_id)
            return

        if action == "channels" and param == "list":
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id})
            self.show_channels_menu(chat_id, from_user_id, edit_msg_id=msg_id)
            return

        # Shazam webhook callbacks
        if action == "shazam":
            if param == "new_token":
                self.db.regenerate_shazam_token(from_user_id)
                self._api_call("answerCallbackQuery", json_data={
                    "callback_query_id": cb_id,
                    "text": "✅ Сгенерирован новый персональный токен!"
                })
                self.show_shazam_info(chat_id, from_user_id, edit_msg_id=msg_id)
                return
            if param == "info":
                self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id})
                self.show_shazam_info(chat_id, from_user_id, edit_msg_id=msg_id)
                return

        # 1. Dismiss notification
        if action == "dismiss":
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id})
            self.delete_message(chat_id, msg_id)
            return

        # 2. Cached track actions (race-condition protected)
        if action == "pubcached":
            if not param.isdigit():
                return
            cache_action_key = f"cache_{param}_{from_user_id}"
            with self._lock:
                if cache_action_key in self.active_track_actions:
                    self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "⏳ Уже публикуется..."})
                    return
                self.active_track_actions.add(cache_action_key)
            self._api_call("answerCallbackQuery", json_data={"callback_query_id": cb_id, "text": "🚀 Публикую из кэша..."})
            self.executor.submit(self._task_publish_cached, int(param), chat_id, msg_id, cache_action_key, from_user_id)
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
            self.executor.submit(self._task_publish_track, track_id, track_dir, meta_path, chat_id, msg_id, from_user_id)
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

    def _task_publish_cached(self, cached_id: int, chat_id: str, msg_id: int, cache_action_key: str, user_id: str = ""):
        try:
            row = self.db.get_track_by_id(cached_id)
            if not row:
                self.send_message(chat_id, "⚠️ Запись в кэше не найдена.")
                return

            target_channel, ch_title = self.get_user_target_channel(user_id, chat_id)
            if not target_channel:
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "➕ Добавить бота в канал", "url": self.get_deep_link_add_bot()}],
                        [{"text": "📋 Мои каналы", "callback_data": "channels:list"}]
                    ]
                }
                self.send_message(
                    chat_id,
                    "⚠️ <b>Канал для публикации не выбран!</b>\n\n"
                    "Чтобы треки отправлялись в ваш канал или группу:\n"
                    "1. Нажмите <b>«➕ Добавить бота в канал»</b> ниже.\n"
                    "2. Выберите ваш канал — Telegram добавит бота администратором.\n"
                    "3. После добавления канал подключится автоматически!",
                    reply_markup=reply_markup
                )
                return

            artist = row["artist"]
            title = row["title"]
            duration = row["duration"]
            file_id = row["file_id"]
            genre = row["genre"] or "#музыка"

            caption = f"🎵 <b>{artist} — {title}</b>\n\n{genre}"
            res = self._api_call("sendAudio", json_data={
                "chat_id": str(target_channel),
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
                c_id_clean = re.sub(r'^-100', '', str(target_channel)).lstrip('-')
                channel_link = f"https://t.me/c/{c_id_clean}/{channel_msg_id}"

            # Update channel link in database
            self.db.update_channel_info(cached_id, channel_msg_id, channel_link)

            self._api_call("editMessageCaption", json_data={
                "chat_id": chat_id,
                "message_id": msg_id,
                "caption": (
                    f"✅ <b>Опубликовано в «{ch_title or 'Канал'}»!</b>\n\n"
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

    def _task_publish_track(self, track_id: str, track_dir: Path, meta_path: Path, chat_id: str, msg_id: int, user_id: str = ""):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = meta["title"]
            artist = meta["artist"]
            duration = meta.get("duration", 0)
            mp3_path = Path(meta["mp3_path"])
            thumb_path = Path(meta["thumb_path"]) if meta.get("thumb_path") else None
            genre = meta.get("genre") or "#музыка"

            target_channel, ch_title = self.get_user_target_channel(user_id, chat_id)
            if not target_channel:
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "➕ Добавить бота в канал", "url": self.get_deep_link_add_bot()}],
                        [{"text": "📋 Мои каналы", "callback_data": "channels:list"}]
                    ]
                }
                self.send_message(
                    chat_id,
                    "⚠️ <b>Канал для публикации не выбран!</b>\n\n"
                    "Чтобы треки отправлялись в ваш канал или группу:\n"
                    "1. Нажмите <b>«➕ Добавить бота в канал»</b> ниже.\n"
                    "2. Выберите ваш канал — Telegram добавит бота администратором.\n"
                    "3. После добавления канал подключится автоматически!",
                    reply_markup=reply_markup
                )
                return

            # Check file size limit before channel upload
            file_size = mp3_path.stat().st_size
            if file_size > MAX_TELEGRAM_AUDIO_BYTES:
                raise Exception(f"Файл слишком большой ({file_size // (1024*1024)} МБ) для отправки в Telegram.")

            # Upload MP3 directly to target channel with genre hashtags
            res = None
            with open(mp3_path, 'rb') as audio_file:
                files = {'audio': (mp3_path.name, audio_file, 'audio/mpeg')}
                caption = f"🎵 <b>{artist} — {title}</b>\n\n{genre}"
                data = {
                    'chat_id': str(target_channel),
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
                c_id_clean = re.sub(r'^-100', '', str(target_channel)).lstrip('-')
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
                    f"✅ <b>Опубликовано в «{ch_title or 'Канал'}»!</b>\n\n"
                    f"🎵 <b>{artist} — {title}</b>\n"
                    f"🔗 <a href=\"{channel_link}\">Перейти к треку в канале</a>"
                ),
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []}
            })

            # Cleanup temp folder
            shutil.rmtree(track_dir, ignore_errors=True)
            logger.info(f"Published track {artist} - {title} to channel {target_channel}")

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

        # Check Telegram Bot API 6.7+ / 7.0+ request_chat response (chat_shared)
        chat_shared = msg.get("chat_shared") or (msg.get("chats_shared", {}).get("chats", [{}])[0] if msg.get("chats_shared") else None)
        if chat_shared:
            shared_id = str(chat_shared.get("chat_id", ""))
            if shared_id:
                self._handle_shared_chat(chat_id, sender_id, shared_id)
                return

        # Check conversational states (tag editing) asynchronously
        if chat_id in self.user_states:
            state = self.user_states.pop(chat_id)
            if state.get("action") == "edit_tags":
                self.executor.submit(self._task_handle_edit_tags, chat_id, raw_text, state)
                return

        parts = raw_text.split()
        cmd = parts[0].lower() if parts else ""
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]
        args = parts[1:] if len(parts) > 1 else []

        if cmd in ("/start", "/help"):
            help_text = (
                "🎵 <b>Telegram Music Bot & Downloader</b>\n\n"
                "🔍 <b>Поиск и скачивание музыки:</b>\n"
                "• Отправьте название трека или исполнителя (например, <code>2Pac Hail Mary</code>)\n"
                "• Отправьте ссылку на трек или альбом (YouTube, SoundCloud, Spotify, Apple Music, Яндекс Музыка)\n"
                "• <code>/music &lt;название&gt;</code> — Скачать MP3 (320 kbps, обложка 1:1, EBU R128)\n"
                "• <code>/album &lt;ссылка&gt;</code> — Пакетное скачивание альбома / плейлиста\n"
                "• <code>/lyrics &lt;трек&gt;</code> — Поиск и показ слов песни\n\n"
                "📢 <b>Настройка каналов:</b>\n"
                "• <code>/channels</code> — Управление подключенными каналами и добавление в 1 клик\n"
                "• <code>/mychannel</code> — Посмотреть текущий активный канал\n"
                "• <code>/clearchannel</code> — Сбросить выбор активного канала\n\n"
                "📱 <b>Интеграция с iPhone / iOS:</b>\n"
                "• <code>/shazam</code> — Личная ссылка для Apple Быстрых команд (Shazam в 1 тап)\n\n"
                "📊 <b>Статус:</b>\n"
                "• <code>/status</code> — Статистика воркеров, кэша и диска"
            )
            reply_markup = {
                "inline_keyboard": [
                    [{"text": "📱 Быстрая команда Shazam (iOS)", "callback_data": "shazam:info"}],
                    [{"text": "📋 Мои каналы", "callback_data": "channels:list"}]
                ]
            }
            self.send_message(chat_id, help_text, reply_markup=reply_markup)
            return

        if cmd in ("/shazam", "/ios", "/shortcut", "/shortcuts"):
            self.show_shazam_info(chat_id, sender_id)
            return

        if cmd in ("/channels", "/channel"):
            self.show_channels_menu(chat_id, sender_id)
            return

        if cmd in ("/setchannel", "/setgroup"):
            target = args[0].strip() if args else ""
            if not target:
                self.show_channels_menu(chat_id, sender_id)
                return

            chat_info = self._api_call("getChat", json_data={"chat_id": target})
            if not chat_info.get("ok"):
                desc = chat_info.get("description", "Чат не найден")
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "➕ Добавить бота в канал", "url": self.get_deep_link_add_bot()}]
                    ]
                }
                self.send_message(
                    chat_id,
                    f"❌ <b>Не удалось найти канал/группу <code>{target}</code></b>\n\n"
                    f"Telegram ответил: <i>{desc}</i>\n\n"
                    "💡 <b>Решение:</b>\n"
                    "Нажмите кнопку ниже, чтобы добавить бота администратором в ваш канал (он подключится автоматически):",
                    reply_markup=reply_markup
                )
                return

            res_obj = chat_info.get("result", {})
            title = res_obj.get("title") or res_obj.get("username") or str(target)
            target_id = str(res_obj.get("id", target))
            username = res_obj.get("username")

            if self.bot_id:
                member_info = self._api_call("getChatMember", json_data={"chat_id": target_id, "user_id": self.bot_id})
                if member_info.get("ok"):
                    status = member_info.get("result", {}).get("status", "")
                    if status not in ("administrator", "creator"):
                        reply_markup = {
                            "inline_keyboard": [
                                [{"text": "👑 Назначить администратором", "url": self.get_deep_link_add_bot()}]
                            ]
                        }
                        self.send_message(
                            chat_id,
                            f"⚠️ <b>Внимание:</b> Бот состоит в «{title}», но статус <code>{status}</code> (не администратор).\n"
                            "Убедитесь, что боту разрешено публиковать сообщения.",
                            reply_markup=reply_markup
                        )

            self.db.save_user_channel(sender_id, target_id, title, username, "administrator")
            self.db.set_active_channel(sender_id, target_id, title)

            self.send_message(
                chat_id,
                f"✅ <b>Канал успешно привязан и выбран активным!</b>\n\n"
                f"📢 <b>{title}</b> (<code>{target_id}</code>)\n\n"
                "Теперь при нажатии «Закинуть в канал» треки будут отправляться в ваш канал!"
            )
            return

        if cmd == "/mychannel":
            active = self.db.get_active_channel(sender_id)
            if active:
                target_ch = active["target_channel"]
                ch_title = active.get("target_channel_title", "Канал")
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "📋 Управление каналами", "callback_data": "channels:list"}],
                        [{"text": "➕ Добавить еще канал", "url": self.get_deep_link_add_bot()}]
                    ]
                }
                self.send_message(
                    chat_id,
                    f"📢 <b>Ваш текущий активный канал:</b>\n\n"
                    f"• Название: <b>{ch_title}</b>\n"
                    f"• ID: <code>{target_ch}</code>\n\n"
                    "Все отправляемые треки публикуются именно сюда.",
                    reply_markup=reply_markup
                )
            else:
                self.show_channels_menu(chat_id, sender_id)
            return

        if cmd == "/clearchannel":
            cleared = self.db.clear_active_channel(sender_id)
            if cleared:
                self.send_message(
                    chat_id,
                    "🗑️ <b>Выбор активного канала сброшен.</b>\n\n"
                    "Вы можете выбрать другой канал командой <code>/channels</code>."
                )
            else:
                self.send_message(
                    chat_id,
                    "ℹ️ У вас не было выбрано активного канала.\n"
                    "Открыть список каналов: <code>/channels</code>"
                )
            return

        if cmd in ("/status", "/mstatus"):
            num_workers = self.settings.max_workers
            disk_items = len(list(self.settings.download_dir.glob("*")))
            with self._lock:
                active_count = len(self.active_track_actions)
            cache_count = self.db.count_cached_tracks()
            user_channels = self.db.get_user_channels(sender_id)
            active = self.db.get_active_channel(sender_id)
            ch_display = f"<b>{active['target_channel_title']}</b> (<code>{active['target_channel']}</code>)" if active else "<i>Не выбран (/channels)</i>"
            temp_hours = self.settings.temp_expiration_sec // 3600

            status_text = (
                "📊 <b>Статус Music Bot</b>\n\n"
                f"⚙️ Воркеров: <code>{num_workers}</code> (активных задач: <code>{active_count}</code>)\n"
                f"⚡ Треков в кэше: <code>{cache_count}</code>\n"
                f"📁 Временных файлов на диске: <code>{disk_items}</code>\n"
                f"⏳ Срок хранения кэша на диске: <code>{temp_hours} ч.</code>\n"
                f"🗄️ Хранение истории в БД: <code>{self.settings.cache_expiration_days or 'бессрочно'} дн.</code>\n"
                f"📢 Ваш активный канал: {ch_display}\n"
                f"📋 Подключенных каналов у вас: <code>{len(user_channels)}</code>\n"
                f"🌐 Webhook порт: <code>{self.settings.port}</code>"
            )
            self.send_message(chat_id, status_text)
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
            hint_artist = None
            hint_title = None
            # Check for "Artist - Title" format (supports -, —, –)
            parts = re.split(r'\s+[-—–]\s+', text, maxsplit=1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                hint_artist, hint_title = parts[0].strip(), parts[1].strip()

            self.process_search_and_preview(
                text,
                chat_id,
                reply_to_msg_id,
                hint_artist=hint_artist,
                hint_title=hint_title
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
            _, ch_title = self.get_user_target_channel(chat_id)
            ch_prompt = f"в канал «{ch_title}»" if ch_title and ch_title != "Канал" else "в канал"
            caption = (
                f"🎧 <b>Найден трек (теги обновлены):</b>\n"
                f"🎵 <b>{new_artist} — {new_title}</b>\n"
                f"⏱ Длительность: <code>{dur_str}</code>\n"
                f"🏷 Жанр: {meta.get('genre') or '#музыка'}\n\n"
                f"Опубликовать {ch_prompt}?"
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

                allowed_updates = json.dumps(["message", "callback_query", "my_chat_member", "chat_member"])
                r = self.session.get(
                    f"https://api.telegram.org/bot{self.settings.bot_token}/getUpdates?offset={offset}&timeout=20&allowed_updates={allowed_updates}",
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

                    mcm = update.get("my_chat_member")
                    if mcm:
                        self.handle_my_chat_member(mcm)
                        continue

                    msg = update.get("message")
                    if msg:
                        self.handle_message(msg)

            except Exception as e:
                logger.error(f"Polling loop exception: {_mask_token(e)}")
                time.sleep(3)

    def stop(self):
        logger.info("Stopping MusicBot...")
        self.running = False
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.session.close()
