"""Потоковая отправка файлов в Telegram (в памяти бота целый файл не лежит)."""

from __future__ import annotations

import contextlib
from pathlib import Path

from telegram import InputFile, InputMediaPhoto, InputMediaVideo, Message
from telegram.constants import ParseMode

from . import config, media

PHOTO_MAX_MB = 10   # лимит Telegram на sendPhoto; больше — отправляем документом
HTML = ParseMode.HTML


def _stream(fh, path: Path) -> InputFile:
    # read_file_handle=False — PTB ≥ 21.5 отдаёт дескриптор httpx, файл не читается в память
    return InputFile(fh, filename=path.name, read_file_handle=False)


async def _send_one(message: Message, path: Path, caption: str | None) -> None:
    kind = media.classify_file(path)
    with open(path, "rb") as fh:
        if kind == "video":
            await message.reply_video(video=_stream(fh, path), caption=caption, parse_mode=HTML,
                                      supports_streaming=True)
        elif kind == "photo" and media.size_mb(path) <= PHOTO_MAX_MB:
            await message.reply_photo(photo=_stream(fh, path), caption=caption, parse_mode=HTML)
        else:
            await message.reply_document(document=_stream(fh, path), caption=caption, parse_mode=HTML)


async def send_files(message: Message, files: list[Path], caption: str | None, source: str | None,
                     parts: bool) -> None:
    """
    caption — полная подпись (с ссылкой на источник), идёт на первый элемент.
    source  — строка со ссылкой; для частей длинного видео добавляется к каждой части,
              чтобы первоисточник был виден под любой из них.
    """
    if len(files) == 1 or (parts and len(files) > 1):
        total = len(files)
        for idx, path in enumerate(files):
            if idx == 0:
                part_caption = caption
            elif parts:
                part_caption = "\n".join(filter(None, (f"Часть {idx + 1}/{total}", source)))
            else:
                part_caption = None
            await _send_one(message, path, part_caption)
        return

    # альбом: до 10 элементов в группе, подпись только на первом
    for start in range(0, len(files), config.MAX_ALBUM_SIZE):
        chunk = files[start:start + config.MAX_ALBUM_SIZE]
        with contextlib.ExitStack() as stack:
            group = []
            for idx, path in enumerate(chunk):
                fh = stack.enter_context(open(path, "rb"))
                # первая группа — полная подпись; следующие — только ссылка на источник
                item_caption = (caption if start == 0 else source) if idx == 0 else None
                cls = InputMediaVideo if media.classify_file(path) == "video" else InputMediaPhoto
                kwargs = {"supports_streaming": True} if cls is InputMediaVideo else {}
                group.append(cls(media=_stream(fh, path), caption=item_caption, parse_mode=HTML, **kwargs))
            await message.reply_media_group(media=group)
