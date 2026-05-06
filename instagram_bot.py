"""
Telegram-бот для загрузки Instagram Reels и TikTok видео через yt-dlp
Установка:
    pip install python-telegram-bot yt-dlp

Запуск:
    python instagram_bot.py
"""

import re
import os
import shutil
import tempfile
import logging
import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ─── НАСТРОЙКИ (из переменных окружения или .env) ──────────────────────────────

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
IG_USERNAME = os.environ.get("IG_USERNAME", "")
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")

# ─── ЛОГИРОВАНИЕ ───────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── ПАТТЕРНЫ ДЛЯ ПОИСКА ССЫЛОК ────────────────────────────────────────────────

INSTAGRAM_PATTERN = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+/?(?:\?[^\s]*)?"
)

TIKTOK_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:tiktok\.com/@[\w.]+/video/\d+|vm\.tiktok\.com/[A-Za-z0-9]+|vt\.tiktok\.com/[A-Za-z0-9]+)/?(?:\?[^\s]*)?"
)

# ─── СКАЧИВАНИЕ ЧЕРЕЗ YT-DLP ──────────────────────────────────────────────────

def download_video(url: str, tmp_dir: str, is_instagram: bool = False) -> str | None:
    """Скачивает видео через yt-dlp, возвращает путь к файлу или None"""
    output_template = os.path.join(tmp_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    # Для Instagram добавляем логин если указан
    if is_instagram and IG_USERNAME and IG_PASSWORD:
        ydl_opts["username"] = IG_USERNAME
        ydl_opts["password"] = IG_PASSWORD

    # TikTok: пробуем скачать без водяного знака
    if not is_instagram:
        ydl_opts["extractor_args"] = {"tiktok": {"webpage_download": ["1"]}}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            # Если расширение изменилось после merge
            if not os.path.exists(filename):
                filename = filename.rsplit(".", 1)[0] + ".mp4"

            if os.path.exists(filename):
                size_mb = os.path.getsize(filename) / 1024 / 1024
                log.info(f"Скачано: {filename} ({size_mb:.1f} МБ)")
                return filename

    except yt_dlp.utils.DownloadError as e:
        log.error(f"yt-dlp ошибка: {e}")
    except Exception as e:
        log.error(f"Неожиданная ошибка: {e}")

    return None

# ─── ОБЩАЯ ЛОГИКА ОТПРАВКИ ─────────────────────────────────────────────────────

async def process_url(message, url: str, is_instagram: bool):
    """Скачивает видео и отправляет в чат, гарантированно удаляет временные файлы"""
    platform = "Instagram" if is_instagram else "TikTok"
    icon = "📸" if is_instagram else "🎵"
    log.info(f"[{platform}] Обрабатываю: {url}")

    status = await message.reply_text(f"⏳ Скачиваю {platform} видео...")

    tmp_dir = tempfile.mkdtemp()
    try:
        video_path = download_video(url, tmp_dir, is_instagram=is_instagram)

        if not video_path:
            err_hint = (
                "Возможно пост приватный или Instagram заблокировал запрос."
                if is_instagram else
                "TikTok мог заблокировать запрос, попробуйте позже."
            )
            await status.edit_text(f"❌ Не удалось скачать видео.\n{err_hint}\n🔗 {url}")
            return

        size_mb = os.path.getsize(video_path) / 1024 / 1024

        if size_mb > 50:
            await status.edit_text(
                f"⚠️ Видео слишком большое ({size_mb:.0f} МБ).\n"
                f"Telegram-боты не могут отправлять файлы > 50 МБ.\n"
                f"🔗 {url}"
            )
            return

        await status.edit_text("📤 Отправляю...")
        with open(video_path, "rb") as f:
            await message.reply_video(
                video=f,
                caption=f"{icon} {url}",
                supports_streaming=True,
            )
        await status.delete()

    except Exception as e:
        log.error(f"Ошибка отправки: {e}")
        await status.edit_text(f"❌ Ошибка при отправке видео: {e}")
    finally:
        # Гарантированно удаляем всю временную папку со всем содержимым
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info(f"Временная папка удалена: {tmp_dir}")

# ─── ОБРАБОТЧИК СООБЩЕНИЙ ──────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text.strip()

    # ─── "да" → "пизда" ────────────────────────────────────────────────────────
    if text.lower() == "да":
        await message.reply_text("пизда")
        return

    ig_urls = INSTAGRAM_PATTERN.findall(text)
    tt_urls = TIKTOK_PATTERN.findall(text)

    if not ig_urls and not tt_urls:
        return

    for url in ig_urls:
        await process_url(message, url, is_instagram=True)

    for url in tt_urls:
        await process_url(message, url, is_instagram=False)

# ─── ЗАПУСК ────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "ВАШ_ТОКЕН_ЗДЕСЬ":
        print("❌ Укажите BOT_TOKEN!")
        print("   Получите токен у @BotFather в Telegram")
        return

    print("🤖 Бот запускается...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Бот работает! Поддерживает Instagram и TikTok.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()