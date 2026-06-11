#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "🛑 Останавливаем контейнеры..."
docker compose down

echo "🔨 Пересобираем образ..."
docker compose build --no-cache

echo "🧹 Чистим мусор..."
docker system prune -f

echo "🚀 Запускаем..."
docker compose up -d

echo "📋 Логи:"
docker compose logs -f