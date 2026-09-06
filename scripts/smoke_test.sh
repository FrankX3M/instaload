#!/usr/bin/env bash
# Smoke-тест yt-dlp: проверяет, что экстракторы живы, БЕЗ скачивания (режим --simulate).
#
#   scripts/smoke_test.sh [/path/to/yt-dlp]
#
# Переменные:
#   SMOKE_URLS     ссылки через пробел (по умолчанию одно стабильное видео YouTube)
#   YTDLP_OLD_BIN  если задан — при падении ссылки на новой версии проверяем её старой:
#                  работает на старой и падает на новой = регресс → exit 2
#   IG_COOKIES_SHARED  cookies для instagram-ссылок в списке (иначе они, скорее всего, упадут)
#
# Коды выхода: 0 — всё ок; 1 — есть падения, но не регресс (сеть, блокировка IP); 2 — регресс.

set -uo pipefail

YTDLP="${1:-${YTDLP_BIN:-/var/lib/instaload/bin/yt-dlp}}"
SMOKE_URLS="${SMOKE_URLS:-https://www.youtube.com/watch?v=jNQXAC9IVRw}"
OLD="${YTDLP_OLD_BIN:-}"
CACHE_DIR="${INSTALOAD_DATA_DIR:-/var/lib/instaload}/cache"

probe() {  # probe <bin> <url>
    local bin="$1" url="$2" extra=()
    if [[ "$url" == *instagram.com* && -n "${IG_COOKIES_SHARED:-}" && -r "$IG_COOKIES_SHARED" ]]; then
        # копия: yt-dlp пишет в cookiefile, а общий файл трогать из теста не хотим
        local tmpc; tmpc="$(mktemp)"; cp "$IG_COOKIES_SHARED" "$tmpc"
        extra=(--cookies "$tmpc")
    fi
    timeout 90 "$bin" --simulate --quiet --no-warnings --no-playlist \
        --socket-timeout 20 --retries 1 --extractor-retries 1 \
        --cache-dir "$CACHE_DIR" ${extra[@]+"${extra[@]}"} -- "$url" >/dev/null 2>&1
    local rc=$?
    [[ -n "${tmpc:-}" ]] && rm -f "$tmpc"
    return $rc
}

fail=0; regress=0; total=0
for url in $SMOKE_URLS; do
    total=$((total + 1))
    if probe "$YTDLP" "$url"; then
        echo "  OK   $url"
        continue
    fi
    if [[ -n "$OLD" && -x "$OLD" ]] && probe "$OLD" "$url"; then
        echo "  REGRESS  $url  (старая версия работает, новая — нет)"
        regress=$((regress + 1))
    else
        echo "  FAIL $url  (недоступно и старой версии — вероятно сеть/блокировка IP)"
        fail=$((fail + 1))
    fi
done

echo "smoke: $total ссылок, регрессов $regress, прочих падений $fail"
if (( regress > 0 )); then exit 2; fi
if (( fail > 0 )); then exit 1; fi
exit 0
