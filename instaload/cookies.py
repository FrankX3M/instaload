"""
Хранилище cookies Instagram — отдельно для каждого пользователя Telegram.

Принципы:
  • файл cookies пользователя лежит в DATA_DIR/cookies/<user_id>.txt с правами 0600;
  • используется ТОЛЬКО для ссылок, которые прислал этот же пользователь (по from_user.id);
  • перед сохранением из файла выкидывается всё, кроме cookies домена instagram.com —
    боту не нужны cookies других сайтов, и хранить их незачем;
  • бот принимает три формата: Netscape cookies.txt (расширения «Get cookies.txt LOCALLY»,
    yt-dlp), JSON-экспорт (Cookie-Editor / EditThisCookie) и строку заголовка
    «sessionid=…; csrftoken=…; ds_user_id=…»;
  • после каждой загрузки обновлённые yt-dlp cookies записываются обратно —
    Instagram ротирует csrftoken/sessionid, так сессия живёт дольше.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .state import atomic_write_text

log = logging.getLogger("instaload.cookies")

IG_DOMAIN_SUFFIX = "instagram.com"
REQUIRED_COOKIE = "sessionid"
USEFUL_COOKIES = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur", "datr")
NETSCAPE_HEADER = "# Netscape HTTP Cookie File\n# Сохранено ботом instaload. Только cookies instagram.com.\n"

_HEADER_STRING_RE = re.compile(r"(?:^|;\s*)sessionid=([^;\s]+)")


class CookieError(ValueError):
    """Файл не похож на cookies Instagram."""


@dataclass
class Cookie:
    domain: str
    include_subdomains: bool
    path: str
    secure: bool
    expires: int
    name: str
    value: str

    def to_netscape(self) -> str:
        return "\t".join([
            self.domain,
            "TRUE" if self.include_subdomains else "FALSE",
            self.path or "/",
            "TRUE" if self.secure else "FALSE",
            str(int(self.expires)),
            self.name,
            self.value,
        ])


@dataclass
class CookieMeta:
    username: str | None = None
    added_at: float = 0.0
    updated_at: float = 0.0
    count: int = 0
    checked: bool = False   # проверялась ли сессия через instaloader

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "CookieMeta":
        try:
            data = json.loads(text)
            return cls(**{k: data.get(k, getattr(cls(), k)) for k in cls().__dict__})
        except Exception:  # noqa: BLE001
            return cls()


# ─── Парсинг ──────────────────────────────────────────────────────────────────

def _is_ig(domain: str) -> bool:
    d = domain.lower().lstrip(".")
    return d == IG_DOMAIN_SUFFIX or d.endswith("." + IG_DOMAIN_SUFFIX)


def _parse_netscape(text: str) -> list[Cookie]:
    out: list[Cookie] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        http_only_prefix = "#HttpOnly_"
        if line.startswith(http_only_prefix):
            line = line[len(http_only_prefix):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # некоторые экспортёры используют пробелы; пробуем распарсить как поля через пробел
            parts = line.split()
            if len(parts) < 7:
                continue
            parts = parts[:6] + [" ".join(parts[6:])]
        domain, flag, path, secure, expires, name, value = parts[:7]
        try:
            exp = int(float(expires)) if expires else 0
        except ValueError:
            exp = 0
        out.append(Cookie(
            domain=domain,
            include_subdomains=flag.upper() == "TRUE",
            path=path or "/",
            secure=secure.upper() == "TRUE",
            expires=exp,
            name=name,
            value=value,
        ))
    return out


def _parse_json(text: str) -> list[Cookie]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("cookies") or data.get("Cookies") or []
    if not isinstance(data, list):
        raise CookieError("JSON не является списком cookies")
    out: list[Cookie] = []
    for item in data:
        if not isinstance(item, dict) or "name" not in item or "value" not in item:
            continue
        domain = str(item.get("domain") or ".instagram.com")
        exp_raw = item.get("expirationDate", item.get("expires", item.get("expiry", 0))) or 0
        try:
            exp = int(float(exp_raw))
        except (TypeError, ValueError):
            exp = 0
        out.append(Cookie(
            domain=domain,
            include_subdomains=domain.startswith(".") or bool(item.get("hostOnly") is False),
            path=str(item.get("path") or "/"),
            secure=bool(item.get("secure", True)),
            expires=exp,
            name=str(item["name"]),
            value=str(item["value"]),
        ))
    return out


def _parse_header_string(text: str) -> list[Cookie]:
    """«sessionid=…; csrftoken=…» → cookies с доменом .instagram.com."""
    exp = int(time.time()) + 365 * 24 * 3600
    out: list[Cookie] = []
    for chunk in re.split(r";\s*|\n", text.strip()):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if not name or not value:
            continue
        out.append(Cookie(".instagram.com", True, "/", True, exp, name, value))
    return out


def looks_like_header_string(text: str) -> bool:
    return bool(_HEADER_STRING_RE.search(text)) and "\t" not in text


def parse_cookies(raw: bytes | str) -> list[Cookie]:
    """
    Определяет формат, парсит, оставляет только cookies instagram.com.
    Бросает CookieError, если результат непригоден (нет sessionid).
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.lstrip("\ufeff")
    stripped = text.strip()
    if not stripped:
        raise CookieError("пустой файл")

    cookies: list[Cookie]
    if stripped[0] in "[{":
        try:
            cookies = _parse_json(stripped)
        except json.JSONDecodeError as e:
            raise CookieError(f"битый JSON: {e}") from e
    elif "\t" in stripped or stripped.startswith("#"):
        cookies = _parse_netscape(text)
    elif looks_like_header_string(stripped):
        cookies = _parse_header_string(stripped)
    else:
        cookies = _parse_netscape(text)

    ig = [c for c in cookies if _is_ig(c.domain)]
    if not ig:
        raise CookieError("в файле нет cookies для instagram.com")

    # дедупликация по имени: оставляем последнюю (обычно самая свежая)
    by_name: dict[str, Cookie] = {}
    for c in ig:
        by_name[c.name] = c
    ig = list(by_name.values())

    if not any(c.name == REQUIRED_COOKIE and c.value for c in ig):
        raise CookieError("нет cookie «sessionid» — вы не залогинены в Instagram в этом браузере?")

    return ig


