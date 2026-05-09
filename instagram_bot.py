"""
Telegram-бот для загрузки Instagram Reels, TikTok и YouTube видео через yt-dlp
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
import shutil
import tempfile
import logging
import yt_dlp
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

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

# ─── ХРАНИЛИЩА ─────────────────────────────────────────────────────────────────

global_caption: str | None = None        # единая подпись, задаёт только админ
user_yt_quality: dict[int, str] = {}     # качество YouTube (chat_id → качество)

# ─── ВСПОМОГАТЕЛЬНЫЕ ───────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


def get_message(update: Update):
    """Возвращает message из любого типа апдейта."""
    return update.message or update.channel_post


# ─── КОМАНДА /caption ──────────────────────────────────────────────────────────

async def cmd_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /caption         — показать текущую подпись (доступно всем)
    /caption <текст> — установить подпись (только админ)
    /caption off     — убрать подпись (только админ)
    """
    global global_caption

    message = get_message(update)
    if not message:
        return

    args = context.args

    # Просмотр — доступен всем
    if not args:
        if global_caption:
            await message.reply_text(f"📝 Текущая подпись:\n{global_caption}")
        else:
            await message.reply_text("📝 Подпись не задана — видео отправляется без подписи.")
        return

    # Изменение — только админ
    from_user = message.from_user
    if not from_user or not is_admin(from_user.id):
        await message.reply_text("🚫 Только администратор может менять подпись.")
        if from_user:
            log.warning(f"Попытка изменить подпись от user_id={from_user.id} (не админ)")
        return

    text = " ".join(args)

    if text.lower() == "off":
        global_caption = None
        await message.reply_text("✅ Подпись убрана — видео будет отправляться без подписи.")
        log.info(f"Подпись убрана (admin={from_user.id})")
        return

    global_caption = text
    await message.reply_text(f"✅ Подпись установлена:\n{text}")
    log.info(f"Подпись изменена (admin={from_user.id}): {text}")


# ─── КОМАНДА /quality ──────────────────────────────────────────────────────────

