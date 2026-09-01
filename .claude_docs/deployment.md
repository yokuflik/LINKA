# Deployment (demo / single-host)

Authoritative runbook: `deploy/README.md`. Decision records: ADR 0007
(single-host `docker compose`) + ADR 0008 (real AWS S3, no MinIO), building
on ADR 0005/0006 (partitions).

## Topology

One free-tier EC2 box (Ubuntu, 1 vCPU, 1 GB RAM + 2 GB swap). Everything is
`docker compose -f docker-compose.prod.yml`:

```
caddy (80/443, only published ports)
  ├── SITE_ADDRESS  → app:8000        (/auth* /users* /chats* /ws /healthz /docs*)
  └── SITE_ADDRESS  → /srv/poc         (static PoC, bind-mounted ./poc)
app (uvicorn, --workers 1)            — one process = one full set of lifespan workers
db (postgres:15-alpine, deploy/postgres.prod.conf, vol pgdata)
redis (7-alpine, --maxmemory 128mb volatile-lru --appendonly yes, vol redisdata)
```

Object storage = **real AWS S3** (ADR 0008), not in compose. Browser
uploads/downloads go straight to the bucket via presigned URLs.

Not HA, not horizontally scalable — intended. `SNOWFLAKE_MACHINE_ID` and
`SERVER_ID` are pinned in `.env` (one instance).

## Image

`Dockerfile` — multi-stage: builder venv from `requirements.txt` → `python:3.13-slim`
runtime, non-root `linka` user, `HEALTHCHECK` on `/healthz`, `CMD` uvicorn one
worker, no `--reload`. `.dockerignore` strips tests/, poc/, docs, `*.md`, `.env*`.

## Environment

Single git-ignored `.env` at repo root, from `deploy/env.production.example`.
`docker-compose.prod.yml` overrides `DATABASE_URL`/`REDIS_URL` to the internal
service names, so the same `.env` works for `docker compose run/exec` scripts.

Must-set for prod (no safe default in `config.py`):
`JWT_SECRET_KEY`, `CORS_ALLOW_ORIGINS` (never `*`, `allow_credentials=True`),
`SNOWFLAKE_MACHINE_ID`, `SERVER_ID`. S3: `S3_ENDPOINT_URL` **empty**,
`S3_ACCESS_KEY`/`S3_SECRET_KEY` (IAM user), `S3_REGION`, `S3_BUCKET_MEDIA`
(private), `S3_BUCKET_AVATARS` (public-read), `S3_AVATARS_PUBLIC_BASE_URL`
(virtual-host URL of the avatars bucket, no trailing slash). Both buckets
need a CORS rule allowing the SITE origin for `GET/PUT/HEAD`.
Demo sizing: `DB_POOL_SIZE=5`, `REDIS_MAX_CONNECTIONS=50`, stream `MAXLEN=20000`,
`SEND_STREAM_SHARDS=1`, `FANOUT_STREAM_SHARDS=1`.

## First boot / updates

`docker compose ... run --rm app python -m scripts.init_db` (create_all +
DEFAULT partitions + `ensure_partitions` + media/receipt ALTERs — no Alembic).
`init_storage` is **not** run against AWS S3 — create the two buckets +
policies + CORS in the AWS console (ADR 0008 / `deploy/README.md`). Re-run
`init_db` after a `git pull` that adds tables/columns/partitions.

## Cron (one host only)

`crontab deploy/partition-maintenance.prod.crontab` →
`deploy/partition-maintenance.prod.sh {ensure|prune-receipts|cold|report}`
which is `docker compose exec -T app python -m scripts.partition_maintenance …`.
Same crontab also does a nightly `pg_dump` → `/opt/linka/backups` (7-day keep);
the named volumes are otherwise the only copy of the data.

## Known demo compromises

- **OTP is an open stub**: any 6-digit code verifies once one has been
  requested (`services/auth_service.py` `verify_otp_and_login`, the
  `# or stored_code != code` line). No SMS. Close this before any real users.
- Real AWS S3 Free Tier (ADR 0008). Media bucket **must** stay private
  ("Block all public access"); a public media bucket exposes every
  attachment. Missing bucket CORS = every browser upload blocked.
- `mem_limit` ceilings sum > 1 GB; swap covers the overlap.
