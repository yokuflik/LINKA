#!/usr/bin/env bash
# Cron wrapper for the nightly IVFFlat reindex (ADR 0042). Loads the
# environment and runs scripts.reindex_vectors.
#
#   reindex_vectors.sh
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
exec python3 -m scripts.reindex_vectors