async def cmd_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /quality        — показать текущее качество YouTube
    /quality 360    — 360p
    /quality 480    — 480p
    /quality 720    — 720p (по умолчанию)
    /quality 1080   — 1080p (риск превысить 50 МБ!)
    """
    message = get_message(update)
    if not message:
        return

    chat_id = message.chat_id
    args = context.args
    current = user_yt_quality.get(chat_id, DEFAULT_YT_QUALITY)

    if not args:
        await message.reply_text(
            f"🎬 Текущее качество YouTube: {current}p\n\n"
            f"Доступные варианты: {', '.join(YOUTUBE_QUALITIES.keys())}\n"
            f"Пример: /quality 480\n\n"
            f"⚠️ Telegram не принимает файлы > 50 МБ.\n"
            f"Для длинных видео рекомендуется 360p или 480p."
        )
        return

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


# ─── СКАЧИВАНИЕ ────────────────────────────────────────────────────────────────

def download_video(url: str, tmp_dir: str, platform: str, yt_quality: str = DEFAULT_YT_QUALITY) -> str | None:
    output_template = os.path.join(tmp_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    if platform == "instagram":
        ydl_opts["format"] = "best[ext=mp4]/best"
        if IG_USERNAME and IG_PASSWORD:
            ydl_opts["username"] = IG_USERNAME
            ydl_opts["password"] = IG_PASSWORD

    elif platform == "tiktok":
        ydl_opts["format"] = "best[ext=mp4]/best"
        ydl_opts["extractor_args"] = {"tiktok": {"webpage_download": ["1"]}}

    elif platform == "youtube":
        ydl_opts["format"] = YOUTUBE_QUALITIES.get(yt_quality, YOUTUBE_QUALITIES[DEFAULT_YT_QUALITY])

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            if not os.path.exists(filename):
                filename = filename.rsplit(".", 1)[0] + ".mp4"

            if os.path.exists(filename):
                size_mb = os.path.getsize(filename) / 1024 / 1024
                log.info(f"Скачано: {filename} ({size_mb:.1f} МБ)")
                return filename

    except yt_dlp.utils.DownloadError as e:
        log.error(f"yt-dlp ошибка [{platform}]: {e}")
    except Exception as e:
        log.error(f"Неожиданная ошибка [{platform}]: {e}")

    return None


# ─── ОТПРАВКА ──────────────────────────────────────────────────────────────────

PLATFORM_META = {
    "instagram": {
        "label": "Instagram",
        "hint":  "Возможно пост приватный или Instagram заблокировал запрос.",
    },
    "tiktok": {
        "label": "TikTok",
        "hint":  "TikTok мог заблокировать запрос, попробуйте позже.",
    },
    "youtube": {
        "label": "YouTube",
        "hint":  "Видео могло быть удалено, приватным или заблокировано по региону.",
    },
}

async def process_url(message, url: str, platform: str, caption: str | None, yt_quality: str = DEFAULT_YT_QUALITY):
    meta = PLATFORM_META[platform]
    log.info(f"[{meta['label']}] Обрабатываю: {url}")

    quality_info = f" ({yt_quality}p)" if platform == "youtube" else ""
    status = await message.reply_text(f"⏳ Скачиваю {meta['label']}{quality_info} видео...")

    tmp_dir = tempfile.mkdtemp()
    try:
        video_path = download_video(url, tmp_dir, platform=platform, yt_quality=yt_quality)

        if not video_path:
            await status.edit_text(
                f"❌ Не удалось скачать видео.\n"
                f"{meta['hint']}\n"
                f"🔗 {url}"
            )
            return

        size_mb = os.path.getsize(video_path) / 1024 / 1024

        if size_mb > 50:
            tip = "\n💡 Попробуй уменьшить качество: /quality 480" if platform == "youtube" else ""
            await status.edit_text(
                f"⚠️ Видео слишком большое ({size_mb:.0f} МБ).\n"
                f"Telegram-боты не могут отправлять файлы > 50 МБ.{tip}\n"
                f"🔗 {url}"
            )
            return

        await status.edit_text("📤 Отправляю...")
        with open(video_path, "rb") as f:
            await message.reply_video(
                video=f,
                caption=caption or None,
                supports_streaming=True,
            )
        await status.delete()

    except Exception as e:
        log.error(f"Ошибка отправки [{platform}]: {e}")
        await status.edit_text(f"❌ Ошибка при отправке видео: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info(f"Временная папка удалена: {tmp_dir}")


# ─── ОБРАБОТЧИК СООБЩЕНИЙ ──────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = get_message(update)
    if not message or not message.text:
        return

    text = message.text.strip()

    if text.lower() == "да":
        await message.reply_text("пизда")
        return

    ig_urls = INSTAGRAM_PATTERN.findall(text)
    tt_urls = TIKTOK_PATTERN.findall(text)
    yt_urls = YOUTUBE_PATTERN.findall(text)

    if not ig_urls and not tt_urls and not yt_urls:
        return

    caption    = global_caption
    yt_quality = user_yt_quality.get(message.chat_id, DEFAULT_YT_QUALITY)

    for url in ig_urls:
        await process_url(message, url, platform="instagram", caption=caption)

    for url in tt_urls:
        await process_url(message, url, platform="tiktok", caption=caption)

    for url in yt_urls:
        await process_url(message, url, platform="youtube", caption=caption, yt_quality=yt_quality)


# ─── ЗАПУСК ────────────────────────────────────────────────────────────────────

async def post_init(app):
    """Регистрируем меню команд в Telegram после старта."""
    await app.bot.set_my_commands([
        BotCommand("caption", "Показать / изменить подпись под видео"),
        BotCommand("quality",  "Качество YouTube: 360 / 480 / 720 / 1080p"),
    ])
    log.info("Меню команд зарегистрировано.")


def main():
    if BOT_TOKEN == "ВАШ_ТОКЕН_ЗДЕСЬ":
        print("❌ Укажите BOT_TOKEN!")
        print("   Получите токен у @BotFather в Telegram")
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

    app.add_handler(CommandHandler("caption", cmd_caption))
    app.add_handler(CommandHandler("quality",  cmd_quality))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Бот работает! Поддерживает Instagram, TikTok и YouTube.")
    print("   /caption <текст> | /caption off       — подпись (только админ)")
    print("   /quality <360|480|720|1080>            — качество YouTube (по умолч. 720p)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()