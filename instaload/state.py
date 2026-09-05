"""
Персистентное состояние бота — один JSON-файл с атомарной записью.

Хранится немного: глобальная подпись, качество YouTube по чатам, счётчики.
Redis/SQLite для этого не нужны (см. аудит, п. 6.1).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("instaload.state")


class State:
    def __init__(self, path: Path):
        self.path = path
        self.caption: str | None = None
        self.quality: dict[int, str] = {}
        self.stats: dict[str, int] = {"done": 0, "failed": 0}
        self.started_at = time.time()
        self._load()

    # ── загрузка / сохранение ────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data: dict[str, Any] = json.loads(self.path.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось прочитать %s: %s — стартую с пустым состоянием", self.path, e)
            return
        self.caption = data.get("caption") or None
        self.quality = {int(k): str(v) for k, v in (data.get("quality") or {}).items()}
        saved_stats = data.get("stats") or {}
        self.stats["done"] = int(saved_stats.get("done", 0))
        self.stats["failed"] = int(saved_stats.get("failed", 0))
        log.info("Состояние загружено: caption=%s, quality для %d чатов",
                 "есть" if self.caption else "нет", len(self.quality))

    def save(self) -> None:
        data = {
            "caption": self.caption,
            "quality": {str(k): v for k, v in self.quality.items()},
            "stats": self.stats,
        }
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    # ── удобные сеттеры ─────────────────────────────────────────────────────

    def set_caption(self, text: str | None) -> None:
        self.caption = text or None
        self.save()

    def set_quality(self, chat_id: int, q: str) -> None:
        self.quality[chat_id] = q
        self.save()

    def bump(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1
        # статистику пишем не чаще, чем раз в 10 задач — незачем дёргать диск
        if (self.stats["done"] + self.stats["failed"]) % 10 == 0:
            self.save()


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Пишет во временный файл рядом и подменяет через os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
