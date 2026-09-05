"""
Telegram-бот для загрузки Instagram (Reels, фото, карусели), TikTok и YouTube.

Архитектура: один процесс, одна очередь, тяжёлая работа в подпроцессах.
  • хэндлер кладёт задачу в asyncio.Queue и отвечает за миллисекунды;
  • воркер(ы) берут задачи по одной и запускают yt-dlp / instaloader / ffmpeg
    через asyncio.create_subprocess_exec с таймаутами — event loop свободен;
  • файлы отправляются потоково (InputFile(read_file_handle=False)), в памяти
    процесса бота никогда не лежит целое видео;
  • cookies Instagram у каждого пользователя свои (см. cookies.py, docs/COOKIES.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
    InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, Message, Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, Defaults, MessageHandler, filters,
)

from . import config, media
from .cookies import CookieError, CookieStore, looks_like_header_string, summarize, write_back_shared
from .state import State

log = logging.getLogger("instaload.bot")

# ─── Паттерны ссылок ──────────────────────────────────────────────────────────

INSTAGRAM_PATTERN = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:reels?|p|tv)/[A-Za-z0-9_-]+/?(?:\?[^\s]*)?"
)
TIKTOK_PATTERN = re.compile(
    r"https?://(?:www\.|m\.)?(?:tiktok\.com/@[\w.]+/(?:video|photo)/\d+|(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+|"
    r"tiktok\.com/t/[A-Za-z0-9]+)/?(?:\?[^\s]*)?"
)
YOUTUBE_PATTERN = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^&\s]+&)*v=|shorts/|embed/|live/)|youtu\.be/)"
    r"[A-Za-z0-9_-]+(?:[&?][^\s]*)?"
)

PLATFORMS = (
    ("instagram", INSTAGRAM_PATTERN, "Instagram"),
    ("tiktok", TIKTOK_PATTERN, "TikTok"),
    ("youtube", YOUTUBE_PATTERN, "YouTube"),
)
LABELS = {p: label for p, _, label in PLATFORMS}

DEFAULT_HINTS = {
    "instagram": "Пост приватный, удалён или Instagram ограничил запросы с сервера.",
    "tiktok": "TikTok мог заблокировать запрос, попробуй позже.",
    "youtube": "Видео могло быть удалено, приватным или недоступным с IP сервера.",
}

PHOTO_MAX_MB = 10   # лимит Telegram на sendPhoto; больше — отправляем документом


def extract_urls(text: str) -> list[tuple[str, str]]:
    """[(platform, url), ...] в порядке появления в тексте."""
    found: list[tuple[int, str, str]] = []
    for platform, pattern, _ in PLATFORMS:
        for m in pattern.finditer(text):
            found.append((m.start(), platform, m.group(0)))
    found.sort()
    return [(p, u) for _, p, u in found]


# ─── Задача очереди ───────────────────────────────────────────────────────────

@dataclass
class Job:
    id: str
    url: str
    platform: str
    message: Message           # исходное сообщение со ссылкой
    status: Message            # наше сообщение-статус, которое редактируем
    chat_id: int
    user_id: int | None        # None для постов канала
    quality: str
    caption: str | None
    link_only: bool
    created_at: float = field(default_factory=time.time)

    @property
    def tag(self) -> str:
        return f"[{self.id} chat={self.chat_id} user={self.user_id}]"


# ─── Бот ──────────────────────────────────────────────────────────────────────

class InstaloadBot:
    def __init__(self) -> None:
        config.ensure_dirs()
        self.state = State(config.STATE_FILE)
        self.cookies = CookieStore(config.COOKIES_DIR)
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=config.QUEUE_MAX)
        self.pending: dict[int | None, int] = {}     # задач в очереди по пользователям
        self.active: dict[str, Job] = {}             # выполняемые задачи
        self.waiting_caption: set[int] = set()       # чаты, где админ вводит подпись
        self.update_running = False
        self.bot_username = ""
        self.tasks: list[asyncio.Task] = []
        self.app: Application = self._build_app()

    # ── сборка приложения ────────────────────────────────────────────────────

    def _build_app(self) -> Application:
        app = (
            ApplicationBuilder()
            .token(config.BOT_TOKEN)
            .defaults(Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True)))
            .post_init(self._post_init)
            .post_stop(self._post_stop)
            .post_shutdown(self._post_shutdown)
            .concurrent_updates(8)
            .connect_timeout(30)
            .pool_timeout(30)
            .read_timeout(config.TG_READ_TIMEOUT)
            .write_timeout(config.TG_WRITE_TIMEOUT)
            .media_write_timeout(config.TG_MEDIA_WRITE_TIMEOUT)
            .build()
        )
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("caption", self.cmd_caption))
        app.add_handler(CommandHandler("quality", self.cmd_quality))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("cookies", self.cmd_cookies))
        app.add_handler(CommandHandler("cookies_delete", self.cmd_cookies_delete))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("update", self.cmd_update))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, self.on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return app

    async def _post_init(self, app: Application) -> None:
        me = await app.bot.get_me()
        self.bot_username = me.username or ""
        await app.bot.set_my_commands([
            BotCommand("start", "О боте и список команд"),
            BotCommand("cookies", "Мои cookies Instagram: статус и как добавить"),
            BotCommand("cookies_delete", "Удалить мои cookies Instagram"),
            BotCommand("quality", "Качество YouTube: 360 / 480 / 720p"),
            BotCommand("caption", "Подпись под медиа (админ)"),
            BotCommand("cancel", "Отменить ввод подписи"),
        ])
        removed, freed = cleanup_stale_tmp()
        log.info("startup cleanup: %d папок, %.1f МБ", removed, freed)
        # обычные asyncio-задачи (не app.create_task): это бесконечные циклы,
        # и Application.stop() иначе ждал бы их завершения вечно
        self.tasks = [asyncio.create_task(self.worker(i), name=f"worker-{i}") for i in range(config.WORKERS)]
        self.tasks.append(asyncio.create_task(self.background_cleanup(), name="cleanup"))
        log.info("Бот @%s запущен: workers=%d, yt-dlp=%s, ffmpeg=%s, tmp=%s",
                 self.bot_username, config.WORKERS, config.YTDLP_BIN, config.FFMPEG_BIN, config.TMP_DIR)

    async def _post_stop(self, app: Application) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _post_shutdown(self, app: Application) -> None:
        self.state.save()

    def run(self) -> None:
        self.app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

    # ── вспомогательное ─────────────────────────────────────────────────────

    @staticmethod
    def is_admin(user_id: int | None) -> bool:
        return config.ADMIN_ID is not None and user_id == config.ADMIN_ID

    @staticmethod
    def msg(update: Update) -> Message | None:
        return update.message or update.channel_post

    def private_link(self) -> str:
        return f"@{self.bot_username}" if self.bot_username else "боту в личные сообщения"

    # ── /start ──────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message:
            return
        uid = message.from_user.id if message.from_user else None
        has_cookies = self.cookies.has(uid)
        admin_section = (
            "\n\n🔑 <b>Администратор:</b>\n"
            "/caption — подпись под медиа\n"
            "/stats — очередь, память, диск, версия yt-dlp\n"
            "/update — обновить yt-dlp сейчас"
            if self.is_admin(uid) else ""
        )
        cookies_line = (
            "🍪 Cookies Instagram: <b>добавлены</b> — /cookies"
            if has_cookies else
            "🍪 Cookies Instagram: <b>нет</b>. Без них Instagram часто не отдаёт посты — /cookies"
        )
        await message.reply_text(
            "👋 <b>Пришли ссылку — получишь медиа прямо в чат.</b>\n\n"
            "📱 <b>Платформы:</b>\n"
            "• Instagram — Reels, посты, фото и карусели\n"
            "• TikTok — видео\n"
            "• YouTube — видео и Shorts\n\n"
            f"{cookies_line}\n\n"
            "⚙️ <b>Команды:</b>\n"
            "/cookies — добавить свои cookies Instagram (только в личке)\n"
            "/quality — качество YouTube (360 / 480 / 720p)\n"
            "/start — это сообщение"
            f"{admin_section}\n\n"
            "ℹ️ Ссылки обрабатываются по очереди. Видео больше 50 МБ режется на части, "
            "для YouTube бот сначала пытается подобрать качество, которое влезет целиком.",
            parse_mode=ParseMode.HTML
        )

    # ── /caption ────────────────────────────────────────────────────────────

    def _caption_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить подпись", callback_data="caption_edit")],
            [InlineKeyboardButton("🗑 Убрать подпись", callback_data="caption_off")],
        ])

    async def cmd_caption(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message:
            return
        uid = message.from_user.id if message.from_user else None
        if context.args:
            if not self.is_admin(uid):
                await message.reply_text("🚫 Только администратор может менять подпись.")
                return
            await self._apply_caption(message, " ".join(context.args), uid)
            return
        text = (
            f"📝 Текущая подпись:\n<b>{html.escape(self.state.caption)}</b>" if self.state.caption
            else "📝 Подпись <b>не задана</b> — медиа отправляется без подписи."
        )
        if self.is_admin(uid):
            await message.reply_text(text + "\n\nВыбери действие:", reply_markup=self._caption_keyboard(),
                                     parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _apply_caption(self, message: Message, text: str, admin_id: int | None) -> None:
        if text.strip().lower() == "off":
            self.state.set_caption(None)
            await message.reply_text("✅ Подпись убрана.")
        else:
            self.state.set_caption(text.strip())
            await message.reply_text(f"✅ Подпись установлена:\n{text.strip()}")
        log.info("caption изменена admin=%s: %r", admin_id, self.state.caption)

    # ── /quality ────────────────────────────────────────────────────────────

    def _quality_keyboard(self, current: str) -> InlineKeyboardMarkup:
        row = [InlineKeyboardButton(f"{'✅ ' if q == current else ''}{q}p", callback_data=f"quality_{q}")
               for q in config.YT_QUALITIES]
        return InlineKeyboardMarkup([row])

    async def cmd_quality(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message:
            return
        chat_id = message.chat_id
        current = self.state.quality.get(chat_id, config.DEFAULT_YT_QUALITY)
        if context.args:
            q = context.args[0].strip().lower().rstrip("p")
            if q not in config.YT_QUALITIES:
                await message.reply_text(f"❌ Доступно: {', '.join(config.YT_QUALITIES)}")
                return
            self.state.set_quality(chat_id, q)
            await message.reply_text(f"✅ Качество YouTube: {q}p")
            return
        await message.reply_text(
            f"🎬 Качество YouTube для этого чата: <b>{current}p</b>\n\n"
            "Если видео в выбранном качестве не влезает в 50 МБ, бот попробует меньшее; "
            "если и 360p не влезает — нарежет на части.",
            reply_markup=self._quality_keyboard(current), parse_mode=ParseMode.HTML,
        )

    # ── /cancel ─────────────────────────────────────────────────────────────

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message:
            return
        if message.chat_id in self.waiting_caption:
            self.waiting_caption.discard(message.chat_id)
            await message.reply_text("❌ Ввод подписи отменён.")
        else:
            await message.reply_text("Нечего отменять.")

    # ── /cookies ────────────────────────────────────────────────────────────

    COOKIES_HOWTO = (
        "🍪 <b>Как добавить свои cookies Instagram</b>\n\n"
        "Cookies — это твоя сессия Instagram. Бот использует их <b>только для твоих ссылок</b>, "
        "хранит в отдельном файле, доступном одному системному пользователю сервера, "
        "и никому не показывает. Удалить можно в любой момент: /cookies_delete.\n\n"
        "<b>Способ 1 — файл (компьютер):</b>\n"
        "1. Поставь расширение «Get cookies.txt LOCALLY» (Chrome/Edge) или «cookies.txt» (Firefox).\n"
        "2. Открой instagram.com и войди в аккаунт.\n"
        "3. Нажми на расширение → <b>Export</b> → сохранится <code>instagram.com_cookies.txt</code>.\n"
        "4. Отправь этот файл мне <b>сюда, в личные сообщения</b>.\n\n"
        "<b>Способ 2 — строка (телефон/любой браузер):</b>\n"
        "Пришли мне одним сообщением строку вида\n"
        "<code>sessionid=…; csrftoken=…; ds_user_id=…</code>\n"
        "(значения — из DevTools → Application → Cookies → instagram.com).\n\n"
        "После сохранения я удалю твоё сообщение с cookies из чата и проверю, что сессия живая.\n\n"
        "⚠️ Лучше завести отдельный аккаунт Instagram для бота: при активных загрузках "
        "Instagram может попросить подтвердить вход. Cookies действуют, пока ты не выйдешь "
        "из аккаунта в том браузере, где их экспортировал."
    )

    async def cmd_cookies(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message or not message.from_user:
            return
        if message.chat.type != ChatType.PRIVATE:
            await message.reply_text(
                f"🔒 Cookies добавляются только в личных сообщениях — напиши {self.private_link()} и отправь /cookies."
            )
            return
        uid = message.from_user.id
        if self.cookies.has(uid):
            meta = self.cookies.meta(uid)
            who = f"аккаунт <b>@{html.escape(meta.username)}</b>" if meta.username else "аккаунт не проверен"
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(meta.updated_at)) if meta.updated_at else "—"
            await message.reply_text(
                f"🍪 Твои cookies Instagram: <b>добавлены</b> ({who}, {meta.count} cookies, обновлены {when}).\n\n"
                "Чтобы заменить — просто пришли новый файл или строку.\n"
                "Удалить: /cookies_delete",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить мои cookies",
                                                                          callback_data="cookies_delete")]]),
            )
        else:
            await message.reply_text(self.COOKIES_HOWTO, parse_mode=ParseMode.HTML)

    async def cmd_cookies_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message or not message.from_user:
            return
        if self.cookies.delete(message.from_user.id):
            await message.reply_text("🗑 Твои cookies удалены.")
        else:
            await message.reply_text("У тебя нет сохранённых cookies.")

    async def _store_cookies(self, message: Message, raw: bytes | str, source: str) -> None:
        """Общий путь для файла и строки: сохранить → удалить сообщение → проверить сессию."""
        uid = message.from_user.id  # type: ignore[union-attr]
        try:
            cookies_list, _ = self.cookies.save(uid, raw)
        except CookieError as e:
            await message.reply_text(
                f"❌ Не похоже на cookies Instagram: {e}.\n\nКак экспортировать правильно — /cookies",
            )
            return

        # убираем cookies из истории чата (в личке бот может удалить сообщение пользователя)
        deleted = False
        with contextlib.suppress(TelegramError):
            await message.delete()
            deleted = True

        note = await message.chat.send_message(
            f"✅ Cookies сохранены ({html.escape(summarize(cookies_list))}).\n"
            + ("🧹 Сообщение с cookies удалено из чата.\n" if deleted else
               "⚠️ Не смог удалить твоё сообщение — удали его сам.\n")
            + "⏳ Проверяю сессию…",
            parse_mode=ParseMode.HTML,
        )
        log.info("cookies user=%s получены (%s)", uid, source)

        check = await media.check_ig_cookies(self.cookies.path_for(uid))  # type: ignore[arg-type]
        meta = self.cookies.meta(uid)
        if check.ok and check.username:
            meta.username, meta.checked = check.username, True
            self.cookies.save_meta(uid, meta)
            result = f"✅ Сессия активна: <b>@{html.escape(check.username)}</b>. Теперь твои ссылки Instagram скачиваются с твоим аккаунтом."
        else:
            result = (f"⚠️ Cookies сохранены, но проверить сессию не удалось: {html.escape(check.error or 'нет ответа')}.\n"
                      "Если загрузки не пойдут — экспортируй cookies заново, не выходя из аккаунта.")
        with contextlib.suppress(TelegramError):
            await note.edit_text(result, parse_mode=ParseMode.HTML)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message or not message.document or not message.from_user:
            return
        doc = message.document
        name = (doc.file_name or "").lower()
        if not (name.endswith(".txt") or name.endswith(".json") or "cookie" in name):
            return
        if doc.file_size and doc.file_size > config.COOKIES_MAX_BYTES:
            await message.reply_text(f"❌ Файл слишком большой для cookies (> {config.COOKIES_MAX_BYTES // 1024} КБ).")
            return
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
        except TelegramError as e:
            await message.reply_text(f"❌ Не удалось получить файл: {e}")
            return
        await self._store_cookies(message, raw, f"файл {doc.file_name}")

    # ── /stats, /update (админ) ─────────────────────────────────────────────

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message or not message.from_user or not self.is_admin(message.from_user.id):
            return
        uptime = int(time.time() - self.state.started_at)
        h, m = divmod(uptime // 60, 60)
        ver = await media.ytdlp_version()
        active = "\n".join(f"  • {j.id} {LABELS[j.platform]} chat={j.chat_id}" for j in self.active.values()) or "  —"
        await message.reply_text(
            f"📊 <b>Состояние</b>\n"
            f"Uptime: {h // 24}д {h % 24}ч {m}м\n"
            f"Очередь: {self.queue.qsize()} / {config.QUEUE_MAX}, воркеров: {config.WORKERS}\n"
            f"Выполняется:\n{active}\n"
            f"Готово / ошибок: {self.state.stats['done']} / {self.state.stats['failed']}\n"
            f"RSS процесса: {media.process_rss_mb():.0f} МБ\n"
            f"Свободно в tmp: {media.disk_free_mb(config.TMP_DIR):.0f} МБ\n"
            f"yt-dlp: {html.escape(ver)}\n"
            f"ffmpeg: {'ок' if os.access(config.FFMPEG_BIN, os.X_OK) else '❌ не найден'}\n"
            f"Пользователей с cookies: {self.cookies.count()}\n"
            f"Общие cookies IG: {'да' if config.IG_COOKIES_SHARED and os.path.isfile(config.IG_COOKIES_SHARED) else 'нет'}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message or not message.from_user or not self.is_admin(message.from_user.id):
            return
        if self.update_running:
            await message.reply_text("⏳ Обновление уже идёт.")
            return
        script = config.SCRIPTS_DIR / "update_ytdlp.sh"
        if not script.is_file():
            await message.reply_text(f"❌ Нет скрипта {script}")
            return
        self.update_running = True
        status = await message.reply_text("⏳ Обновляю yt-dlp и прогоняю smoke-тест…")
        try:
            res = await media.run(["/bin/bash", str(script)], timeout=config.UPDATE_TIMEOUT_SEC)
            out = (res.stdout + "\n" + res.stderr).strip()[-1500:]
            verdict = "✅ Готово" if res.ok else ("⏰ Таймаут" if res.timed_out else f"❌ Ошибка (exit {res.returncode})")
            await status.edit_text(f"{verdict}\n<pre>{html.escape(out) or '—'}</pre>", parse_mode=ParseMode.HTML)
        finally:
            self.update_running = False

    # ── inline-кнопки ───────────────────────────────────────────────────────

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data or not query.message:
            return
        uid = query.from_user.id
        chat_id = query.message.chat_id
        data = query.data

        if data.startswith("caption_"):
            if not self.is_admin(uid):
                await query.answer("🚫 Только администратор.", show_alert=True)
                return
            await query.answer()
            if data == "caption_off":
                self.state.set_caption(None)
                await query.edit_message_text("✅ Подпись убрана.")
            elif data == "caption_edit":
                self.waiting_caption.add(chat_id)
                await query.edit_message_text("✏️ Введи новый текст подписи следующим сообщением.\nОтмена: /cancel")

        elif data.startswith("quality_"):
            q = data.split("_", 1)[1]
            await query.answer()
            if q in config.YT_QUALITIES:
                self.state.set_quality(chat_id, q)
                await query.edit_message_text(f"✅ Качество YouTube: <b>{q}p</b>", parse_mode=ParseMode.HTML)

        elif data == "cookies_delete":
            await query.answer()
            if self.cookies.delete(uid):
                await query.edit_message_text("🗑 Твои cookies удалены.")
            else:
                await query.edit_message_text("У тебя нет сохранённых cookies.")
        else:
            await query.answer()

    # ── текстовые сообщения ─────────────────────────────────────────────────

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self.msg(update)
        if not message or not message.text:
            return
        text = message.text.strip()
        chat_id = message.chat_id
        user = message.from_user
        uid = user.id if user else None

        if chat_id in self.waiting_caption:
            if self.is_admin(uid):
                self.waiting_caption.discard(chat_id)
                await self._apply_caption(message, text, uid)
            return

        # строка cookies в личке
        if user and message.chat.type == ChatType.PRIVATE and looks_like_header_string(text):
            await self._store_cookies(message, text, "строка")
            return

        if config.EASTER_EGG and text.lower() == "да":
            await message.reply_text("пизда")
            return

        urls = extract_urls(text)
        if not urls:
            return

        rest = text
        for _, u in urls:
            rest = rest.replace(u, "")
        link_only = not rest.strip()

        for platform, url in urls:
            await self.enqueue(message, platform, url, uid, link_only)

    # ── очередь ─────────────────────────────────────────────────────────────

    async def enqueue(self, message: Message, platform: str, url: str, uid: int | None, link_only: bool) -> None:
        if self.pending.get(uid, 0) >= config.QUEUE_PER_USER:
            await message.reply_text(f"⏳ У тебя уже {config.QUEUE_PER_USER} ссылки в очереди — дождись их.")
            return
        if self.queue.full():
            await message.reply_text("🚦 Очередь переполнена, попробуй через пару минут.")
            return

        ahead = self.queue.qsize() + len(self.active)
        label = LABELS[platform]
        quality = self.state.quality.get(message.chat_id, config.DEFAULT_YT_QUALITY)
        qinfo = f" ({quality}p)" if platform == "youtube" else ""
        status_text = (f"⏳ {label}{qinfo}: в очереди, впереди {ahead}" if ahead
                       else f"⏳ Скачиваю {label}{qinfo}…")
        status = await message.reply_text(status_text)

        job = Job(
            id=secrets.token_hex(3), url=url, platform=platform, message=message, status=status,
            chat_id=message.chat_id, user_id=uid, quality=quality, caption=self.state.caption,
            link_only=link_only,
        )
        self.pending[uid] = self.pending.get(uid, 0) + 1
        self.queue.put_nowait(job)
        log.info("%s поставлена в очередь: %s (впереди %d)", job.tag, url, ahead)

    async def worker(self, n: int) -> None:
        log.info("worker-%d запущен", n)
        while True:
            job = await self.queue.get()
            self.active[job.id] = job
            try:
                await self.process(job)
            except Exception:  # noqa: BLE001
                log.exception("%s необработанная ошибка воркера", job.tag)
            finally:
                self.active.pop(job.id, None)
                self.pending[job.user_id] = max(0, self.pending.get(job.user_id, 1) - 1)
                self.queue.task_done()

    # ── обработка задачи ────────────────────────────────────────────────────

    def _resolve_cookies(self, job: Job) -> tuple[Path | None, str]:
        """(путь к cookies, 'user' | 'shared' | ''). Свои cookies пользователя всегда в приоритете."""
        if job.platform == "instagram":
            own = self.cookies.path_for(job.user_id)
            if own:
                return own, "user"
            if config.IG_COOKIES_SHARED and os.path.isfile(config.IG_COOKIES_SHARED):
                return Path(config.IG_COOKIES_SHARED), "shared"
        elif job.platform == "youtube" and config.YT_COOKIES and os.path.isfile(config.YT_COOKIES):
            return Path(config.YT_COOKIES), "shared"
        return None, ""

    async def _edit(self, status: Message, text: str) -> None:
        with contextlib.suppress(TelegramError):
            await status.edit_text(text)

    async def process(self, job: Job) -> None:
        label = LABELS[job.platform]
        qinfo = f" ({job.quality}p)" if job.platform == "youtube" else ""
        t0 = time.monotonic()
        log.info("%s старт: %s", job.tag, job.url)
        await self._edit(job.status, f"⏳ Скачиваю {label}{qinfo}…")

        tmp_dir = Path(tempfile.mkdtemp(dir=config.TMP_DIR, prefix=f"{job.id}_"))
        try:
            cookies_src, owner = self._resolve_cookies(job)
            cookies_copy: Path | None = None
            if cookies_src:
                # yt-dlp перезаписывает cookiefile; работаем с копией и потом сохраняем её обратно
                cookies_copy = tmp_dir / "ig_cookies.txt"
                shutil.copyfile(cookies_src, cookies_copy)
                os.chmod(cookies_copy, 0o600)

            files: list[Path] = []
            error = ""
            res = await media.download_ytdlp(job.url, job.platform, tmp_dir, job.quality, cookies_copy)
            files, error = res.files, res.error
            if res.detail and not files:
                log.warning("%s yt-dlp: %s", job.tag, res.detail.replace("\n", " | "))

            if not files and job.platform == "instagram" and not res.timed_out:
                await self._edit(job.status, "⏳ Пробую как фото-пост…")
                meta = self.cookies.meta(job.user_id) if owner == "user" and job.user_id else None
                hres = await media.download_instaloader(job.url, tmp_dir, cookies_copy,
                                                        meta.username if meta else None)
                if hres.username and meta is not None and meta.username != hres.username:
                    meta.username, meta.checked = hres.username, True
                    self.cookies.save_meta(job.user_id, meta)  # type: ignore[arg-type]
                if hres.files:
                    files = hres.files
                elif hres.error and not hres.skipped:
                    log.warning("%s instaloader: %s", job.tag, hres.error)
                    error = error or (
                        "Instagram требует авторизацию. Добавь свои cookies: /cookies"
                        if "авториз" in hres.error or "login" in hres.error.lower() else ""
                    )

            # сохраняем обновлённые cookies (ротация Instagram)
            if cookies_copy and job.platform == "instagram":
                if owner == "user":
                    self.cookies.write_back(job.user_id, cookies_copy)
                elif owner == "shared":
                    write_back_shared(config.IG_COOKIES_SHARED, cookies_copy)

            if not files:
                hint = error or DEFAULT_HINTS[job.platform]
                if job.platform == "instagram" and owner != "user":
                    hint += f"\n💡 Добавь свои cookies Instagram — напиши {self.private_link()} команду /cookies"
                await self._edit(job.status, f"❌ Не удалось скачать {label}.\n{hint}\n🔗 {job.url}")
                self.state.bump("failed")
                return

            total_mb = sum(media.size_mb(f) for f in files)
            log.info("%s скачано %d файл(ов), %.1f МБ за %.0f с", job.tag, len(files), total_mb, time.monotonic() - t0)

            oversized = [f for f in files if media.size_mb(f) > config.TG_MAX_MB]
            if oversized:
                sizes = ", ".join(f"{media.size_mb(f):.0f} МБ" for f in oversized)
                await self._edit(job.status, f"✂️ Видео большое ({sizes}), нарезаю на части…")
                files = await media.prepare_for_sending(files, tmp_dir)
                still = [f for f in files if media.size_mb(f) > config.TG_MAX_MB]
                if still:
                    tip = "\n💡 Попробуй /quality ниже." if job.platform == "youtube" else ""
                    await self._edit(job.status, f"⚠️ Не удалось уложить файл в 50 МБ "
                                     f"({', '.join(f'{media.size_mb(f):.0f} МБ' for f in still)}).{tip}\n🔗 {job.url}")
                    self.state.bump("failed")
                    return

            n = len(files)
            is_parts = n > 1 and all(media.classify_file(f) == "video" for f in files) and len(oversized) > 0
            await self._edit(job.status, f"📤 Отправляю видео по частям ({n})…" if is_parts
                             else f"📤 Отправляю альбом ({n} файлов)…" if n > 1 else "📤 Отправляю…")

            await self.send_files(job.message, files, job.caption, parts=is_parts)

            with contextlib.suppress(TelegramError):
                await job.status.delete()
            if job.link_only and job.message.reply_to_message is None:
                with contextlib.suppress(TelegramError):
                    await job.message.delete()
            self.state.bump("done")
            log.info("%s готово за %.0f с", job.tag, time.monotonic() - t0)

        except TelegramError as e:
            log.error("%s ошибка Telegram: %s", job.tag, e)
            await self._edit(job.status, f"❌ Ошибка при отправке: {e}\n🔗 {job.url}")
            self.state.bump("failed")
        except Exception as e:  # noqa: BLE001
            log.exception("%s ошибка", job.tag)
            await self._edit(job.status, f"❌ Внутренняя ошибка: {type(e).__name__}\n🔗 {job.url}")
            self.state.bump("failed")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── отправка (потоковая) ────────────────────────────────────────────────

    @staticmethod
    def _stream(fh, path: Path) -> InputFile:
        # read_file_handle=False — PTB ≥ 21.5 отдаёт дескриптор httpx, файл не читается в память
        return InputFile(fh, filename=path.name, read_file_handle=False)

    async def send_files(self, message: Message, files: list[Path], caption: str | None, parts: bool) -> None:
        if len(files) == 1 or (parts and len(files) > 1):
            total = len(files)
            for idx, path in enumerate(files):
                part_caption = caption if idx == 0 else (f"Часть {idx + 1}/{total}" if parts else None)
                with open(path, "rb") as fh:
                    kind = media.classify_file(path)
                    if kind == "video":
                        await message.reply_video(video=self._stream(fh, path), caption=part_caption,
                                                  supports_streaming=True)
                    elif kind == "photo" and media.size_mb(path) <= PHOTO_MAX_MB:
                        await message.reply_photo(photo=self._stream(fh, path), caption=part_caption)
                    else:
                        await message.reply_document(document=self._stream(fh, path), caption=part_caption)
            return

        # альбом: до 10 элементов в группе, подпись только на первом
        for start in range(0, len(files), config.MAX_ALBUM_SIZE):
            chunk = files[start:start + config.MAX_ALBUM_SIZE]
            with contextlib.ExitStack() as stack:
                group = []
                for idx, path in enumerate(chunk):
                    fh = stack.enter_context(open(path, "rb"))
                    item_caption = caption if (start == 0 and idx == 0) else None
                    if media.classify_file(path) == "video":
                        group.append(InputMediaVideo(media=self._stream(fh, path), caption=item_caption,
                                                     supports_streaming=True))
                    else:
                        group.append(InputMediaPhoto(media=self._stream(fh, path), caption=item_caption))
                await message.reply_media_group(media=group)

    # ── фоновая очистка ─────────────────────────────────────────────────────

    async def background_cleanup(self) -> None:
        while True:
            await asyncio.sleep(config.CLEANUP_INTERVAL_SEC)
            try:
                removed, freed = cleanup_stale_tmp(skip=set(j.id for j in self.active.values()))
                if removed:
                    log.info("cleanup: %d папок, %.1f МБ", removed, freed)
            except Exception:  # noqa: BLE001
                log.exception("cleanup: ошибка")


def cleanup_stale_tmp(skip: set[str] | None = None) -> tuple[int, float]:
    """Удаляет каталоги в TMP_DIR старше STALE_FILE_AGE_SEC (мусор после крэшей)."""
    removed, freed = 0, 0
    now = time.time()
    skip = skip or set()
    try:
        entries = list(os.scandir(config.TMP_DIR))
    except OSError as e:
        log.error("cleanup: %s", e)
        return 0, 0.0
    for entry in entries:
        if not entry.is_dir() or entry.name.split("_", 1)[0] in skip:
            continue
        try:
            if now - entry.stat().st_mtime > config.STALE_FILE_AGE_SEC:
                size = sum(f.stat().st_size for f in os.scandir(entry.path) if f.is_file())
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
                freed += size
        except OSError as e:
            log.warning("cleanup: %s: %s", entry.path, e)
    return removed, freed / 1024 / 1024


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=config.LOG_LEVEL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    if not config.BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN не задан (см. /etc/instaload/instaload.env)")
    if config.ADMIN_ID is None:
        log.warning("ADMIN_ID не задан — /caption, /stats, /update недоступны")
    for name, path in (("yt-dlp", config.YTDLP_BIN), ("ffmpeg", config.FFMPEG_BIN), ("ffprobe", config.FFPROBE_BIN)):
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            log.error("%s не найден по пути %s — загрузки/нарезка не будут работать", name, path)

    InstaloadBot().run()
