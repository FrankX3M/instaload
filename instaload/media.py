"""
Тяжёлая работа — yt-dlp, instaloader, ffmpeg — только в подпроцессах.

Процесс бота ничего из этого не импортирует: event loop остаётся свободным,
память после каждой задачи возвращается ОС, зависший экстрактор убивается по
таймауту вместе со всей группой процессов.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path

from . import config

log = logging.getLogger("instaload.media")

# ─── Общие утилиты ────────────────────────────────────────────────────────────

_NAT_RE = re.compile(r"(\d+)")


def natural_key(p: Path | str) -> list:
    """Сортировка «1, 2, 10», а не «1, 10, 2» — важно для порядка карусели."""
    return [int(t) if t.isdigit() else t.lower() for t in _NAT_RE.split(str(p))]


def classify_file(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    if ext in config.VIDEO_EXTENSIONS:
        return "video"
    if ext in config.PHOTO_EXTENSIONS:
        return "photo"
    return "unknown"


def collect_media_files(directory: Path) -> list[Path]:
    """Все медиафайлы в каталоге (без вложенных), в естественном порядке."""
    files = [
        p for p in directory.iterdir()
        if p.is_file() and classify_file(p) != "unknown" and not p.name.startswith(".")
        and p.stat().st_size > 0
    ]
    return sorted(files, key=natural_key)


def size_mb(path: Path | str) -> float:
    return os.path.getsize(path) / 1024 / 1024


def _subprocess_env() -> dict[str, str]:
    """Минимальное окружение: наш bin впереди PATH, чтобы yt-dlp нашёл ffmpeg."""
    env = {
        "PATH": os.pathsep.join(filter(None, [
            str(Path(config.FFMPEG_BIN).parent),
            str(Path(config.YTDLP_BIN).parent),
            os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        ])),
        "HOME": os.environ.get("HOME", str(config.DATA_DIR)),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
    }
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
              "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


@dataclass
class ProcResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def stderr_tail(self, n: int = 400) -> str:
        return self.stderr.strip()[-n:]


async def run(cmd: list[str], timeout: float, cwd: Path | None = None,
              stdin_data: bytes | None = None) -> ProcResult:
    """
    Запускает команду в отдельной группе процессов, ждёт не дольше timeout,
    по истечении убивает всю группу (yt-dlp + его ffmpeg).
    """
    log.debug("run: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=_subprocess_env(),
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return ProcResult(127, "", f"не найден исполняемый файл: {e.filename}")

    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_data), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_group(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:  # noqa: BLE001
            out, err = b"", b""
        return ProcResult(proc.returncode, out.decode("utf-8", "replace"),
                          err.decode("utf-8", "replace"), timed_out=True)
    return ProcResult(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# ─── yt-dlp ───────────────────────────────────────────────────────────────────

def yt_format(quality: str) -> str:
    """
    Селектор формата YouTube с фильтром по размеру: сначала пытаемся найти качество,
    которое влезет в лимит Telegram без нарезки (понижая до 360p), и только потом —
    запрошенное качество как есть (его нарежем).
    """
    q = int(quality)
    ladder = [h for h in (720, 480, 360) if h <= q] or [360]
    vbudget = max(10, config.TG_MAX_MB - 10)   # ~40 МБ на видео
    abudget = 8

    if config.YT_MERGE:
        parts = [
            f"bv*[height<={h}][ext=mp4][filesize<{vbudget}M]+ba[ext=m4a][filesize<{abudget}M]"
            for h in ladder
        ]
        parts += [
            f"bv*[height<={q}][ext=mp4]+ba[ext=m4a]",
            f"b[height<={q}][ext=mp4]",
            f"bv*[height<={q}]+ba",
            f"b[height<={q}]",
            "b",
        ]
    else:
        parts = [f"b[height<={h}][ext=mp4][filesize<{vbudget + abudget}M]" for h in ladder]
        parts += [f"b[height<={q}][ext=mp4]", f"b[height<={q}]", "b"]
    return "/".join(parts)


def build_ytdlp_cmd(url: str, platform: str, tmp_dir: Path, quality: str,
                    cookies_file: Path | None) -> list[str]:
    cmd = [
        config.YTDLP_BIN,
        "--no-warnings", "--quiet", "--no-progress", "--no-colors",
        "--print", "after_move:filepath",
        "--paths", str(tmp_dir),
        "--output", "%(playlist_index|0)02d_%(id)s.%(ext)s",
        "--restrict-filenames", "--no-mtime", "--no-part",
        "--max-filesize", f"{config.MAX_DOWNLOAD_MB}M",
        "--socket-timeout", "30",
        "--retries", "3", "--fragment-retries", "3", "--extractor-retries", "2",
        "--cache-dir", str(config.YTDLP_CACHE_DIR),
        "--ffmpeg-location", str(Path(config.FFMPEG_BIN).parent),
        "--merge-output-format", "mp4",
    ]
    if platform == "instagram":
        cmd += ["--format", "b[ext=mp4]/b", "--yes-playlist"]
    elif platform == "tiktok":
        cmd += ["--format", "b[ext=mp4]/b", "--no-playlist"]
    elif platform == "youtube":
        cmd += ["--format", yt_format(quality), "--no-playlist"]
    if cookies_file:
        cmd += ["--cookies", str(cookies_file)]
    cmd += ["--", url]
    return cmd


@dataclass
class DownloadResult:
    files: list[Path] = field(default_factory=list)
    error: str = ""           # короткое объяснение для пользователя
    detail: str = ""          # хвост stderr для логов
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.files)


_YTDLP_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"login required|requested content is not available|rate-limit reached|"
                r"empty media response|checkpoint_required|Please wait a few minutes", re.I),
     "Instagram требует авторизацию или ограничил запросы. Добавь свои cookies: /cookies"),
    (re.compile(r"Sign in to confirm|not a bot|cookies.*youtube", re.I),
     "YouTube требует подтверждения (блокировка IP сервера). Админ может добавить cookies YouTube."),
    (re.compile(r"Private video|This video is private|is private", re.I), "Видео приватное."),
    (re.compile(r"Video unavailable|has been removed|does not exist|404", re.I),
     "Видео недоступно или удалено."),
    (re.compile(r"not available in your country|geo.?restricted|blocked it in your country", re.I),
     "Видео заблокировано по региону сервера."),
    (re.compile(r"larger than max-filesize|File is larger", re.I),
     f"Файл больше {config.MAX_DOWNLOAD_MB} МБ — слишком большой для этого бота. Попробуй /quality ниже."),
    (re.compile(r"Requested format is not available", re.I), "Нет подходящего формата."),
    (re.compile(r"Unsupported URL", re.I), "Ссылка не поддерживается."),
]


def explain_ytdlp_error(stderr: str) -> str:
    for pattern, hint in _YTDLP_HINTS:
        if pattern.search(stderr):
            return hint
    return ""


async def download_ytdlp(url: str, platform: str, tmp_dir: Path, quality: str,
                         cookies_file: Path | None) -> DownloadResult:
    cmd = build_ytdlp_cmd(url, platform, tmp_dir, quality, cookies_file)
    before = {p.name for p in tmp_dir.iterdir()}
    res = await run(cmd, timeout=config.DOWNLOAD_TIMEOUT_SEC, cwd=tmp_dir)

    if res.timed_out:
        return DownloadResult(error=f"yt-dlp не уложился в {config.DOWNLOAD_TIMEOUT_SEC} с и был остановлен.",
                              detail=res.stderr_tail(), timed_out=True)

    files: list[Path] = []
    for line in res.stdout.splitlines():
        p = Path(line.strip())
        if p.is_file() and classify_file(p) != "unknown" and p.stat().st_size > 0:
            files.append(p)
    if not files:
        # запасной вариант (например, карусель, где один элемент упал, а остальные скачались):
        # берём только новые медиафайлы каталога; копия cookies не медиа и отфильтруется
        files = [p for p in collect_media_files(tmp_dir) if p.name not in before]
    files = sorted(set(files), key=natural_key)

    if files:
        return DownloadResult(files=files, detail=res.stderr_tail())
    return DownloadResult(error=explain_ytdlp_error(res.stderr), detail=res.stderr_tail())


async def ytdlp_version() -> str:
    res = await run([config.YTDLP_BIN, "--version"], timeout=20)
    return res.stdout.strip() if res.ok else f"недоступен ({res.stderr_tail(80) or res.returncode})"


# ─── instaloader (через helper-скрипт в подпроцессе) ──────────────────────────

HELPER = Path(__file__).with_name("ig_fetch.py")


def shortcode_from_url(url: str) -> str | None:
    m = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


@dataclass
class HelperResult:
    ok: bool
    username: str | None = None
    files: list[Path] = field(default_factory=list)
    error: str = ""
    skipped: bool = False   # пост — обычное видео, instaloader не нужен


async def _run_helper(args: list[str], timeout: float, cwd: Path | None = None) -> HelperResult:
    res = await run([config.HELPER_PYTHON, str(HELPER), *args], timeout=timeout, cwd=cwd)
    if res.timed_out:
        return HelperResult(ok=False, error=f"instaloader не уложился в {int(timeout)} с")
    try:
        data = json.loads(res.stdout.strip().splitlines()[-1]) if res.stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        data = {}
    if not data:
        return HelperResult(ok=False, error=res.stderr_tail(200) or f"exit {res.returncode}")
    return HelperResult(
        ok=bool(data.get("ok")),
        username=data.get("username"),
        files=[Path(f) for f in data.get("files", []) if os.path.isfile(f)],
        error=str(data.get("error") or ""),
        skipped=bool(data.get("skipped")),
    )


async def check_ig_cookies(cookies_file: Path) -> HelperResult:
    """Проверяет сессию: возвращает username, если cookies живые."""
    return await _run_helper(["--cookies", str(cookies_file), "--check"],
                             timeout=config.HELPER_CHECK_TIMEOUT_SEC)


async def download_instaloader(url: str, tmp_dir: Path, cookies_file: Path | None,
                               username: str | None) -> HelperResult:
    shortcode = shortcode_from_url(url)
    if not shortcode:
        return HelperResult(ok=False, error="не удалось извлечь shortcode")
    args = ["--shortcode", shortcode, "--out", str(tmp_dir)]
    if cookies_file:
        args += ["--cookies", str(cookies_file)]
    if username:
        args += ["--username", username]
    return await _run_helper(args, timeout=config.DOWNLOAD_TIMEOUT_SEC, cwd=tmp_dir)


# ─── ffmpeg: нарезка одним проходом ───────────────────────────────────────────

async def ffprobe_duration(path: Path) -> float | None:
    res = await run(
        [config.FFPROBE_BIN, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        timeout=30,
    )
    if not res.ok:
        log.warning("ffprobe: %s", res.stderr_tail(200))
        return None
    try:
        return float(json.loads(res.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


async def split_video(path: Path, out_dir: Path) -> list[Path]:
    """
    Режет видео на части ≤ TG_MAX_MB одним проходом ffmpeg (muxer segment, -c copy).
    Если какая-то часть всё равно больше лимита — уменьшает длину сегмента и повторяет.
    Возвращает [path], если нарезать не удалось.
    """
    total_mb = size_mb(path)
    if total_mb <= config.TG_MAX_MB:
        return [path]

    duration = await ffprobe_duration(path)
    if not duration or duration <= 0:
        log.warning("split: нет длительности для %s", path.name)
        return [path]

    # длительность сегмента пропорционально целевому размеру; резка идёт по ключевым
    # кадрам, поэтому реальные части чуть длиннее — запас уже заложен в PART_TARGET_MB
    seg_time = duration * (config.PART_TARGET_MB / total_mb)
    base = path.stem

    for attempt in range(3):
        for old in out_dir.glob(f"{base}_part*.mp4"):
            old.unlink(missing_ok=True)
        pattern = out_dir / f"{base}_part%02d.mp4"
        cmd = [
            config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path),
            "-map", "0", "-c", "copy",
            "-f", "segment", "-segment_time", f"{seg_time:.2f}",
            "-reset_timestamps", "1", "-avoid_negative_ts", "1",
            "-movflags", "+faststart",
            str(pattern),
        ]
        res = await run(cmd, timeout=config.FFMPEG_TIMEOUT_SEC)
        parts = sorted(out_dir.glob(f"{base}_part*.mp4"), key=natural_key)
        parts = [p for p in parts if p.stat().st_size > 0]
        if not res.ok or not parts:
            log.error("ffmpeg segment (попытка %d): %s", attempt + 1, res.stderr_tail(300))
            return [path]
        biggest = max(size_mb(p) for p in parts)
        log.info("split: %s → %d частей по %.0f с, макс. %.1f МБ", path.name, len(parts), seg_time, biggest)
        if biggest <= config.TG_MAX_MB:
            path.unlink(missing_ok=True)   # оригинал больше не нужен — экономим диск
            return parts
        seg_time *= 0.7

    log.warning("split: не удалось уложиться в лимит за 3 попытки")
    return [path]


async def prepare_for_sending(files: list[Path], tmp_dir: Path) -> list[Path]:
    """Большие видео → части; фото и мелкие файлы — как есть."""
    out: list[Path] = []
    for f in files:
        if classify_file(f) == "video" and size_mb(f) > config.TG_MAX_MB:
            out.extend(await split_video(f, tmp_dir))
        else:
            out.append(f)
    return out


# ─── Диагностика для /stats ───────────────────────────────────────────────────

def disk_free_mb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / 1024 / 1024
    except OSError:
        return -1


def process_rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return -1
