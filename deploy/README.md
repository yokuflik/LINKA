# Linka — single-host deployment (free-tier EC2, 1 GB RAM)

See ADR 0007 for the rationale. This is a demo topology, not the billion-scale
production design.

## Files

| File | Purpose |
|---|---|
| `../Dockerfile` | multi-stage image for the FastAPI app |
| `../.dockerignore` | keeps the build context small |
| `../docker-compose.prod.yml` | full stack: `app` + `caddy` + `db` + `redis` + `minio` + `id_service` |
| `../id_service/` | Rust Snowflake ID gRPC service (ADR 0011); `id_service/Dockerfile` builds it |
| `env.production.example` | copy to `../.env`, fill every `<CHANGE ME>` |
| `Caddyfile` | reverse proxy: API + WS + static PoC + MinIO |
| `postgres.prod.conf` | Postgres tuned for a 1 GB box |
| `partition-maintenance.prod.sh` / `.crontab` | host cron → `docker compose exec` |

## One-time host setup

1. **Swap (mandatory — the box has none):**
   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
   sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
2. **Docker + compose plugin:**
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER   # re-login
   ```
3. **DNS:** point `linka.example.com` **and** `s3.linka.example.com` (A
   records) at the instance's public IP. Open inbound 80 + 443 in the
   security group. (No domain? set `SITE_ADDRESS=:80` / `S3_ADDRESS=:80` —
   HTTP only, WebSocket-over-TLS features degraded.)
4. **Clone to `/opt/linka`:**
   ```bash
   sudo git clone <repo> /opt/linka && sudo chown -R $USER /opt/linka
   cd /opt/linka
   mkdir -p backups
   ```

## Configure

```bash
cp deploy/env.production.example .env
# edit .env: SITE_ADDRESS, S3_ADDRESS, POSTGRES_PASSWORD,
# JWT_SECRET_KEY (openssl rand -hex 32), S3_ACCESS_KEY / S3_SECRET_KEY,
# CORS_ALLOW_ORIGINS, S3_ENDPOINT_URL / S3_AVATARS_PUBLIC_BASE_URL
#
# Security (ADR 0012) - set the host-based ones to SITE_ADDRESS's hostname:
#   CORS_ALLOW_ORIGINS   also gates the WebSocket handshake Origin; never "*"
#   ALLOWED_HOSTS        Host-header allowlist (comma-separated, "*" disables)
#   TRUSTED_PROXY_IPS    default (docker bridge + loopback) is fine here
#   API_IP_BACKSTOP_MAX / API_IP_BACKSTOP_WINDOW_SECONDS   coarse per-IP REST
#                         ceiling, default 1000 / 180 s - the retune knob
```

### Avatars bucket CORS (ADR 0016 device cache)

The app calls `put_bucket_cors` on the avatars bucket at startup, but a real
AWS S3 bucket provisioned by IaC may deny that call. Ensure the avatars bucket
carries this CORS rule so browsers can `fetch()` full-res avatars (for the
client-side Cache Storage copy) - not just render them via `<img>`:

```json
{ "CORSRules": [ {
  "AllowedOrigins": ["https://linka-web.com", "https://www.linka-web.com"],
  "AllowedMethods": ["GET", "HEAD"],
  "AllowedHeaders": ["*"],
  "ExposeHeaders": ["ETag", "Content-Length"],
  "MaxAgeSeconds": 3600
} ] }
```

### Rate-limit retune knobs (ADR 0012 / COMMS_SECURITY_PLAN)

Every limit is an env var with a generous default — ship as-is, then tighten
from real metrics (429 rate in the app log, Redis `rlsw:*` / `ratelimit:*` key
counts). Full table + bucket keys: `.claude_docs/security_and_rate_limiting.md`.
All are commented out in `env.production.example`; uncomment to override.

| Surface | Knob(s) | Default |
|---|---|---|
| Global per-IP REST backstop | `API_IP_BACKSTOP_MAX` / `_WINDOW_SECONDS` | 1000 / 180 s |
| OTP request (per phone) | `OTP_REQUEST_RATE_LIMIT_MAX` / `_WINDOW_SECONDS` | 5 / 1800 s |
| OTP request / verify (per IP) | `OTP_REQUEST_IP_RATE_LIMIT_*`, `OTP_VERIFY_IP_RATE_LIMIT_*` | 15 / h, 30 / h |
| `/auth/refresh` | `REFRESH_IP_RATE_LIMIT_*`, `REFRESH_JTI_RATE_LIMIT_*` | 60 / h IP, 10 / h token |
| New accounts (per IP) | `ACCOUNT_CREATE_IP_RATE_LIMIT_*` | 5 / day |
| WS concurrent conns / user | `WS_CONN_MAX_CONNECTIONS`, `WS_CONN_MAX_AGE_SECONDS` | 5, 26 h |
| WS handshake churn | `WS_UPGRADE_IP_RATE_LIMIT_*`, `WS_UPGRADE_USER_RATE_LIMIT_*` | 20 / 10 s, 10 / 10 s |
| WS inbound frame rate | `WS_FRAME_RATE_MAX` / `_WINDOW_SECONDS`, `WS_FRAME_FLOOD_STRIKES` | 30 / 10 s, 60 |
| WS `send_message` | `WS_SEND_MESSAGE_RATE_*`, `WS_SEND_MESSAGE_BURST_*` | 3 / 1 s, 40 / 60 s |
| WS per-action buckets | `WS_RECEIPTS_*`, `WS_SUBSCRIBE_PRESENCE_*`, `WS_TYPING_*`, `WS_EDIT_*` | 60/10 s, 20/10 s, 10/10 s, 20/60 s |
| REST message history | `MSG_HISTORY_RATE_*`, `MSG_HISTORY_MAX_LIMIT` | 30 / 60 s, 100 rows |
| REST upload ticket | `UPLOAD_TICKET_RATE_*`, `UPLOAD_TICKET_IP_RATE_LIMIT_*` | 5 / 60 s user, 20 / 60 s IP |
| REST detail / list reads | `DETAIL_READ_RATE_*`, `LIST_READ_RATE_*` | 60 / 60 s, 120 / 60 s |

WS close codes: `4401` auth · `4403` bad Origin · `4409` connection-limit
eviction (silent) · `4429` handshake churn or sustained frame flood.

## Build & boot

The 1 GB box can build the image but it is slow and swap-heavy. Prefer
building elsewhere and `docker save | docker load`, or just:

```bash
docker compose -f docker-compose.prod.yml build            # ~3-5 min
docker compose -f docker-compose.prod.yml up -d db redis minio
# wait for healthy:
docker compose -f docker-compose.prod.yml ps

