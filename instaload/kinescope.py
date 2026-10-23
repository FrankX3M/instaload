"""
Kinescope: превращает ссылку-шеринг в прямую HLS-ссылку (m3u8).

Само скачивание делает yt-dlp в подпроцессе (см. media.download_ytdlp) — как и для
остальных платформ. Здесь только один короткий HTTP-запрос к API Kinescope.
Используется только стандартная библиотека: новых зависимостей нет, процесс бота
по-прежнему не импортирует ничего тяжёлого. Блокирующий urllib вынесен в поток,
event loop остаётся свободным.

Поддерживаются ссылки вида:
    https://kinescope.io/sh/<share_id>/v/<video_id>
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger("instaload.kinescope")

URL_RE = re.compile(
    r"https?://(?:www\.)?kinescope\.io/sh/(?P<share>[A-Za-z0-9_-]+)/v/(?P<video>[A-Za-z0-9_-]+)"
)

API_TIMEOUT_SEC = 20
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")


class KinescopeError(Exception):
    """Ошибка с готовым для пользователя текстом."""


def parse_url(url: str) -> tuple[str, str]:
    """(share_id, video_id) из ссылки или KinescopeError."""
    m = URL_RE.search(url)
    if not m:
        raise KinescopeError("Не удалось распознать ссылку Kinescope.")
    return m.group("share"), m.group("video")


def extract_hls(payload: object) -> str:
    """Достаёт ссылку на HLS из ответа API; KinescopeError, если её нет."""
    data = payload.get("data") if isinstance(payload, dict) else None
    hls = data.get("hls") if isinstance(data, dict) else None
    if not isinstance(hls, str) or not hls.startswith("https://"):
        raise KinescopeError("В ответе Kinescope нет ссылки на поток. Видео защищено, удалено "
                             "или ссылка устарела.")
    return hls


def _fetch_sync(url: str) -> str:
    share, video = parse_url(url)
    api_url = f"https://kinescope.io/sh/{share}/api/video/{video}"
    req = urllib.request.Request(api_url, headers={
        "Accept": "application/json",
        "User-Agent": _UA,
        "Referer": url,
    })
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SEC) as resp:  # noqa: S310 (https, фикс. хост)
            body = resp.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        log.warning("kinescope api: HTTP %s для %s", e.code, api_url)
        if e.code in (401, 403):
            raise KinescopeError("Kinescope отказал в доступе: видео приватное или ссылка устарела.") from e
        if e.code == 404:
            raise KinescopeError("Видео не найдено — возможно, удалено.") from e
        raise KinescopeError(f"Kinescope ответил ошибкой {e.code}.") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("kinescope api: %s", e)
        raise KinescopeError("Не удалось связаться с Kinescope, попробуй позже.") from e

    try:
        payload = json.loads(body)
    except ValueError as e:
        raise KinescopeError("Kinescope вернул неожиданный ответ (не JSON).") from e
    return extract_hls(payload)


async def resolve_hls(url: str) -> str:
    """Ссылка на страницу Kinescope → прямая ссылка m3u8."""
    return await asyncio.to_thread(_fetch_sync, url)
