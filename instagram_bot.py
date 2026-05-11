"""
Telegram-бот для загрузки Instagram Reels/Posts, TikTok и YouTube видео через yt-dlp
Установка:
    pip install python-telegram-bot yt-dlp

Переменные окружения:
    BOT_TOKEN    — токен бота от @BotFather
    ADMIN_ID     — Telegram user_id администратора (только он может менять подпись)
    IG_USERNAME  — логин Instagram (опционально)
    IG_PASSWORD  — пароль Instagram (опционально)

Узнать свой user_id: написать боту @userinfobot

Запуск:
    python instagram_bot.py
"""

import re
import os
import glob
import shutil
import tempfile
import logging
import yt_dlp
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ─── НАСТРОЙКИ ─────────────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
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

    await message.reply_text(
        "👋 <b>Привет! Я умею скачивать видео и фото.</b>\n\n"
        "Просто отправь ссылку — получишь медиа прямо в чат.\n\n"
        "📱 <b>Поддерживаемые платформы:</b>\n"
        "• Instagram — Reels, посты (фото/карусели), IGTV\n"
        "• TikTok — обычные и короткие видео\n"
        "• YouTube — видео и Shorts\n\n"
        "⚙️ <b>Команды:</b>\n"
        "/quality — выбрать качество YouTube\n"
        "           (360p / 480p / 720p / 1080p)\n"
        "/start — показать это сообщение"
        f"{admin_section}\n\n"
        "⚠️ Максимальный размер файла — 50 МБ.\n"
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
        # Для фото: write_all_thumbnails=False, но сам yt-dlp скачает фото как .jpg
        # Для видео/reels: скачает mp4
        # write_pages=False чтобы не засорять папку
        ydl_opts["format"] = "best[ext=mp4]/best"
        if IG_USERNAME and IG_PASSWORD:
            ydl_opts["username"] = IG_USERNAME
            ydl_opts["password"] = IG_PASSWORD

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


# ─── ОТПРАВКА ──────────────────────────────────────────────────────────────────

PLATFORM_META = {
    "instagram": {"label": "Instagram", "hint": "Возможно пост приватный или Instagram заблокировал запрос."},
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

    tmp_dir = tempfile.mkdtemp()
    try:
        files = download_media(url, tmp_dir, platform=platform, yt_quality=yt_quality)

        if not files:
            await status.edit_text(
                f"❌ Не удалось скачать медиа.\n{meta['hint']}\n🔗 {url}"
            )
            return False

        # Проверяем размер до отправки
        oversized = [f for f in files if os.path.getsize(f) / 1024 / 1024 > 50]
        if oversized:
            sizes = ", ".join(f"{os.path.getsize(f)/1024/1024:.0f} МБ" for f in oversized)
            tip = "\n💡 Попробуй уменьшить качество: /quality" if platform == "youtube" else ""
            await status.edit_text(
                f"⚠️ Файл(ы) слишком большие ({sizes}).\n"
                f"Telegram-боты не могут отправлять файлы > 50 МБ.{tip}\n"
                f"🔗 {url}"
            )
            return False

        count = len(files)
        kinds = set(classify_file(f) for f in files)
        if count > 1:
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


# ─── ЗАПУСК ────────────────────────────────────────────────────────────────────

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start",   "О боте и список команд"),
        BotCommand("caption", "Показать / изменить подпись под медиа"),
        BotCommand("quality",  "Качество YouTube: 360 / 480 / 720 / 1080p"),
        BotCommand("cancel",   "Отменить ввод подписи"),
    ])
    log.info("Меню команд зарегистрировано.")


def main():
    if BOT_TOKEN == "ВАШ_ТОКЕН_ЗДЕСЬ":
        print("❌ Укажите BOT_TOKEN!")
        return

    if ADMIN_ID is None:
        print("⚠️  ADMIN_ID не задан — смена подписи недоступна.")
        print("   Узнать свой ID: написать @userinfobot в Telegram.")
    else:
        print(f"🔑 Администратор: {ADMIN_ID}")

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