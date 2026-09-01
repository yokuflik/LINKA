# Linka — single-host deployment (free-tier EC2, 1 GB RAM)

See ADR 0007 for the rationale. This is a demo topology, not the billion-scale
production design.

## Files

| File | Purpose |
|---|---|
| `../Dockerfile` | multi-stage image for the FastAPI app |
| `../.dockerignore` | keeps the build context small |
| `../docker-compose.prod.yml` | full stack: `app` + `caddy` + `db` + `redis` (object storage = real AWS S3, ADR 0008) |
| `env.production.example` | copy to `../.env`, fill every `<CHANGE ME>` |
| `Caddyfile` | reverse proxy: API + WS + static PoC |
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
3. **DNS:** point `linka.example.com` (A record) at the instance's public
   IP. Open inbound 80 + 443 in the security group. (No domain? set
   `SITE_ADDRESS=:80` — HTTP only, WebSocket-over-TLS features degraded.)
   Object storage is AWS S3, not self-hosted — no S3 DNS record needed.
4. **Clone to `/opt/linka`:**
   ```bash
   sudo git clone <repo> /opt/linka && sudo chown -R $USER /opt/linka
   cd /opt/linka
   mkdir -p backups
   ```

## Configure

```bash
cp deploy/env.production.example .env
# edit .env: SITE_ADDRESS, POSTGRES_PASSWORD,
# JWT_SECRET_KEY (openssl rand -hex 32), CORS_ALLOW_ORIGINS,
# and the S3 block (see "AWS S3 setup" below):
#   S3_ENDPOINT_URL= (empty), S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION,
#   S3_BUCKET_MEDIA, S3_BUCKET_AVATARS, S3_AVATARS_PUBLIC_BASE_URL
```

## AWS S3 setup (ADR 0008 — one-time, in the AWS console)

Object storage is real AWS S3 on the Free Tier; there is no MinIO container.

1. **Two buckets** in one region (e.g. `eu-north-1`):
   - `linka-media` — keep **"Block all public access" ON**. Private; the app
     serves objects via presigned GET only.
   - `linka-avatars` — **turn Block-public-access OFF** and attach a bucket
     policy granting public read:
     ```json
     {
       "Version": "2012-10-17",
       "Statement": [{
         "Sid": "PublicReadAvatars",
         "Effect": "Allow",
         "Principal": "*",
         "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::linka-avatars/*"
       }]
     }
     ```

2. **CORS on BOTH buckets** (S3 → bucket → Permissions → CORS). Without this
   every browser upload/download is blocked:
   ```json
   [{
     "AllowedHeaders": ["*"],
     "AllowedMethods": ["GET", "PUT", "HEAD"],
     "AllowedOrigins": ["https://linka.example.com"],
     "ExposeHeaders": ["ETag"]
   }]
   ```
   Use the real SITE origin (scheme + host, no path). For `SITE_ADDRESS=:80`
   with a raw IP, use `"http://<PUBLIC_IP>"`.

3. **IAM user** `linka-app` (programmatic access), inline policy:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["s3:PutObject", "s3:GetObject", "s3:HeadObject", "s3:DeleteObject"],
       "Resource": [
         "arn:aws:s3:::linka-media/*",
         "arn:aws:s3:::linka-avatars/*"
       ]
     }]
   }
   ```
   Put its access key id / secret in `.env` as `S3_ACCESS_KEY` /
   `S3_SECRET_KEY`.

4. **`.env` S3 block:**
   ```
   S3_ENDPOINT_URL=
   S3_REGION=eu-north-1
   S3_BUCKET_MEDIA=linka-media
   S3_BUCKET_AVATARS=linka-avatars
   S3_AVATARS_PUBLIC_BASE_URL=https://linka-avatars.s3.eu-north-1.amazonaws.com
   ```
   `S3_ENDPOINT_URL` **must be empty**. `S3_AVATARS_PUBLIC_BASE_URL` has **no
   trailing slash** and no `/avatars` suffix — the app appends the full
   object key itself.

## Build & boot

The 1 GB box can build the image but it is slow and swap-heavy. Prefer
building elsewhere and `docker save | docker load`, or just:

```bash
docker compose -f docker-compose.prod.yml build            # ~3-5 min
docker compose -f docker-compose.prod.yml up -d db redis
# wait for healthy:
docker compose -f docker-compose.prod.yml ps

# schema + partitions (create_all, no migrations):
docker compose -f docker-compose.prod.yml run --rm app python -m scripts.init_db
# NB: do NOT run scripts.init_storage — the S3 buckets are created in the
# AWS console (see "AWS S3 setup" above).

docker compose -f docker-compose.prod.yml up -d
```

Smoke-test S3 wiring from the app container (mints a presigned PUT, uploads a
byte, HEADs it back):

```bash
docker compose -f docker-compose.prod.yml exec app python -c "
import asyncio, urllib.request
from services.storage.media_service import create_upload_ticket, object_exists
t = create_upload_ticket('file', 'text/plain', 1)
r = urllib.request.Request(t.upload_url, data=b'x', method='PUT',
                           headers=t.required_headers)
print('PUT', urllib.request.urlopen(r).status)
print('exists', asyncio.run(object_exists(t.storage_key)))
"
```
`403`/`SignatureDoesNotMatch` on the PUT → keys/region wrong.
`NoSuchBucket` → bucket name / region in `.env` wrong.
`exists False` → the IAM user can't `HeadObject`.

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
| redis | 160m | ~40m |
| caddy | 64m | ~20m |

Object storage is AWS S3 (ADR 0008), off-box — no memory cost. Totals now
fit ~1 GB with swap as headroom.
