# Linka — single-host deployment (free-tier EC2, 1 GB RAM)

See ADR 0007 for the rationale. This is a demo topology, not the billion-scale
production design.

## Files

| File | Purpose |
|---|---|
| `../Dockerfile` | multi-stage image for the FastAPI app |
| `../.dockerignore` | keeps the build context small |
| `../docker-compose.prod.yml` | full stack: `app` + `caddy` + `db` + `redis` + `minio` |
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
```

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
docker compose -f docker-compose.prod.yml build app
docker compose -f docker-compose.prod.yml run --rm app python -m scripts.init_db  # picks up new tables/columns/partitions
docker compose -f docker-compose.prod.yml up -d
```

## Memory budget (≈, idle)

| Service | `mem_limit` | typical |
|---|---|---|
| db | 320m | ~180m |
| app | 320m | ~170m |
| minio | 256m | ~120m |
| redis | 160m | ~40m |
| caddy | 64m | ~20m |

Ceilings sum above 1 GB on purpose — they are limits, not reservations; swap
covers the overlap. If the OOM killer fires, drop `minio` and move to real S3
(blank `S3_ENDPOINT_URL`, set real keys) — that frees ~120 MB.
