#!/usr/bin/env bash
# Обновление бинарника yt-dlp со smoke-тестом и откатом.
#
# Логика:
#   1. сохранить копию текущего бинарника;
#   2. yt-dlp --update-to $YTDLP_CHANNEL (stable | nightly | master);
#   3. если версия не изменилась — выйти;
#   4. прогнать smoke-тест (scripts/smoke_test.sh) новой версией;
#   5. если новая версия падает на ссылке, где СТАРАЯ работает — откатить.
#      Если падают обе — считаем, что это сеть/блокировка IP, а не регресс, и оставляем новую.
#
# Запускается systemd-таймером (instaload-ytdlp-update.timer) от пользователя instaload
# и командой /update из бота. Перезапуск бота не нужен: yt-dlp — отдельный процесс на каждую задачу.
#
# Переменные (из /etc/instaload/instaload.env):
#   YTDLP_BIN      путь к бинарнику (по умолчанию /var/lib/instaload/bin/yt-dlp)
#   YTDLP_CHANNEL  stable | nightly | master (по умолчанию stable)
#   SMOKE_URLS     ссылки для проверки через пробел

set -euo pipefail

YTDLP_BIN="${YTDLP_BIN:-/var/lib/instaload/bin/yt-dlp}"
YTDLP_CHANNEL="${YTDLP_CHANNEL:-stable}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKE="$SCRIPT_DIR/smoke_test.sh"
BACKUP="$YTDLP_BIN.bak"

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }

if [[ ! -x "$YTDLP_BIN" ]]; then
    log "yt-dlp не найден: $YTDLP_BIN (запусти scripts/install.sh)"
    exit 1
fi

old_ver="$("$YTDLP_BIN" --version 2>/dev/null || echo unknown)"
log "текущая версия: $old_ver, канал: $YTDLP_CHANNEL"

cp -p "$YTDLP_BIN" "$BACKUP"
cleanup_backup() { rm -f "$BACKUP"; }

if ! "$YTDLP_BIN" --update-to "$YTDLP_CHANNEL" 2>&1 | sed 's/^/  yt-dlp: /'; then
    log "обновление не удалось (сеть/GitHub?), оставляю $old_ver"
    cleanup_backup
    exit 1
fi

new_ver="$("$YTDLP_BIN" --version 2>/dev/null || echo unknown)"
if [[ "$new_ver" == "$old_ver" ]]; then
    log "уже актуальная версия ($old_ver)"
    cleanup_backup
    exit 0
fi
log "обновлено: $old_ver → $new_ver, запускаю smoke-тест"

# smoke_test.sh возвращает 0, если всё ок; 2 — если есть ссылки, которые падают
# на новой версии, но работают на старой (регресс).
set +e
YTDLP_OLD_BIN="$BACKUP" bash "$SMOKE" "$YTDLP_BIN"
rc=$?
set -e

case $rc in
    0)
        log "smoke-тест пройден, версия $new_ver принята"
        cleanup_backup
        ;;
    2)
        log "РЕГРЕСС: новая версия падает там, где старая работает — откат на $old_ver"
        mv -f "$BACKUP" "$YTDLP_BIN"
        chmod 755 "$YTDLP_BIN"
        exit 2
        ;;
    *)
        log "smoke-тест завершился с кодом $rc (нет сети/ссылки недоступны обеим версиям) — оставляю $new_ver"
        cleanup_backup
        ;;
esac