# schema + partitions (create_all, no migrations) and storage buckets:
docker compose -f docker-compose.prod.yml run --rm app python -m scripts.init_db
docker compose -f docker-compose.prod.yml run --rm app python -m scripts.init_storage

docker compose -f docker-compose.prod.yml up -d
```

Check: `curl -fsS https://linka.example.com/healthz` → `{"database": true}`.

Open `https://linka.example.com/` (the PoC). In its settings set the API base
to the same origin (`https://linka.example.com`). OTP codes print to the app
log — `docker compose -f docker-compose.prod.yml logs -f app` — and **any code
is accepted** (deliberate demo stub, no SMS).

## Partition maintenance cron

```bash
crontab deploy/partition-maintenance.prod.crontab
```

Runs `ensure` / `prune-receipts` / `cold` / `report` inside the app
container, plus a nightly `pg_dump` to `/opt/linka/backups`.

## Updating

```bash
cd /opt/linka && git pull
docker compose -f docker-compose.prod.yml build app id_service   # id_service only when its Dockerfile/src changed
docker compose -f docker-compose.prod.yml run --rm app python -m scripts.init_db  # picks up new tables/columns/partitions
docker compose -f docker-compose.prod.yml up -d
```

The Rust `id_service` build is slow on a 1 GB box (fetches + compiles the crate
tree, ~15-20 min, swap-heavy). Prefer `docker build -f id_service/Dockerfile
-t linka-id-service:latest .` on a bigger machine + `docker save | ssh … docker
load`, then `up -d` on the host.

## Memory budget (≈, idle)

| Service | `mem_limit` | typical |
|---|---|---|
| db | 320m | ~180m |
| app | 320m | ~170m |
| minio | 256m | ~120m |
| redis | 160m | ~40m |
| id_service | 32m | ~5m |
| caddy | 64m | ~20m |

Ceilings sum above 1 GB on purpose — they are limits, not reservations; swap
covers the overlap. If the OOM killer fires, drop `minio` and move to real S3
(blank `S3_ENDPOINT_URL`, set real keys) — that frees ~120 MB.
