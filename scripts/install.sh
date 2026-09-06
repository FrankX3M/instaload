#!/usr/bin/env bash
# Установка / обновление instaload на Debian 12+ / Ubuntu 22.04+ без Docker.
#
#   sudo bash scripts/install.sh              # первая установка или обновление кода
#   sudo bash scripts/install.sh --force-binaries   # заново скачать yt-dlp и ffmpeg
#   sudo bash scripts/install.sh --system-ffmpeg    # использовать ffmpeg из apt вместо статического
#
# Раскладка:
#   /opt/instaload/app      код (копия этого репозитория)         root, read-only для бота
#   /opt/instaload/venv     python-telegram-bot + instaloader     root, read-only для бота
#   /opt/instaload/bin      статические ffmpeg + ffprobe          root, read-only для бота
#   /var/lib/instaload      state.json, cookies/, cache/, tmp/, bin/yt-dlp   instaload, 0700
#   /etc/instaload/instaload.env   настройки                       root:instaload, 0640
#
# Скрипт идемпотентен: повторный запуск обновляет код и зависимости, не трогая настройки и данные.

set -euo pipefail

APP_USER=instaload
OPT=/opt/instaload
APP_DIR=$OPT/app
VENV=$OPT/venv
BIN_DIR=$OPT/bin
DATA_DIR=/var/lib/instaload
ENV_DIR=/etc/instaload
ENV_FILE=$ENV_DIR/instaload.env
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FORCE_BINARIES=0
SYSTEM_FFMPEG=0
for arg in "$@"; do
    case "$arg" in
        --force-binaries) FORCE_BINARIES=1 ;;
        --system-ffmpeg) SYSTEM_FFMPEG=1 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "неизвестный аргумент: $arg"; exit 1 ;;
    esac
done

say() { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запусти от root: sudo bash scripts/install.sh"
[[ -f "$SRC_DIR/requirements.txt" && -d "$SRC_DIR/instaload" ]] || die "запускай из клона репозитория"

# ── архитектура ───────────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)  YTDLP_ASSET=yt-dlp_linux;          FFMPEG_ARCH=amd64 ;;
    aarch64) YTDLP_ASSET=yt-dlp_linux_aarch64;  FFMPEG_ARCH=arm64 ;;
    armv7l)  YTDLP_ASSET=yt-dlp_linux_armv7l;   FFMPEG_ARCH=armhf ;;
    *) die "неподдерживаемая архитектура: $ARCH" ;;
esac
YTDLP_URL="${YTDLP_URL:-https://github.com/yt-dlp/yt-dlp/releases/latest/download/$YTDLP_ASSET}"
FFMPEG_URL="${FFMPEG_URL:-https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FFMPEG_ARCH}-static.tar.xz}"

# ── системные пакеты ──────────────────────────────────────────────────────────
say "Системные пакеты"
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    pkgs=(python3 python3-venv curl xz-utils ca-certificates)
    (( SYSTEM_FFMPEG )) && pkgs+=(ffmpeg)
    apt-get install -y -qq --no-install-recommends "${pkgs[@]}"
else
    echo "  не Debian/Ubuntu — убедись, что есть python3 (≥3.10), python3-venv, curl, xz"
fi
PY=$(command -v python3)
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "нужен Python ≥ 3.10 (сейчас $($PY --version))"

# ── пользователь и каталоги ───────────────────────────────────────────────────
say "Пользователь $APP_USER и каталоги"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin --user-group "$APP_USER"
fi
install -d -m 0755 "$OPT" "$BIN_DIR" "$ENV_DIR"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR" "$DATA_DIR/bin" "$DATA_DIR/cookies" \
        "$DATA_DIR/cache" "$DATA_DIR/tmp"

# предупреждение про tmpfs (аудит 2.2): временные файлы не должны жить в RAM
if df -P "$DATA_DIR" | awk 'NR==2{print $1}' | grep -q tmpfs; then
    echo "  ⚠ $DATA_DIR лежит на tmpfs (в RAM). Задай INSTALOAD_TMP_DIR на реальный диск."
fi

# ── код ───────────────────────────────────────────────────────────────────────
say "Код → $APP_DIR"
rm -rf "$APP_DIR.new"
mkdir -p "$APP_DIR.new"
cp -a "$SRC_DIR/instaload" "$SRC_DIR/scripts" "$SRC_DIR/systemd" "$SRC_DIR/requirements.txt" "$APP_DIR.new/"
[[ -d "$SRC_DIR/docs" ]] && cp -a "$SRC_DIR/docs" "$APP_DIR.new/"
[[ -f "$SRC_DIR/README.md" ]] && cp -a "$SRC_DIR/README.md" "$APP_DIR.new/"
find "$APP_DIR.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod +x "$APP_DIR.new"/scripts/*.sh
rm -rf "$APP_DIR"
mv "$APP_DIR.new" "$APP_DIR"
chown -R root:root "$APP_DIR"

