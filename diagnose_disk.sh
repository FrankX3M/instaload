#!/bin/bash
# =============================================================================
# diagnose_disk.sh — диагностика диска на сервере с Docker и systemctl
# Запуск: bash diagnose_disk.sh | tee /tmp/disk_report.txt
# =============================================================================

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

sep()  { echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }
h1()   { sep; echo -e "${BOLD}${CYAN}  $1${NC}"; sep; }
warn() { echo -e "  ${YELLOW}⚠  $1${NC}"; }
ok()   { echo -e "  ${GREEN}✓  $1${NC}"; }
info() { echo -e "     $1"; }

echo ""
echo -e "${BOLD}  🔍 ДИАГНОСТИКА ДИСКА — $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo ""

# ─── 1. ОБЩИЙ РАЗМЕР ДИСКА ────────────────────────────────────────────────────
h1 "1. РАЗДЕЛЫ И СВОБОДНОЕ МЕСТО"
df -h | grep -v tmpfs | grep -v udev

DISK_FREE_PCT=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print 100-$5}')
DISK_USED_PCT=$(df / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
echo ""
if [ "$DISK_USED_PCT" -ge 90 ]; then
    warn "Использовано ${DISK_USED_PCT}% диска — КРИТИЧНО!"
elif [ "$DISK_USED_PCT" -ge 75 ]; then
    warn "Использовано ${DISK_USED_PCT}% диска — стоит почистить"
else
    ok "Использовано ${DISK_USED_PCT}% диска"
fi

# ─── 2. TOP-10 САМЫХ ТЯЖЁЛЫХ ПАПОК В / ──────────────────────────────────────
h1 "2. ТОП-10 ТЯЖЁЛЫХ ДИРЕКТОРИЙ (первый уровень)"
du -sh /* 2>/dev/null | sort -rh | head -10

# ─── 3. DOCKER ────────────────────────────────────────────────────────────────
h1 "3. DOCKER — СКОЛЬКО ЗАНИМАЕТ"

if ! command -v docker &>/dev/null; then
    warn "Docker не установлен или недоступен"
else
    docker system df 2>/dev/null || warn "Не удалось получить статистику Docker (нет прав?)"

    echo ""
    echo -e "  ${BOLD}Образы (images):${NC}"
    docker images --format "  {{.Repository}}:{{.Tag}}  →  {{.Size}}  [создан {{.CreatedSince}}]" 2>/dev/null \
        | sort || warn "Не удалось получить список образов"

    echo ""
    echo -e "  ${BOLD}Запущенные контейнеры:${NC}"
    docker ps --format "  {{.Names}}  {{.Status}}  (образ: {{.Image}})" 2>/dev/null \
        || warn "Не удалось получить список контейнеров"

    echo ""
    echo -e "  ${BOLD}Остановленные / упавшие контейнеры:${NC}"
    STOPPED=$(docker ps -a --filter "status=exited" --format "  {{.Names}}  [{{.Status}}]  образ: {{.Image}}" 2>/dev/null)
    if [ -n "$STOPPED" ]; then
        echo "$STOPPED"
        warn "Есть остановленные контейнеры — можно удалить: docker container prune -f"
    else
        ok "Остановленных контейнеров нет"
    fi

    echo ""
    echo -e "  ${BOLD}Вольюмы Docker:${NC}"
    docker volume ls 2>/dev/null
    VOLUMES_COUNT=$(docker volume ls -q 2>/dev/null | wc -l)
    if [ "$VOLUMES_COUNT" -gt 0 ]; then
        echo ""
        echo "  Размер данных в /var/lib/docker/volumes:"
        du -sh /var/lib/docker/volumes/* 2>/dev/null | sort -rh | head -10 \
            || warn "Нет доступа (запусти от root)"
    fi

    echo ""
    echo -e "  ${BOLD}Кэш сборки (build cache):${NC}"
    BUILD_CACHE=$(docker system df 2>/dev/null | grep "Build Cache" | awk '{print $4}')
    if [ -n "$BUILD_CACHE" ]; then
        info "Build cache занимает: $BUILD_CACHE"
        info "Очистить: docker builder prune -f"
    fi

    echo ""
    echo -e "  ${BOLD}/var/lib/docker суммарно:${NC}"
    du -sh /var/lib/docker 2>/dev/null || warn "Нет доступа к /var/lib/docker (нужен root)"

    # Конкретно твой бот
    echo ""
    echo -e "  ${BOLD}Твой инста-бот (смонтированный volume /tmp/bot_videos):${NC}"
    du -sh /tmp/bot_videos 2>/dev/null && echo "" || info "/tmp/bot_videos не существует или пуст"
fi

# ─── 4. SYSTEMCTL — СЕРВИСЫ И ИХ ЛОГИ ────────────────────────────────────────
h1 "4. SYSTEMD — ЗАПУЩЕННЫЕ ПОЛЬЗОВАТЕЛЬСКИЕ СЕРВИСЫ"

echo -e "  ${BOLD}Все активные сервисы (не системные):${NC}"
systemctl list-units --type=service --state=running 2>/dev/null \
    | grep -v "system.slice\|systemd\|dbus\|ssh\|cron\|rsyslog\|ufw\|networkd\|resolved\|timesyncd\|polkit\|accounts\|upower\|ModemManager\|bluetooth\|avahi\|cups" \
    | head -30

echo ""
echo -e "  ${BOLD}Упавшие/неактивные сервисы:${NC}"
FAILED=$(systemctl list-units --type=service --state=failed 2>/dev/null | grep "failed")
if [ -n "$FAILED" ]; then
    echo "$FAILED"
    warn "Есть упавшие сервисы! Проверь логи: journalctl -u <имя_сервиса> -n 50"
else
    ok "Упавших сервисов нет"
fi

# ─── 5. JOURNALD (ЛОГИ SYSTEMD) ───────────────────────────────────────────────
h1 "5. ЛОГИ JOURNALD — СКОЛЬКО ЗАНИМАЮТ"

journalctl --disk-usage 2>/dev/null || warn "journalctl недоступен"
echo ""

JOURNAL_SIZE_MB=$(du -sm /var/log/journal 2>/dev/null | awk '{print $1}')
if [ -n "$JOURNAL_SIZE_MB" ] && [ "$JOURNAL_SIZE_MB" -gt 500 ]; then
    warn "Журналы journald занимают ${JOURNAL_SIZE_MB} МБ"
    info "Очистить до 100 МБ: sudo journalctl --vacuum-size=100M"
    info "Очистить старше 7 дней: sudo journalctl --vacuum-time=7d"
else
    ok "Журналы journald: ${JOURNAL_SIZE_MB:-?} МБ — в норме"
fi

# ─── 6. ЛОГИ В /var/log ───────────────────────────────────────────────────────
h1 "6. /var/log — РАЗМЕР ЛОГОВ"

echo "  Топ-10 файлов в /var/log:"
find /var/log -type f -exec du -sh {} \; 2>/dev/null | sort -rh | head -10

echo ""
VARLOG_SIZE=$(du -sh /var/log 2>/dev/null | awk '{print $1}')
info "Всего /var/log: $VARLOG_SIZE"

# ─── 7. /tmp И ВРЕМЕННЫЕ ФАЙЛЫ ────────────────────────────────────────────────
h1 "7. /tmp — ВРЕМЕННЫЕ ФАЙЛЫ"

du -sh /tmp 2>/dev/null
echo ""
echo "  Топ файлы/папки в /tmp:"
du -sh /tmp/* 2>/dev/null | sort -rh | head -10 || info "  /tmp пуст или нет доступа"

# ─── 8. ДОМАШНИЕ ДИРЕКТОРИИ ───────────────────────────────────────────────────
h1 "8. ДОМАШНИЕ ДИРЕКТОРИИ"
du -sh /home/* /root 2>/dev/null | sort -rh

# Ищем большие файлы в домашних директориях
echo ""
echo "  Файлы > 100 МБ в /home и /root:"
find /home /root -type f -size +100M 2>/dev/null \
    | xargs du -sh 2>/dev/null | sort -rh \
    || info "Таких файлов нет или нет доступа"

# ─── 9. INODES (важно!) ───────────────────────────────────────────────────────
h1 "9. INODE — ИСПОЛЬЗОВАНИЕ"
df -i | grep -v tmpfs | grep -v udev
echo ""
INODE_PCT=$(df -i / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
if [ "$INODE_PCT" -ge 80 ]; then
    warn "Использовано ${INODE_PCT}% inode! Диск может казаться свободным, но файлы не создаются."
    info "Найти папки с огромным числом файлов:"
    info "  for d in /*; do echo \$(find \$d -maxdepth 3 | wc -l) \$d; done 2>/dev/null | sort -rn | head -10"
else
    ok "Inode: использовано ${INODE_PCT}%"
fi

# ─── 10. РЕКОМЕНДАЦИИ ─────────────────────────────────────────────────────────
h1 "10. БЫСТРАЯ ОЧИСТКА — КОМАНДЫ"

echo -e "  ${BOLD}Docker (самый частый виновник):${NC}"
echo "    docker system prune -a --volumes -f    # удалить ВСЁ неиспользуемое"
echo "    docker image prune -a -f               # только образы"
echo "    docker builder prune -f                # только build cache"
echo ""
echo -e "  ${BOLD}Журналы systemd:${NC}"
echo "    sudo journalctl --vacuum-size=100M"
echo "    sudo journalctl --vacuum-time=7d"
echo ""
echo -e "  ${BOLD}Логи apt:${NC}"
echo "    sudo apt-get clean                     # кэш пакетов"
echo "    sudo apt-get autoremove -y             # ненужные зависимости"
echo ""
echo -e "  ${BOLD}Временные файлы бота (твой volume):${NC}"
echo "    rm -rf /tmp/bot_videos/*               # недоотправленные видео"
echo ""

sep
echo ""
echo -e "  ${GREEN}Отчёт сохранён в /tmp/disk_report.txt${NC} (если запущен с | tee)"
echo ""
