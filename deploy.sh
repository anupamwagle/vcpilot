#!/bin/bash
# AstraTrade deploy script — run on NAS after pushing code changes from dev machine
# Usage: bash deploy.sh
# Or to rebuild only specific services: bash deploy.sh dashboard
# Or to rebuild all: bash deploy.sh --all

set -e

cd "$(dirname "$0")"

echo "==> Pulling latest code..."
git pull

SERVICES="${1:-dashboard worker-equities worker-crypto beat mcp-server migrate}"

if [ "$1" = "--all" ]; then
  echo "==> Rebuilding all services..."
  docker compose build
else
  echo "==> Rebuilding: $SERVICES"
  docker compose build $SERVICES
fi

echo "==> Restarting containers..."
# --remove-orphans clears out containers for services renamed/removed in docker-compose.yml
# (e.g. the old 'api'/'whatsapp' services) so they don't hold ports the new ones need.
docker compose up -d --remove-orphans

echo "==> Running migrations..."
docker compose start migrate 2>/dev/null || true
sleep 3
docker compose logs --tail=20 migrate

echo "==> Done. Dashboard logs:"
docker compose logs --tail=10 dashboard
