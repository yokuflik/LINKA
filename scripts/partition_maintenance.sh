#!/usr/bin/env bash
# Cron wrapper for time-partition maintenance (ADR 0006). Loads the environment
# and dispatches one keyword to scripts.partition_maintenance.
#
#   partition_maintenance.sh {ensure|report|cold|prune-receipts} [--dry-run]
#
# Adjust APP_DIR and ENV_FILE for your deployment. DATABASE_URL must be set
# (via ENV_FILE or the ambient environment).
set -euo pipefail

APP_DIR="${LINKA_APP_DIR:-/opt/linka}"
ENV_FILE="${LINKA_ENV_FILE:-$APP_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

cd "$APP_DIR"
exec python3 -m scripts.partition_maintenance "$@"
