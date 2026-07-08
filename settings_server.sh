#!/bin/bash
# Применяет все серверные настройки: очистка диска, swap, ротация логов Docker.
# Идемпотентен — можно запускать повторно. Рабочие контейнеры не трогает.

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "\n${CYAN}=== $1 ===${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
err()  { echo -e "${RED}✗ $1${NC}"; }

if [ "$(id -u)" -ne 0 ]; then err "Запусти от root (sudo)."; exit 1; fi

FREE_BEFORE=$(df -h / | awk 'NR==2{print $4}')

# ─── 1. ОЧИСТКА ДИСКА ─────────────────────────────────────────────────────────
log "Очистка диска"

# Логи всех контейнеров (обрезаем на месте, контейнеры продолжают работать)
truncate -s 0 /var/lib/docker/containers/*/*-json.log 2>/dev/null && ok "логи контейнеров обрезаны"

# Застрявшие временные видео бота
if [ -d /tmp/bot_dl ]; then
    SZ=$(du -sh /tmp/bot_dl 2>/dev/null | awk '{print $1}')
    rm -rf /tmp/bot_dl/* 2>/dev/null
    ok "/tmp/bot_dl очищен (было: $SZ)"
fi
rm -rf /tmp/bot_videos/* 2>/dev/null   # старый путь на всякий случай

# Журналы systemd
journalctl --vacuum-size=50M >/dev/null 2>&1 && ok "журналы systemd ужаты до 50M"

# Docker build cache (рабочие образы не трогает)
docker builder prune -af >/dev/null 2>&1 && ok "build cache очищен"
docker image prune -f    >/dev/null 2>&1 && ok "dangling-образы удалены"

# ─── 2. SWAP ──────────────────────────────────────────────────────────────────
log "Swap"

if swapon --show 2>/dev/null | grep -q '/swapfile'; then
    ok "swap уже активен"
else
    if [ -f /swapfile ]; then
        chmod 600 /swapfile
        # Пробуем активировать существующий файл (мог быть создан fallocate без mkswap)
        if ! swapon /swapfile 2>/dev/null; then
            mkswap /swapfile >/dev/null 2>&1
            if ! swapon /swapfile 2>/dev/null; then
                warn "существующий /swapfile не активируется — пересоздаю через dd"
                rm -f /swapfile
                dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
                chmod 600 /swapfile; mkswap /swapfile >/dev/null; swapon /swapfile
            fi
        fi
    else
        warn "/swapfile не найден — создаю (1 ГБ)"
        dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
        chmod 600 /swapfile; mkswap /swapfile >/dev/null; swapon /swapfile
    fi
    swapon --show | grep -q '/swapfile' && ok "swap активирован" || err "swap поднять не удалось"
fi

# Автоподключение после перезагрузки
if ! grep -q '/swapfile' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "swap добавлен в /etc/fstab"
else
    ok "swap уже прописан в /etc/fstab"
fi

# ─── 3. РОТАЦИЯ ЛОГОВ DOCKER (глобально) ──────────────────────────────────────
log "Ротация логов Docker"

mkdir -p /etc/docker
NEED_RESTART=0
if command -v jq >/dev/null 2>&1 && [ -s /etc/docker/daemon.json ]; then
    # Аккуратно вливаем log-opts в существующий конфиг, сохраняя прочие ключи
    cp /etc/docker/daemon.json "/etc/docker/daemon.json.bak.$(date +%s)"
    TMP=$(mktemp)
    if jq '. + {"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' \
         /etc/docker/daemon.json > "$TMP" 2>/dev/null; then
        if ! cmp -s "$TMP" /etc/docker/daemon.json; then
            mv "$TMP" /etc/docker/daemon.json; NEED_RESTART=1; ok "log-opts добавлены в daemon.json (бэкап сохранён)"
        else
            rm -f "$TMP"; ok "daemon.json уже настроен"
        fi
    else
        rm -f "$TMP"; err "jq не смог обработать daemon.json — пропускаю"
    fi
elif [ -s /etc/docker/daemon.json ]; then
    warn "daemon.json существует, а jq не установлен — не трогаю, чтобы не сломать. Добавь вручную:"
    echo '  "log-driver":"json-file", "log-opts":{"max-size":"10m","max-file":"3"}'
else
    cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
    NEED_RESTART=1; ok "daemon.json создан с ротацией логов"
fi

# ─── 4. ПРИМЕНЕНИЕ (перезапуск Docker — затрагивает ВСЕ контейнеры) ────────────
log "Применение конфигурации Docker"

if [ "$NEED_RESTART" -eq 1 ]; then
    warn "Ротация логов действует только на пересозданные контейнеры."
    warn "Перезапуск Docker кратко остановит ВСЕ контейнеры: инста-бот, gims-bot, naiveproxy."
    if [ -t 0 ]; then
        read -rp "Перезапустить Docker сейчас? [y/N] " ANS
        case "$ANS" in
            [Yy]*) systemctl restart docker && ok "docker перезапущен"
                   warn "Чтобы лимит логов применился к текущим контейнерам, пересоздай их:"
                   echo "     cd ~/insta/instaload && docker compose up -d --force-recreate" ;;
            *)     warn "пропущено. Применишь позже: systemctl restart docker" ;;
        esac
    else
        warn "неинтерактивный запуск — Docker не перезапущен. Выполни вручную: systemctl restart docker"
    fi
else
    ok "конфиг Docker не менялся, перезапуск не нужен"
fi

# ─── ИТОГ ─────────────────────────────────────────────────────────────────────
log "Итог"
FREE_AFTER=$(df -h / | awk 'NR==2{print $4}')
echo -e "Свободно на /: было ${YELLOW}${FREE_BEFORE}${NC} → стало ${GREEN}${FREE_AFTER}${NC}"
free -h | awk 'NR==1||/Swap/{print}'
ok "Готово"