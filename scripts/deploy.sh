#!/usr/bin/env bash
# Обновление бота после изменений в git: pull → install.sh (код + зависимости) → restart.
# Заменяет старый redeploy.sh с пересборкой Docker-образа. Занимает секунды.
#
#   sudo bash scripts/deploy.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -d "$REPO/.git" ]]; then
    git -C "$REPO" pull --ff-only
fi
bash "$REPO/scripts/install.sh"
systemctl restart instaload.service
sleep 2
systemctl --no-pager --lines=10 status instaload.service || true
