"""
Telegram-бот для загрузки Instagram Reels/Posts, TikTok и YouTube видео через yt-dlp
Установка:
    pip install python-telegram-bot yt-dlp instaloader

Переменные окружения:
    BOT_TOKEN      — токен бота от @BotFather
    ADMIN_ID       — Telegram user_id администратора (только он может менять подпись)
    IG_COOKIES     — путь к файлу cookies Instagram в формате Netscape (рекомендуется)
    IG_USERNAME    — логин Instagram (устаревший способ, работает нестабильно)
    IG_PASSWORD    — пароль Instagram (устаревший способ, работает нестабильно)

Узнать свой user_id: написать боту @userinfobot

Как получить cookies (рекомендуемый способ):
    1. Установите расширение «Get cookies.txt LOCALLY» для Chrome/Firefox
    2. Зайдите на instagram.com в браузере под своим аккаунтом
    3. Нажмите на расширение → Export cookies → сохраните файл как instagram_cookies.txt
    4. Укажите путь: IG_COOKIES=/path/to/instagram_cookies.txt

    Или через yt-dlp напрямую:
        yt-dlp --cookies-from-browser chrome --cookies instagram_cookies.txt https://www.instagram.com/p/XXXX/

Запуск:
    python instagram_bot.py
"""

import re
import os
import glob
import shutil
import tempfile
import logging
import time
import asyncio
import http.cookiejar
import yt_dlp
import instaloader
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ─── НАСТРОЙКИ ─────────────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
IG_COOKIES  = os.environ.get("IG_COOKIES", "instagram_cookies.txt")   # путь к файлу cookies
IG_USERNAME = os.environ.get("IG_USERNAME", "")
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")

_admin_id_raw = os.environ.get("ADMIN_ID", "")
try:
    ADMIN_ID: int | None = int(_admin_id_raw)
except ValueError:
    ADMIN_ID = None

# ─── ЛОГИРОВАНИЕ ───────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── ПАТТЕРНЫ ──────────────────────────────────────────────────────────────────

INSTAGRAM_PATTERN = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+/?(?:\?[^\s]*)?"
)
TIKTOK_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:tiktok\.com/@[\w.]+/video/\d+|vm\.tiktok\.com/[A-Za-z0-9]+|vt\.tiktok\.com/[A-Za-z0-9]+)/?(?:\?[^\s]*)?"
)
YOUTUBE_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?(?:[^&\s]+&)*v=|shorts/|embed/)|youtu\.be/)[A-Za-z0-9_-]+(?:[&?][^\s]*)?"
)

# ─── КАЧЕСТВО YOUTUBE ──────────────────────────────────────────────────────────

YOUTUBE_QUALITIES = {
    "360":  "best[height<=360][ext=mp4]/best[height<=360]",
    "480":  "best[height<=480][ext=mp4]/best[height<=480]",
    "720":  "best[height<=720][ext=mp4]/best[height<=720]",
    "1080": "best[height<=1080][ext=mp4]/best[height<=1080]",
}
DEFAULT_YT_QUALITY = "720"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# ─── ВРЕМЕННАЯ ПАПКА ДЛЯ ВИДЕО ────────────────────────────────────────────────
# Используем отдельную поддиректорию внутри /tmp чтобы:
# 1. Не мешать системному /tmp
# 2. Иметь возможность чистить только наши файлы
BOT_TMP_DIR = os.environ.get("BOT_TMP_DIR", "/tmp/bot_dl")
os.makedirs(BOT_TMP_DIR, exist_ok=True)

# Файлы старше этого времени (в секундах) считаются мусором от прошлых крэшей
STALE_FILE_AGE_SEC = int(os.environ.get("STALE_FILE_AGE_SEC", str(60 * 30)))  # 30 минут

# Интервал фоновой очистки
CLEANUP_INTERVAL_SEC = int(os.environ.get("CLEANUP_INTERVAL_SEC", str(60 * 15)))  # каждые 15 минут

# Telegram: максимум 10 медиа в одном альбоме
MAX_ALBUM_SIZE = 10

# ─── ХРАНИЛИЩА ─────────────────────────────────────────────────────────────────