# ── venv ──────────────────────────────────────────────────────────────────────
say "Python venv → $VENV"
[[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade -r "$APP_DIR/requirements.txt"
echo "  $("$VENV/bin/pip" list 2>/dev/null | grep -Ei 'python-telegram-bot|instaloader' | tr -s ' ' | paste -sd ', ')"

# ── yt-dlp (отдельный бинарник, обновляется сам) ──────────────────────────────
YTDLP_BIN=$DATA_DIR/bin/yt-dlp
if [[ ! -x "$YTDLP_BIN" || $FORCE_BINARIES -eq 1 ]]; then
    say "yt-dlp → $YTDLP_BIN"
    curl -fsSL --retry 3 -o "$YTDLP_BIN.tmp" "$YTDLP_URL"
    chmod 755 "$YTDLP_BIN.tmp"
    mv -f "$YTDLP_BIN.tmp" "$YTDLP_BIN"
fi
chown "$APP_USER:$APP_USER" "$YTDLP_BIN"
echo "  yt-dlp $("$YTDLP_BIN" --version)"

# ── ffmpeg ────────────────────────────────────────────────────────────────────
if (( SYSTEM_FFMPEG )); then
    say "ffmpeg из системы"
    ln -sf "$(command -v ffmpeg)" "$BIN_DIR/ffmpeg"
    ln -sf "$(command -v ffprobe)" "$BIN_DIR/ffprobe"
elif [[ ! -x "$BIN_DIR/ffmpeg" || ! -x "$BIN_DIR/ffprobe" || $FORCE_BINARIES -eq 1 ]]; then
    say "Статический ffmpeg → $BIN_DIR (≈80 МБ вместо ≈500 МБ из apt)"
    tmpd=$(mktemp -d)
    curl -fsSL --retry 3 -o "$tmpd/ffmpeg.tar.xz" "$FFMPEG_URL"
    tar -xJf "$tmpd/ffmpeg.tar.xz" -C "$tmpd"
    ff=$(find "$tmpd" -type f -name ffmpeg | head -1)
    fp=$(find "$tmpd" -type f -name ffprobe | head -1)
    [[ -n "$ff" && -n "$fp" ]] || die "в архиве ffmpeg нет бинарников"
    install -m 0755 "$ff" "$BIN_DIR/ffmpeg"
    install -m 0755 "$fp" "$BIN_DIR/ffprobe"
    rm -rf "$tmpd"
fi
echo "  $("$BIN_DIR/ffmpeg" -version | head -1)"

# ── настройки ─────────────────────────────────────────────────────────────────
say "Настройки → $ENV_FILE"
if [[ ! -f "$ENV_FILE" ]]; then
    install -m 0640 -o root -g "$APP_USER" "$SRC_DIR/instaload.env.example" "$ENV_FILE"
    echo "  создан из примера — впиши BOT_TOKEN и ADMIN_ID"
else
    chown root:"$APP_USER" "$ENV_FILE"; chmod 0640 "$ENV_FILE"
    echo "  уже существует, не трогаю"
fi

# ── systemd ───────────────────────────────────────────────────────────────────
say "systemd"
install -m 0644 "$APP_DIR"/systemd/instaload.service /etc/systemd/system/
install -m 0644 "$APP_DIR"/systemd/instaload-ytdlp-update.service /etc/systemd/system/
install -m 0644 "$APP_DIR"/systemd/instaload-ytdlp-update.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now instaload-ytdlp-update.timer >/dev/null
systemctl enable instaload.service >/dev/null

if grep -Eq '^BOT_TOKEN=(ВАШ_ТОКЕН_ОТ_BOTFATHER)?$' "$ENV_FILE"; then
    cat <<EOF

✅ Установлено. Осталось:
   1. sudo nano $ENV_FILE       # BOT_TOKEN, ADMIN_ID
   2. sudo systemctl start instaload
   3. journalctl -u instaload -f
EOF
else
    systemctl restart instaload.service
    sleep 2
    systemctl --no-pager --lines=5 status instaload.service || true
    echo
    echo "✅ Готово. Логи: journalctl -u instaload -f"
fi
echo "   Обновление yt-dlp: ежедневно по таймеру (systemctl list-timers instaload*), вручную — /update в боте"
