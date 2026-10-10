"""
Работа со ссылками: поиск в тексте, очистка от трекинга и сборка подписи
с ссылкой на первоисточник.

Модуль чистый (без Telegram и сети), поэтому легко тестируется.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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

# Лимит Telegram на подпись к медиа
CAPTION_LIMIT = 1024

# Параметры, которые имеют смысл сохранить (остальные — трекинг: igsh, utm_*, si, is_from_webapp…)
_KEEP_PARAMS = {
    "youtube": {"v", "t"},
    "instagram": {"img_index"},   # номер слайда карусели
    "tiktok": set(),
}


def extract_urls(text: str) -> list[tuple[str, str]]:
    """[(platform, url), ...] в порядке появления в тексте, без дубликатов."""
    found: list[tuple[int, str, str]] = []
    for platform, pattern, _ in PLATFORMS:
        for m in pattern.finditer(text):
            found.append((m.start(), platform, m.group(0)))
    found.sort()
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for _, platform, url in found:
        if url not in seen:
            seen.add(url)
            result.append((platform, url))
    return result


def clean_url(url: str, platform: str) -> str:
    """Убирает трекинговые параметры, оставляя ссылку рабочей."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    keep = _KEEP_PARAMS.get(platform, set())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in keep])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def source_line(url: str, platform: str) -> str:
    """HTML-строка со ссылкой на первоисточник."""
    return f'🔗 <a href="{html.escape(url, quote=True)}">Источник ({LABELS.get(platform, platform)})</a>'


def build_caption(url: str, platform: str, extra: str | None = None, *, with_source: bool = True) -> str | None:
    """
    Подпись под медиа (parse_mode=HTML): текст админа + ссылка на источник.
    Ссылка гарантированно помещается в лимит 1024 символа — при нехватке места
    обрезается текст, а не ссылка.
    """
    tail = source_line(clean_url(url, platform), platform) if with_source else ""
    text = html.escape(extra.strip()) if extra and extra.strip() else ""
    if not tail:
        return text[:CAPTION_LIMIT] or None
    if not text:
        return tail
    room = CAPTION_LIMIT - len(tail) - 2
    if len(text) > room:
        text = text[: max(0, room - 1)].rstrip() + "…"
    return f"{text}\n\n{tail}" if text else tail
