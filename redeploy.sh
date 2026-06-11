#!/bin/bash
set -e

cd "$(dirname "$0")"

# ─── ЦВЕТА ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log() { echo -e "${CYAN}$1${NC}"; }
ok()  { echo -e "${GREEN}✓ $1${NC}"; }
warn(){ echo -e "${YELLOW}⚠ $1${NC}"; }

# ─── 1. ЧИСТИМ ДО СБОРКИ (не после!) ─────────────────────────────────────────
log "🧹 Чистим перед сборкой..."

# Остановленные контейнеры и неиспользуемые образы/сети/кэш
docker system prune -f
docker volume prune -f

# Чистим недоотправленные видео бота (volume /tmp/bot_videos)
if [ -d "/tmp/bot_videos" ]; then
    BOT_SIZE=$(du -sh /tmp/bot_videos 2>/dev/null | awk '{print $1}')
    rm -rf /tmp/bot_videos/*
    ok "bot_videos очищен (было: $BOT_SIZE)"
fi

# Чистим journald если занимает > 200 МБ
JOURNAL_MB=$(du -sm /var/log/journal 2>/dev/null | awk '{print $1}')
if [ -n "$JOURNAL_MB" ] && [ "$JOURNAL_MB" -gt 200 ]; then
    warn "Журналы journald: ${JOURNAL_MB} МБ — чистим до 100 МБ..."
    journalctl --vacuum-size=100M
    ok "Журналы очищены"
fi

# Показываем свободное место до сборки
FREE_BEFORE=$(df -h / | awk 'NR==2 {print $4}')
log "💾 Свободно до сборки: $FREE_BEFORE"

# ─── 2. ОСТАНАВЛИВАЕМ ─────────────────────────────────────────────────────────
log "🛑 Останавливаем контейнеры..."
docker compose down

# ─── 3. ПЕРЕСОБИРАЕМ ──────────────────────────────────────────────────────────
log "🔨 Пересобираем образ..."
# --no-cache убираем — без него Docker переиспользует неизменившиеся слои,
# что экономит и время и место. Образ всё равно обновится если изменились
# requirements.txt, instagram_bot.py или Dockerfile.
docker compose build

# Удаляем промежуточные (dangling) образы оставшиеся после пересборки
docker image prune -f

# ─── 4. ЗАПУСКАЕМ ─────────────────────────────────────────────────────────────
log "🚀 Запускаем..."
docker compose up -d

FREE_AFTER=$(df -h / | awk 'NR==2 {print $4}')
ok "Готово! Свободно после деплоя: $FREE_AFTER"

# ─── 5. ЛОГИ ──────────────────────────────────────────────────────────────────
log "📋 Логи (Ctrl+C чтобы выйти, контейнер продолжит работу):"
docker compose logs -f