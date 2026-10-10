# 🎵 Telegram Music Bot & Downloader

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![yt-dlp](https://img.shields.io/badge/engine-yt--dlp-red.svg)](https://github.com/yt-dlp/yt-dlp)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Модульный, высокопроизводительный Telegram-бот для поиска, скачивания музыки в высоком качестве (MP3 320 kbps) и публикации в Telegram-канал. Включает обход блокировок YouTube (HTTP 403 / 18+ Age Gate), автоматический фоллбэк на SoundCloud, многопоточную неблокирующую архитектуру (`ThreadPoolExecutor`) и Webhook API для интеграции с **Apple Быстрыми командами (Shazam)**.

---

## ✨ Основные возможности

1. **Обход блокировок и 18+ ограничений YouTube**:
   - Использование Android и iOS API-клиентов YouTube в `yt-dlp`, что предотвращает ошибку `HTTP 403 Forbidden` на серверных IP датацентров.
   - Успешный обход требований `Sign in to confirm your age` (возрастной ценз) без необходимости ручной выгрузки cookies браузера.
2. **Умный фоллбэк на SoundCloud**:
   - Если трек заблокирован правообладателем или недоступен на YouTube, бот автоматически переключается на поиск и загрузку из SoundCloud (`scsearch`).
3. **Многопоточность и отсутствие зависаний (`ThreadPoolExecutor`)**:
   - Поиск, конвертация и выгрузка больших аудиофайлов выполняются в отдельных воркерах.
   - Long-polling цикл Telegram и Webhook сервер никогда не блокируются — бот мгновенно отвечает на кнопки и параллельно обрабатывает множество запросов.
4. **Интеграция с Apple Shortcuts (Командами) и Shazam**:
   - Встроенный легковесный HTTP Webhook сервер (`/shazam` и `/music`, методы `GET` и `POST`) с защитой секретным токеном.
   - Распознанный трек с iPhone сразу отправляется в бота в один клик.
5. **Качественный звук и метаданные**:
   - Конвертация в MP3 **320 kbps** через FFmpeg.
   - Автоматическая очистка мусора из названий (удаление `[Official Video]`, `(Remastered 4K)` и т.д.).
   - Защита от дублирования исполнителя (например, `Moby — Moby - Natural Blues` превращается в `Moby — Natural Blues`).
   - Прошивка ID3v2 тегов (Исполнитель, Название, Альбом) и вшивание обложки в файл.
6. **Интерактивный предпросмотр**:
   - Бот сначала отправляет трек в личные сообщения пользователю с кнопками:
     - `✅ Закинуть в канал` — публикация в указанный канал с хештегом `#музыка` и ссылкой на пост.
     - `❌ Отмена` — удаление временного MP3 и сообщения из диалога.
7. **Автоматическая очистка**:
   - Встроенный фоновый сборщик мусора удаляет неотправленные файлы старше 2 часов.

---

## 🏗 Архитектура

```mermaid
flowchart TD
    User(["Пользователь в Telegram / iOS Shortcut"]) --> Ingress{"Входной запрос"}
    
    subgraph Service["music-tg-bot (Systemd / Port 8082)"]
        Ingress -->|Сообщение / Команда| TG["Telegram Bot Controller"]
        Ingress -->|HTTP Webhook /shazam| WH["Threaded Webhook Server"]
        
        TG -->|Submit Task| Pool["ThreadPoolExecutor Worker Pool"]
        WH -->|Submit Task| Pool
        
        Pool --> DL["Music Downloader"]
        DL -->|1. Попытка YouTube (Android/iOS)| YT[("YouTube Engine")]
        DL -->|2. Фоллбэк при ошибке| SC[("SoundCloud Engine")]
        
        DL --> FF["FFmpeg 320k + Mutagen ID3"]
        FF --> Preview["Отправка MP3 Preview в ЛС"]
        
        Preview --> Action{"Действие пользователя"}
        Action -->|✅ Закинуть| Pub["Публикация в Music Channel (#музыка)"]
        Action -->|❌ Отмена| Del["Удаление файла и сообщения"]
    end
    
    subgraph Storage["Диск"]
        FF -.-> Temp[("Временные файлы downloads/")]
        Del -.-> Clean["Очистка папки"]
        Pub -.-> Clean
    end
```

---

## 📁 Структура проекта

```
music-tg-bot/
├── config.py           # Загрузка и строгая типизация переменных окружения (.env)
├── downloader.py       # Движок загрузки, обход 403/18+, SoundCloud фоллбэк, ID3 теги
├── bot.py              # Контроллер Telegram бота, ThreadPool, кнопки предпросмотра
├── webhook.py          # HTTP Webhook сервер для Apple Shortcuts (/shazam, /music)
├── main.py             # Точка входа: graceful shutdown, инициализация потоков
├── requirements.txt    # Зависимости Python
├── .env.example        # Пример конфигурационного файла (с тестовыми данными)
├── .gitignore          # Исключение .env, временных аудиофайлов и кэша
└── tests/
    └── test_music_bot.py # Юнит-тесты очистки метаданных и параметров
```

---

## 🚀 Установка и запуск

### 1. Клонирование и виртуальное окружение

```bash
git clone https://github.com/teddyxanya/music-tg-bot.git /opt/music-tg-bot
cd /opt/music-tg-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Убедитесь, что в системе установлен **FFmpeg**:
```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

### 2. Настройка конфигурации (.env)

Скопируйте пример конфига и заполните ваши данные:
```bash
cp .env.example .env
nano .env
```

Параметры:
- `TELEGRAM_BOT_TOKEN`: токен бота от `@BotFather`.
- `TELEGRAM_CHAT_ID`: ваш Telegram ID для получения предпросмотра треков.
- `TELEGRAM_OWNER_ID`: ваш Telegram ID владельца.
- `MUSIC_CHANNEL_ID`: ID канала для публикации (бот должен быть администратором канала с правом отправки сообщений), например `-1001234567890`.
- `SHAZAM_SECRET_TOKEN`: случайный секретный ключ для защиты вызовов Webhook.
- `WEBHOOK_HOST`: `127.0.0.1` (слушает локально за Nginx).
- `WEBHOOK_PORT`: `8082`.
- `MAX_WORKERS`: количество параллельных потоков загрузки (по умолчанию `4`).

---

### 3. Запуск как системный сервис (systemd)

Создайте файл `/etc/systemd/system/music-bot.service`:

```ini
[Unit]
Description=Telegram Music Bot and Downloader Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/music-tg-bot
ExecStart=/opt/music-tg-bot/venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Активируйте и запустите сервис:
```bash
sudo systemctl daemon-reload
sudo systemctl enable music-bot
sudo systemctl start music-bot
sudo systemctl status music-bot
```

---

### 4. Настройка Nginx Reverse Proxy

Для интеграции с существующим веб-сервером добавьте следующий блок в конфигурацию вашего сайта Nginx (например, в блок `server`):

```nginx
# Маршрутизация запросов Shazam / Music на автономный сервис
location ~ ^/(shazam|music) {
    proxy_pass http://127.0.0.1:8082;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}
```

Проверьте и перезагрузите Nginx:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## 📱 Настройка Apple Shortcuts (Быстрых команд на iPhone)

1. Откройте приложение **Команды** (Shortcuts) на iOS.
2. Добавьте действие **«Распознать с Shazam»**.
3. Добавьте действие **«Получить содержимое URL»**:
   - **URL**: `http://YOUR_SERVER_IP:8081/shazam` (или ваш домен/порт)
   - **Метод**: `GET` (или `POST`)
   - **Параметры**:
     - `token`: `ВАШ_SHAZAM_SECRET_TOKEN`
     - `artist`: `[Исполнитель из Shazam]`
     - `title`: `[Название из Shazam]`
4. После распознавания бот мгновенно пришлёт MP3 с обложкой вам в Telegram с кнопками подтверждения.

---

## 🧪 Запуск тестов

```bash
python -m unittest discover -s tests
```

---

## 📄 Лицензия

MIT License © 2026 teddyxanya