def to_netscape_text(cookies: list[Cookie]) -> str:
    return NETSCAPE_HEADER + "\n".join(c.to_netscape() for c in cookies) + "\n"


def summarize(cookies: list[Cookie]) -> str:
    names = {c.name for c in cookies}
    present = [n for n in USEFUL_COOKIES if n in names]
    return f"{len(cookies)} cookies, из ключевых: {', '.join(present) or '—'}"


# ─── Хранилище ────────────────────────────────────────────────────────────────

class CookieStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def _path(self, user_id: int) -> Path:
        return self.dir / f"{int(user_id)}.txt"

    def _meta_path(self, user_id: int) -> Path:
        return self.dir / f"{int(user_id)}.json"

    def has(self, user_id: int | None) -> bool:
        return user_id is not None and self._path(user_id).is_file()

    def path_for(self, user_id: int | None) -> Path | None:
        if user_id is None:
            return None
        p = self._path(user_id)
        return p if p.is_file() else None

    def count(self) -> int:
        return sum(1 for _ in self.dir.glob("*.txt"))

    def meta(self, user_id: int) -> CookieMeta:
        p = self._meta_path(user_id)
        if p.is_file():
            return CookieMeta.from_json(p.read_text("utf-8"))
        return CookieMeta()

    def save_meta(self, user_id: int, meta: CookieMeta) -> None:
        atomic_write_text(self._meta_path(user_id), meta.to_json())

    def save(self, user_id: int, raw: bytes | str) -> tuple[list[Cookie], CookieMeta]:
        """Парсит, фильтрует и сохраняет cookies пользователя. Бросает CookieError."""
        cookies = parse_cookies(raw)
        atomic_write_text(self._path(user_id), to_netscape_text(cookies), mode=0o600)
        meta = self.meta(user_id)
        now = time.time()
        meta.added_at = meta.added_at or now
        meta.updated_at = now
        meta.count = len(cookies)
        meta.username = None      # аккаунт мог измениться — перепроверим
        meta.checked = False
        self.save_meta(user_id, meta)
        log.info("cookies сохранены для user=%s (%s)", user_id, summarize(cookies))
        return cookies, meta

    def delete(self, user_id: int) -> bool:
        removed = False
        for p in (self._path(user_id), self._meta_path(user_id)):
            try:
                p.unlink()
                removed = True
            except FileNotFoundError:
                pass
        if removed:
            log.info("cookies удалены для user=%s", user_id)
        return removed

    def write_back(self, user_id: int | None, updated_file: Path) -> bool:
        """
        После загрузки yt-dlp мог обновить cookies (ротация csrftoken/sessionid).
        Сохраняем обновлённую копию, если она по-прежнему валидна.
        """
        if user_id is None or not updated_file.is_file():
            return False
        target = self._path(user_id)
        if not target.is_file():
            return False
        try:
            new_raw = updated_file.read_bytes()
            if new_raw == target.read_bytes():
                return False
            cookies = parse_cookies(new_raw)
            atomic_write_text(target, to_netscape_text(cookies), mode=0o600)
            meta = self.meta(user_id)
            meta.updated_at = time.time()
            meta.count = len(cookies)
            self.save_meta(user_id, meta)
            log.debug("cookies user=%s обновлены после загрузки", user_id)
            return True
        except CookieError as e:
            log.warning("cookies user=%s после загрузки невалидны (%s) — оставляю старые", user_id, e)
        except OSError as e:
            log.warning("cookies user=%s: не удалось записать обновление: %s", user_id, e)
        return False


def write_back_shared(shared_path: str, updated_file: Path) -> bool:
    """То же для общего файла админа, если он задан и доступен на запись."""
    if not shared_path or not updated_file.is_file():
        return False
    target = Path(shared_path)
    if not target.is_file() or not os.access(target, os.W_OK):
        return False
    try:
        new_raw = updated_file.read_bytes()
        if new_raw == target.read_bytes():
            return False
        cookies = parse_cookies(new_raw)
        atomic_write_text(target, to_netscape_text(cookies), mode=0o600)
        return True
    except (CookieError, OSError) as e:
        log.warning("общие cookies: не обновлены (%s)", e)
        return False
