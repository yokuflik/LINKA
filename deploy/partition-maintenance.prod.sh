#!/usr/bin/env bash
# Prod wrapper: runs partition maintenance (ADR 0005/0006) *inside* the
# running app container of the compose stack. Install in the host crontab
# via deploy/partition-maintenance.prod.crontab.
#
#   partition-maintenance.prod.sh {ensure|report|cold|prune-receipts} [--dry-run]
set -euo pipefail

APP_DIR="${LINKA_APP_DIR:-/opt/linka}"
COMPOSE_FILE="${LINKA_COMPOSE_FILE:-docker-compose.prod.yml}"

cd "$APP_DIR"
exec docker compose -f "$COMPOSE_FILE" exec -T app \
    python -m scripts.partition_maintenance "$@"
