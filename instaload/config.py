"""
Настройки бота. Всё берётся из переменных окружения (systemd EnvironmentFile),
значения по умолчанию рассчитаны на раскладку из scripts/install.sh:

    /opt/instaload/app          код
    /opt/instaload/venv         виртуальное окружение
    /opt/instaload/bin          статический ffmpeg/ffprobe (read-only)
    /var/lib/instaload          состояние, cookies пользователей, кэш, yt-dlp
    /var/lib/instaload/tmp      временные файлы загрузок
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _which(name: str, fallback: str) -> str:
    """Путь к бинарнику: явный из env → в нашей bin → в PATH → как есть."""
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    found = shutil.which(name)
    return found or fallback


# ─── Telegram ─────────────────────────────────────────────────────────────────

BOT_TOKEN = _env("BOT_TOKEN")

_admin_raw = _env("ADMIN_ID")
ADMIN_ID: int | None = int(_admin_raw) if _admin_raw.isdigit() else None

# ─── Каталоги ─────────────────────────────────────────────────────────────────

DATA_DIR = Path(_env("INSTALOAD_DATA_DIR", "/var/lib/instaload"))
TMP_DIR = Path(_env("INSTALOAD_TMP_DIR", str(DATA_DIR / "tmp")))
COOKIES_DIR = DATA_DIR / "cookies"
STATE_FILE = DATA_DIR / "state.json"
YTDLP_CACHE_DIR = DATA_DIR / "cache"

APP_DIR = Path(__file__).resolve().parent.parent      # /opt/instaload/app
SCRIPTS_DIR = APP_DIR / "scripts"

# ─── Внешние бинарники ────────────────────────────────────────────────────────

YTDLP_BIN = _which("yt-dlp", _env("YTDLP_BIN", str(DATA_DIR / "bin" / "yt-dlp")))
FFMPEG_BIN = _which("ffmpeg", _env("FFMPEG_BIN", "/opt/instaload/bin/ffmpeg"))
FFPROBE_BIN = _which("ffprobe", _env("FFPROBE_BIN", "/opt/instaload/bin/ffprobe"))
# Python, в котором установлен instaloader (тот же venv, что и бот)
HELPER_PYTHON = _env("HELPER_PYTHON") or sys.executable

# ─── Cookies ──────────────────────────────────────────────────────────────────

# Необязательный общий файл cookies Instagram (Netscape), который админ кладёт
# на сервер руками. Используется ТОЛЬКО если у пользователя нет своих cookies.
# По умолчанию выключено: у каждого пользователя — свои cookies (см. docs/COOKIES.md).
IG_COOKIES_SHARED = _env("IG_COOKIES_SHARED")

# Необязательный файл cookies YouTube (помогает против «Sign in to confirm you're not a bot»).
YT_COOKIES = _env("YT_COOKIES")

# Максимальный размер файла cookies, который бот примет от пользователя.
COOKIES_MAX_BYTES = _env_int("COOKIES_MAX_BYTES", 512 * 1024)

# ─── Лимиты и таймауты ────────────────────────────────────────────────────────

WORKERS = max(1, _env_int("WORKERS", 1))                 # 1 на 1 vCPU, 2 на 2 vCPU/1 ГБ+
QUEUE_MAX = _env_int("QUEUE_MAX", 20)                    # длина очереди
QUEUE_PER_USER = _env_int("QUEUE_PER_USER", 3)           # задач одного пользователя в очереди

DOWNLOAD_TIMEOUT_SEC = _env_int("DOWNLOAD_TIMEOUT_SEC", 240)   # yt-dlp / instaloader
FFMPEG_TIMEOUT_SEC = _env_int("FFMPEG_TIMEOUT_SEC", 180)
HELPER_CHECK_TIMEOUT_SEC = _env_int("HELPER_CHECK_TIMEOUT_SEC", 40)
UPDATE_TIMEOUT_SEC = _env_int("UPDATE_TIMEOUT_SEC", 600)

TG_MAX_MB = _env_int("TG_MAX_MB", 50)          # лимит Bot API на отправку
PART_TARGET_MB = _env_int("PART_TARGET_MB", 40)  # целевой размер части (запас под VBR)
MAX_DOWNLOAD_MB = _env_int("MAX_DOWNLOAD_MB", 400)  # --max-filesize, защита диска
MAX_ALBUM_SIZE = 10

# Телеграм-таймауты на отправку больших файлов
TG_READ_TIMEOUT = _env_int("TG_READ_TIMEOUT", 60)
TG_WRITE_TIMEOUT = _env_int("TG_WRITE_TIMEOUT", 60)
TG_MEDIA_WRITE_TIMEOUT = _env_int("TG_MEDIA_WRITE_TIMEOUT", 180)

STALE_FILE_AGE_SEC = _env_int("STALE_FILE_AGE_SEC", 30 * 60)
CLEANUP_INTERVAL_SEC = _env_int("CLEANUP_INTERVAL_SEC", 15 * 60)

# ─── YouTube ──────────────────────────────────────────────────────────────────

# YT_MERGE=1: bestvideo+bestaudio с ремуксом через ffmpeg (-c copy, дёшево по CPU),
#             даёт настоящие 480/720.
# YT_MERGE=0: только прогрессивные форматы (на практике почти всегда 360p).
YT_MERGE = _env_bool("YT_MERGE", True)
YT_QUALITIES = ("360", "480", "720")
DEFAULT_YT_QUALITY = _env("DEFAULT_YT_QUALITY", "720")
if DEFAULT_YT_QUALITY not in YT_QUALITIES:
    DEFAULT_YT_QUALITY = "720"

# ─── Прочее ───────────────────────────────────────────────────────────────────

EASTER_EGG = _env_bool("EASTER_EGG", True)   # «да» → «пизда», как в оригинале
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def ensure_dirs() -> None:
    """Создаёт рабочие каталоги с безопасными правами (только владелец)."""
    for d in (DATA_DIR, TMP_DIR, COOKIES_DIR, YTDLP_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
