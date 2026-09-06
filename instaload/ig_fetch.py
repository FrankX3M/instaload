#!/usr/bin/env python3
"""
Helper для Instagram-фото и каруселей через instaloader.

Запускается ботом как ОТДЕЛЬНЫЙ ПРОЦЕСС (см. media.py): instaloader и requests
не живут в процессе бота, память освобождается по завершении, зависание
убивается по таймауту.

Использование:
    ig_fetch.py --cookies FILE --check
    ig_fetch.py --shortcode CODE --out DIR [--cookies FILE] [--username NAME]

На stdout — ровно одна JSON-строка:
    {"ok": true, "username": "...", "files": ["..."], "skipped": false, "error": ""}
"""

from __future__ import annotations

import argparse
import glob
import http.cookiejar
import json
import os
import re
import sys


def emit(**data) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies")
    ap.add_argument("--username")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--shortcode")
    ap.add_argument("--out")
    args = ap.parse_args()

    try:
        import instaloader  # импорт здесь, в подпроцессе
    except ImportError:
        emit(ok=False, error="instaloader не установлен в venv")
        return 2

    L = instaloader.Instaloader(
        dirname_pattern=args.out or ".",
        filename_pattern="{shortcode}_{mediaid}",
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        compress_json=False,
        quiet=True,
        request_timeout=60.0,
        max_connection_attempts=2,
    )

    username = args.username
    if args.cookies and os.path.isfile(args.cookies):
        try:
            cj = http.cookiejar.MozillaCookieJar(args.cookies)
            cj.load(ignore_discard=True, ignore_expires=True)
            L.context._session.cookies.update(cj)  # noqa: SLF001 — публичного API для этого нет
        except Exception as e:  # noqa: BLE001
            emit(ok=False, error=f"не удалось загрузить cookies: {e}")
            return 3

        if not username or args.check:
            try:
                username = L.test_login()
            except Exception as e:  # noqa: BLE001
                emit(ok=False, error=f"проверка сессии не удалась: {e}")
                return 4
        if username:
            L.context.username = username

    if args.check:
        if username:
            emit(ok=True, username=username)
            return 0
        emit(ok=False, error="сессия не подтвердилась: cookies протухли, экспортированы без входа в аккаунт, или Instagram не ответил серверу")
        return 5

    if not args.shortcode or not args.out:
        emit(ok=False, error="нужны --shortcode и --out")
        return 1

    try:
        post = instaloader.Post.from_shortcode(L.context, args.shortcode)
        if post.is_video and post.typename != "GraphSidecar":
            # обычное видео — это работа yt-dlp, не дублируем
            emit(ok=False, skipped=True, username=username, error="пост — видео")
            return 0
        L.download_post(post, target=args.out)
    except instaloader.exceptions.LoginRequiredException as e:
        emit(ok=False, username=username, error=f"нужна авторизация: {e}")
        return 6
    except instaloader.exceptions.InstaloaderException as e:
        emit(ok=False, username=username, error=f"instaloader: {e}")
        return 7
    except Exception as e:  # noqa: BLE001
        emit(ok=False, username=username, error=f"ошибка: {e}")
        return 8

    files = []
    for ext in ("jpg", "jpeg", "png", "webp", "mp4"):
        files.extend(glob.glob(os.path.join(args.out, f"*.{ext}")))
    files = sorted(set(files), key=natural_key)
    emit(ok=bool(files), username=username, files=files,
         error="" if files else "instaloader ничего не скачал")
    return 0 if files else 9


if __name__ == "__main__":
    sys.exit(main())