global_caption: str | None = None
user_yt_quality: dict[int, str] = {}
waiting_for_caption: set[int] = set()

# ─── ВСПОМОГАТЕЛЬНЫЕ ───────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


def get_message(update: Update):
    return update.message or update.channel_post


def admin_caption_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить подпись", callback_data="caption_edit")],
        [InlineKeyboardButton("🗑 Убрать подпись",   callback_data="caption_off")],
    ])


def admin_quality_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for q in YOUTUBE_QUALITIES:
        mark = "✅ " if q == current else ""
        row.append(InlineKeyboardButton(f"{mark}{q}p", callback_data=f"quality_{q}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def classify_file(path: str) -> str:
    """Возвращает 'video', 'photo' или 'unknown'."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in PHOTO_EXTENSIONS:
        return "photo"
    return "unknown"


def collect_media_files(tmp_dir: str) -> list[str]:
    """Собирает все медиафайлы из папки, сортирует по имени (сохраняет порядок карусели)."""
    files = []
    for ext in list(VIDEO_EXTENSIONS) + list(PHOTO_EXTENSIONS):
        files.extend(glob.glob(os.path.join(tmp_dir, f"*{ext}")))
        files.extend(glob.glob(os.path.join(tmp_dir, f"*{ext.upper()}")))
    # убираем дубли и сортируем
    files = sorted(set(files))
    return files


# ─── НАРЕЗКА ВИДЕО ─────────────────────────────────────────────────────────────

TG_MAX_MB = 50          # Telegram-лимит для ботов (МБ)
PART_TARGET_MB = 45     # целевой размер части с запасом

def get_video_duration(path: str) -> float | None:
    """Возвращает длительность видео в секундах через ffprobe."""
    import subprocess, json
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", path
            ],
            capture_output=True, text=True, timeout=30
        )
        info = json.loads(result.stdout)
        return float(info["format"]["duration"])
    except Exception as e:
        log.error(f"ffprobe ошибка: {e}")
        return None


def split_video_by_size(path: str, tmp_dir: str) -> list[str]:
    """
    Нарезает видео на части, каждая ≤ PART_TARGET_MB МБ.
    Использует ffmpeg без перекодирования (copy), быстро.
    Возвращает список путей к частям; если нарезка не удалась — [path].
    """
    import subprocess

    file_mb = os.path.getsize(path) / 1024 / 1024
    if file_mb <= TG_MAX_MB:
        return [path]

    duration = get_video_duration(path)
    if not duration:
        log.warning(f"Не удалось получить длительность {path}, отправляю как есть")
        return [path]

    # Считаем сколько частей нужно
    num_parts = int(file_mb / PART_TARGET_MB) + 1
    part_duration = duration / num_parts

    log.info(f"Видео {file_mb:.1f} МБ → нарезаю на {num_parts} части по ~{part_duration:.0f}с")

    base = os.path.splitext(os.path.basename(path))[0]
    parts = []

    for i in range(num_parts):
        start = i * part_duration
        part_path = os.path.join(tmp_dir, f"{base}_part{i+1:02d}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", path,
            "-t", str(part_duration),
            "-c", "copy",           # без перекодирования — быстро
            "-avoid_negative_ts", "1",
            part_path
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode == 0 and os.path.exists(part_path) and os.path.getsize(part_path) > 0:
                part_mb = os.path.getsize(part_path) / 1024 / 1024
                log.info(f"  Часть {i+1}: {part_mb:.1f} МБ → {part_path}")
                parts.append(part_path)
            else:
                log.error(f"ffmpeg не создал часть {i+1}: {result.stderr.decode()[:300]}")
        except subprocess.TimeoutExpired:
            log.error(f"ffmpeg таймаут на части {i+1}")
        except FileNotFoundError:
            log.error("ffmpeg не найден! Установи: apt install ffmpeg")
            return [path]

    if not parts:
        log.warning("Нарезка не удалась, возвращаю оригинал")
        return [path]

    return parts


def prepare_files_for_sending(files: list[str], tmp_dir: str) -> list[str]:
    """
    Проверяет каждый файл и при необходимости нарезает большие видео на части.
    Возвращает итоговый список файлов готовых к отправке.
    """
    result = []
    for path in files:
        file_mb = os.path.getsize(path) / 1024 / 1024
        if file_mb > TG_MAX_MB and classify_file(path) == "video":
            log.info(f"Файл {os.path.basename(path)} ({file_mb:.1f} МБ) > {TG_MAX_MB} МБ, нарезаю...")
            parts = split_video_by_size(path, tmp_dir)
            result.extend(parts)
        else:
            result.append(path)
    return result


# ─── КОМАНДА /start ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = get_message(update)
    if not message:
        return

    from_user = message.from_user
    is_adm = from_user and is_admin(from_user.id)

    admin_section = (
        "\n\n🔑 <b>Панель администратора:</b>"
        "\n/caption — управление подписью под видео"
        if is_adm else ""
    )

    ig_cookies_status = (
        f"✅ Cookies: <code>{IG_COOKIES}</code>"
        if IG_COOKIES and os.path.isfile(IG_COOKIES)
        else "⚠️ Cookies Instagram не найдены — публичные посты могут не скачиваться"
    )

    await message.reply_text(
        "👋 <b>Привет! Я умею скачивать видео и фото.</b>\n\n"
        "Просто отправь ссылку — получишь медиа прямо в чат.\n\n"
        "📱 <b>Поддерживаемые платформы:</b>\n"
        "• Instagram — Reels, посты (фото/карусели), IGTV\n"
        "• TikTok — обычные и короткие видео\n"
        "• YouTube — видео и Shorts\n\n"
        f"{ig_cookies_status}\n\n"
        "🍪 <b>Как настроить cookies Instagram:</b>\n"
        "1. Установи расширение <b>«Get cookies.txt LOCALLY»</b>\n"
        "   (Chrome / Firefox)\n"
        "2. Войди на instagram.com в браузере\n"
        "3. Нажми расширение → <b>Export cookies</b>\n"
        "4. Сохрани файл как <code>instagram_cookies.txt</code>\n"
        "   рядом с ботом (или укажи путь в <code>IG_COOKIES</code>)\n\n"
        "⚙️ <b>Команды:</b>\n"
        "/quality — выбрать качество YouTube\n"
        "           (360p / 480p / 720p / 1080p)\n"
        "/start — показать это сообщение"
        f"{admin_section}\n\n"
        "✂️ Большие видео (> 50 МБ) автоматически нарезаются на части.\n"
        "   Для нарезки нужен <b>ffmpeg</b>: <code>apt install ffmpeg</code>\n"
        "📸 Карусели (до 10 фото/видео) отправляются одним альбомом.",
        parse_mode="HTML",
    )


# ─── КОМАНДА /caption ──────────────────────────────────────────────────────────

async def cmd_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global global_caption
    message = get_message(update)
    if not message:
        return

    from_user = message.from_user
    args = context.args

    if args:
        if not from_user or not is_admin(from_user.id):
            await message.reply_text("🚫 Только администратор может менять подпись.")
            return
        await _apply_caption(message, " ".join(args), from_user.id)
        return

    caption_text = (
        f"📝 Текущая подпись:\n<b>{global_caption}</b>" if global_caption
        else "📝 Подпись <b>не задана</b> — медиа отправляется без подписи."
    )

    if from_user and is_admin(from_user.id):
        await message.reply_text(
            caption_text + "\n\nВыбери действие:",
            reply_markup=admin_caption_keyboard(),
            parse_mode="HTML",
        )
    else:
        await message.reply_text(caption_text, parse_mode="HTML")


async def _apply_caption(message, text: str, admin_id: int):
    global global_caption
    if text.lower() == "off":
        global_caption = None
        await message.reply_text("✅ Подпись убрана — медиа будет отправляться без подписи.")
        log.info(f"Подпись убрана (admin={admin_id})")
    else:
        global_caption = text
        await message.reply_text(f"✅ Подпись установлена:\n{text}")
        log.info(f"Подпись изменена (admin={admin_id}): {text}")


# ─── КОМАНДА /quality ──────────────────────────────────────────────────────────

async def cmd_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = get_message(update)
    if not message:
        return

    chat_id = message.chat_id
    args = context.args
    current = user_yt_quality.get(chat_id, DEFAULT_YT_QUALITY)

    if args:
        q = args[0].strip().rstrip("p")
        if q not in YOUTUBE_QUALITIES:
            await message.reply_text(
                f"❌ Неверное качество: {args[0]}\n"
                f"Доступные варианты: {', '.join(YOUTUBE_QUALITIES.keys())}"
            )
            return
        user_yt_quality[chat_id] = q
        warn = " ⚠️ Длинные видео могут превысить лимит 50 МБ!" if q == "1080" else ""
        await message.reply_text(f"✅ Качество YouTube установлено: {q}p{warn}")
        return

    await message.reply_text(
        f"🎬 Текущее качество YouTube: <b>{current}p</b>\n\n"
        f"⚠️ Telegram не принимает файлы > 50 МБ.\n"
        f"Для длинных видео рекомендуется 360p или 480p.\n\n"
        f"Выбери качество:",
        reply_markup=admin_quality_keyboard(current),
        parse_mode="HTML",
    )


# ─── КОМАНДА /cancel ───────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = get_message(update)
    if not message:
        return
    chat_id = message.chat_id
    if chat_id in waiting_for_caption:
        waiting_for_caption.discard(chat_id)
        await message.reply_text("❌ Ввод подписи отменён.")
    else:
        await message.reply_text("Нечего отменять.")


# ─── ОБРАБОТЧИК ИНЛАЙН-КНОПОК ─────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    data = query.data

    if data.startswith("caption_"):
        if not is_admin(user_id):
            await query.answer("🚫 Только администратор.", show_alert=True)
            return

        if data == "caption_off":
            global global_caption
            global_caption = None
            await query.edit_message_text("✅ Подпись убрана — медиа отправляется без подписи.")
            log.info(f"Подпись убрана через кнопку (admin={user_id})")

        elif data == "caption_edit":
            waiting_for_caption.add(chat_id)
            await query.edit_message_text(
                "✏️ Введи новый текст подписи следующим сообщением.\n"
                "Для отмены напиши /cancel"
            )

    elif data.startswith("quality_"):
        q = data.split("_")[1]
        if q not in YOUTUBE_QUALITIES:
            return
        user_yt_quality[chat_id] = q
        warn = " ⚠️ Длинные видео могут превысить лимит 50 МБ!" if q == "1080" else ""
        await query.edit_message_text(
            f"✅ Качество YouTube установлено: <b>{q}p</b>{warn}",
            parse_mode="HTML",
        )


# ─── СКАЧИВАНИЕ ────────────────────────────────────────────────────────────────

def download_media(url: str, tmp_dir: str, platform: str, yt_quality: str = DEFAULT_YT_QUALITY) -> list[str]:
    """
    Скачивает медиа по URL в tmp_dir.
    Возвращает список путей к скачанным файлам (видео или фото).
    Пустой список означает ошибку.
    """
    output_template = os.path.join(tmp_dir, "%(playlist_index)s_%(id)s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        # Скачиваем все элементы карусели/плейлиста (для Instagram)
        "noplaylist": False,
    }

    if platform == "instagram":
        ydl_opts["format"] = "best[ext=mp4]/best"
        # Cookies — самый надёжный способ авторизации в Instagram
        if IG_COOKIES and os.path.isfile(IG_COOKIES):
            # yt-dlp перезаписывает cookiefile после скачивания (обновляет сессию).
            # Оригинал примонтирован read-only, поэтому копируем в writable tmp_dir —
            # иначе на выходе [Errno 30] Read-only file system и скачанный файл теряется.
            cookie_copy = os.path.join(tmp_dir, "ig_cookies.txt")
            shutil.copyfile(IG_COOKIES, cookie_copy)
            ydl_opts["cookiefile"] = cookie_copy
            log.info(f"Instagram: используем cookies из {IG_COOKIES}")
        elif IG_USERNAME and IG_PASSWORD:
            # Устаревший способ — Instagram часто его блокирует
            ydl_opts["username"] = IG_USERNAME
            ydl_opts["password"] = IG_PASSWORD
            log.info("Instagram: используем логин/пароль (нестабильно)")
        else:
            log.warning("Instagram: cookies не найдены, скачивание публичных постов может не работать")
        # User-Agent реального браузера снижает вероятность блокировки
        ydl_opts["http_headers"] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        }

    elif platform == "tiktok":
        ydl_opts["format"] = "best[ext=mp4]/best"
        ydl_opts["extractor_args"] = {"tiktok": {"webpage_download": ["1"]}}
        ydl_opts["noplaylist"] = True

    elif platform == "youtube":
        ydl_opts["format"] = YOUTUBE_QUALITIES.get(yt_quality, YOUTUBE_QUALITIES[DEFAULT_YT_QUALITY])
        ydl_opts["noplaylist"] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # Собираем все скачанные медиафайлы из папки
            files = collect_media_files(tmp_dir)

            if not files:
                # Запасной вариант: попробуем имя из info
                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    filename = filename.rsplit(".", 1)[0] + ".mp4"
                if os.path.exists(filename):
                    files = [filename]

            if files:
                total_mb = sum(os.path.getsize(f) for f in files) / 1024 / 1024
                log.info(f"Скачано {len(files)} файл(ов), суммарно {total_mb:.1f} МБ: {files}")
            return files

    except yt_dlp.utils.DownloadError as e:
        log.error(f"yt-dlp ошибка [{platform}]: {e}")
    except Exception as e:
        log.error(f"Неожиданная ошибка [{platform}]: {e}")

    return []


def _shortcode_from_url(url: str) -> str | None:
    """Извлекает shortcode поста из URL Instagram."""
    m = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def download_instagram_photos(url: str, tmp_dir: str) -> list[str]:
    """
    Скачивает фото/карусель из Instagram через instaloader.
    Возвращает список путей к jpg-файлам (отсортированных по порядку).
    Пустой список — ошибка или пост не является фото.
    """
    shortcode = _shortcode_from_url(url)
    if not shortcode:
        log.error(f"instaloader: не удалось извлечь shortcode из {url}")
        return []

    try:
        L = instaloader.Instaloader(
            dirname_pattern=tmp_dir,
            filename_pattern="{shortcode}_{mediaid}",
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            post_metadata_txt_pattern="",
            quiet=True,
        )

        # Загружаем Netscape-cookies напрямую в requests-сессию instaloader.
        # (load_session_from_file ждёт собственный формат сессии, а не cookies.txt.)
        if IG_COOKIES and os.path.isfile(IG_COOKIES):
            try:
                cj = http.cookiejar.MozillaCookieJar(IG_COOKIES)
                cj.load(ignore_discard=True, ignore_expires=True)
                L.context._session.cookies.update(cj)
                who = L.test_login()
                if who:
                    L.context.username = who
                    log.info(f"instaloader: авторизован через cookies как {who}")
                else:
                    log.warning("instaloader: cookies загружены, но сессия не активна (протухли?)")
            except Exception as e:
                log.warning(f"instaloader: не удалось загрузить cookies: {e}")

        # Авторизация через логин/пароль
        if not L.context.is_logged_in and IG_USERNAME and IG_PASSWORD:
            try:
                L.login(IG_USERNAME, IG_PASSWORD)
                log.info(f"instaloader: авторизован как {IG_USERNAME}")
            except Exception as e:
                log.warning(f"instaloader: не удалось войти: {e}")

        post = instaloader.Post.from_shortcode(L.context, shortcode)

        # Если пост видео — instaloader не нужен, вернём пустой список
        # чтобы не дублировать скачивание (yt-dlp уже справился)
        if post.is_video and not post.typename == "GraphSidecar":
            log.info("instaloader: пост является видео, пропускаем")
            return []

        L.download_post(post, target=tmp_dir)

        # Собираем только jpg (не mp4, не txt)
        files = sorted(glob.glob(os.path.join(tmp_dir, "*.jpg")))
        # Если карусель содержит видео, добавим и их
        files += sorted(glob.glob(os.path.join(tmp_dir, "*.mp4")))

        if files:
            log.info(f"instaloader: скачано {len(files)} файл(ов): {files}")
        return files

    except instaloader.exceptions.InstaloaderException as e:
        log.error(f"instaloader ошибка: {e}")
    except Exception as e:
        log.error(f"instaloader неожиданная ошибка: {e}")

    return []


# ─── ОТПРАВКА ──────────────────────────────────────────────────────────────────

PLATFORM_META = {
    "instagram": {"label": "Instagram", "hint": "Возможно пост приватный или Instagram заблокировал запрос.\n💡 Для надёжной работы укажи cookies: см. /start"},
    "tiktok":    {"label": "TikTok",    "hint": "TikTok мог заблокировать запрос, попробуйте позже."},
    "youtube":   {"label": "YouTube",   "hint": "Видео могло быть удалено, приватным или заблокировано по региону."},
}


async def send_media_files(message, files: list[str], caption: str | None):
    """
    Отправляет медиафайлы в чат:
    - 1 видео → reply_video
    - 1 фото  → reply_photo
    - несколько файлов → reply_media_group (альбом, до 10 штук)
    """
    if not files:
        return False

    # Проверяем, нет ли слишком больших файлов
    oversized = [f for f in files if os.path.getsize(f) / 1024 / 1024 > 50]
    if oversized:
        return "oversized"

    # Один файл
    if len(files) == 1:
        path = files[0]
        kind = classify_file(path)
        with open(path, "rb") as f:
            if kind == "video":
                await message.reply_video(
                    video=f,
                    caption=caption or None,
                    supports_streaming=True,
                )
            else:
                # фото или неизвестный формат — шлём как фото
                await message.reply_photo(
                    photo=f,
                    caption=caption or None,
                )
        return True

    # Несколько файлов — альбом (Telegram принимает до 10)
    chunks = [files[i:i + MAX_ALBUM_SIZE] for i in range(0, len(files), MAX_ALBUM_SIZE)]
    for chunk_idx, chunk in enumerate(chunks):
        media_group = []
        open_files = []
        try:
            for idx, path in enumerate(chunk):
                kind = classify_file(path)
                f = open(path, "rb")
                open_files.append(f)
                # Подпись ставим только на первый элемент первого альбома
                item_caption = caption if (chunk_idx == 0 and idx == 0) else None
                if kind == "video":
                    media_group.append(InputMediaVideo(media=f, caption=item_caption, supports_streaming=True))
                else:
                    media_group.append(InputMediaPhoto(media=f, caption=item_caption))
            await message.reply_media_group(media=media_group)
        finally:
            for f in open_files:
                f.close()

    return True


async def process_url(
    message,
    url: str,
    platform: str,
    caption: str | None,
    yt_quality: str = DEFAULT_YT_QUALITY,
    link_only: bool = False,
    extra_text: str = "",
) -> bool:
    """
    Скачивает и отправляет медиа. После успешной отправки:
      - link_only=True  → удаляет исходное сообщение (было только ссылка)
    Возвращает True если медиа успешно отправлено.
    """
    meta = PLATFORM_META[platform]
    log.info(f"[{meta['label']}] Обрабатываю: {url}")

    quality_info = f" ({yt_quality}p)" if platform == "youtube" else ""
    status = await message.reply_text(f"⏳ Скачиваю {meta['label']}{quality_info}...")

    tmp_dir = tempfile.mkdtemp(dir=BOT_TMP_DIR)
    try:
        files = download_media(url, tmp_dir, platform=platform, yt_quality=yt_quality)

        # Если yt-dlp не справился с Instagram — пробуем instaloader (фото/карусели)
        if not files and platform == "instagram":
            await status.edit_text("⏳ Пробую альтернативный способ (фото-пост)...")
            files = download_instagram_photos(url, tmp_dir)

        if not files:
            await status.edit_text(
                f"❌ Не удалось скачать медиа.\n{meta['hint']}\n🔗 {url}"
            )
            return False

        # Нарезаем большие видео на части если нужно
        oversized = [f for f in files if os.path.getsize(f) / 1024 / 1024 > TG_MAX_MB]
        if oversized:
            sizes = ", ".join(f"{os.path.getsize(f)/1024/1024:.0f} МБ" for f in oversized)
            await status.edit_text(f"✂️ Видео слишком большое ({sizes}), нарезаю на части...")
            files = prepare_files_for_sending(files, tmp_dir)
            # Если после нарезки всё равно есть oversized (фото или нарезка не удалась)
            still_oversized = [f for f in files if os.path.getsize(f) / 1024 / 1024 > TG_MAX_MB]
            if still_oversized:
                sizes2 = ", ".join(f"{os.path.getsize(f)/1024/1024:.0f} МБ" for f in still_oversized)
                tip = "\n💡 Попробуй уменьшить качество: /quality" if platform == "youtube" else ""
                await status.edit_text(
                    f"⚠️ Не удалось разбить файл(ы) ({sizes2}).\n"
                    f"Возможно, ffmpeg не установлен или произошла ошибка.{tip}\n"
                    f"🔗 {url}"
                )
                return False

        count = len(files)
        if count > 1:
            # Определяем есть ли среди файлов части одного видео
            has_parts = any("_part" in os.path.basename(f) for f in files)
            if has_parts:
                await status.edit_text(f"📤 Отправляю видео по частям ({count} шт.)...")
            else:
                await status.edit_text(f"📤 Отправляю альбом ({count} файлов)...")
        else:
            await status.edit_text("📤 Отправляю...")

        result = await send_media_files(message, files, caption)

        if result is True:
            await status.delete()

            # Удаляем исходное сообщение если оно было только ссылкой
            is_reply = message.reply_to_message is not None
            if link_only and not is_reply:
                try:
                    await message.delete()
                    log.info(f"Исходное сообщение удалено (chat={message.chat_id})")
                except Exception as e:
                    log.warning(f"Не удалось удалить сообщение: {e} (бот не админ?)")

            return True
        else:
            await status.edit_text(f"❌ Ошибка при отправке медиа.\n🔗 {url}")
            return False

    except Exception as e:
        log.error(f"Ошибка отправки [{platform}]: {e}")
        await status.edit_text(f"❌ Ошибка при отправке: {e}")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info(f"Временная папка удалена: {tmp_dir}")


# ─── ОБРАБОТЧИК СООБЩЕНИЙ ──────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = get_message(update)
    if not message or not message.text:
        return

    text = message.text.strip()
    chat_id = message.chat_id
    from_user = message.from_user

    # ── Ожидаем новую подпись от админа ───────────────────────────────────────
    if chat_id in waiting_for_caption:
        if from_user and is_admin(from_user.id):
            waiting_for_caption.discard(chat_id)
            await _apply_caption(message, text, from_user.id)
        return

    # ── "да" → "пизда" ────────────────────────────────────────────────────────
    if text.lower() == "да":
        await message.reply_text("пизда")
        return

    ig_urls = INSTAGRAM_PATTERN.findall(text)
    tt_urls = TIKTOK_PATTERN.findall(text)
    yt_urls = YOUTUBE_PATTERN.findall(text)

    if not ig_urls and not tt_urls and not yt_urls:
        return

    # Вычисляем текст без ссылок
    all_urls = ig_urls + tt_urls + yt_urls
    text_without_urls = text
    for url in all_urls:
        text_without_urls = text_without_urls.replace(url, "")
    text_without_urls = text_without_urls.strip()

    link_only  = not text_without_urls
    extra_text = text_without_urls

    caption    = global_caption
    yt_quality = user_yt_quality.get(chat_id, DEFAULT_YT_QUALITY)

    for url in ig_urls:
        await process_url(message, url, platform="instagram", caption=caption,
                          link_only=link_only, extra_text=extra_text)
    for url in tt_urls:
        await process_url(message, url, platform="tiktok", caption=caption,
                          link_only=link_only, extra_text=extra_text)
    for url in yt_urls:
        await process_url(message, url, platform="youtube", caption=caption,
                          yt_quality=yt_quality, link_only=link_only, extra_text=extra_text)


# ─── ФОНОВАЯ ОЧИСТКА ───────────────────────────────────────────────────────────

def cleanup_stale_tmp() -> tuple[int, float]:
    """
    Удаляет папки в BOT_TMP_DIR старше STALE_FILE_AGE_SEC секунд.
    Это подчищает мусор от предыдущих крэшей бота.
    Возвращает (количество удалённых папок, освобождённые МБ).
    """
    removed = 0
    freed_bytes = 0
    now = time.time()

    try:
        for entry in os.scandir(BOT_TMP_DIR):
            if not entry.is_dir():
                continue
            try:
                age = now - entry.stat().st_mtime
                if age > STALE_FILE_AGE_SEC:
                    size = sum(
                        f.stat().st_size
                        for f in os.scandir(entry.path)
                        if f.is_file()
                    )
                    shutil.rmtree(entry.path, ignore_errors=True)
                    freed_bytes += size
                    removed += 1
                    log.info(
                        f"[cleanup] Удалена старая папка: {entry.name} "
                        f"(возраст {age/60:.0f} мин, {size/1024/1024:.1f} МБ)"
                    )
            except Exception as e:
                log.warning(f"[cleanup] Не удалось удалить {entry.path}: {e}")
    except Exception as e:
        log.error(f"[cleanup] Ошибка сканирования {BOT_TMP_DIR}: {e}")

    return removed, freed_bytes / 1024 / 1024


async def background_cleanup(interval: int = CLEANUP_INTERVAL_SEC):
    """Фоновая задача — периодически чистит BOT_TMP_DIR."""
    log.info(f"[cleanup] Запущена фоновая очистка каждые {interval // 60} мин, "
             f"папка: {BOT_TMP_DIR}, порог: {STALE_FILE_AGE_SEC // 60} мин")
    while True:
        await asyncio.sleep(interval)
        try:
            removed, freed_mb = cleanup_stale_tmp()
            if removed > 0:
                log.info(f"[cleanup] Очищено {removed} папок, освобождено {freed_mb:.1f} МБ")
        except Exception as e:
            log.error(f"[cleanup] Неожиданная ошибка: {e}")


# ─── ЗАПУСК ────────────────────────────────────────────────────────────────────

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start",   "О боте и список команд"),
        BotCommand("caption", "Показать / изменить подпись под медиа"),
        BotCommand("quality",  "Качество YouTube: 360 / 480 / 720 / 1080p"),
        BotCommand("cancel",   "Отменить ввод подписи"),
    ])
    log.info("Меню команд зарегистрировано.")

    # Чистим мусор от предыдущего запуска сразу при старте
    removed, freed_mb = cleanup_stale_tmp()
    if removed > 0:
        log.info(f"[startup cleanup] Удалено {removed} старых папок, освобождено {freed_mb:.1f} МБ")
    else:
        log.info(f"[startup cleanup] {BOT_TMP_DIR} чист")

    # Запускаем фоновую очистку
    asyncio.create_task(background_cleanup())


def main():
    if BOT_TOKEN == "ВАШ_ТОКЕН_ЗДЕСЬ":
        print("❌ Укажите BOT_TOKEN!")
        return

    if ADMIN_ID is None:
        print("⚠️  ADMIN_ID не задан — смена подписи недоступна.")
        print("   Узнать свой ID: написать @userinfobot в Telegram.")
    else:
        print(f"🔑 Администратор: {ADMIN_ID}")

    if IG_COOKIES and os.path.isfile(IG_COOKIES):
        print(f"🍪 Instagram cookies: {IG_COOKIES}")
    elif IG_USERNAME:
        print(f"🔐 Instagram логин: {IG_USERNAME} (нестабильно, лучше использовать cookies)")
    else:
        print("⚠️  Instagram cookies не найдены.")
        print("   Создайте instagram_cookies.txt (см. /start в боте) для надёжной работы.")

    print("🤖 Бот запускается...")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("caption", cmd_caption))
    app.add_handler(CommandHandler("quality",  cmd_quality))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Бот работает! Поддерживает Instagram (видео + фото + карусели), TikTok и YouTube.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()