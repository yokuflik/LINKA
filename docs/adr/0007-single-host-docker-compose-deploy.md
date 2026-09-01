# ADR 0007 — Single-host `docker compose` deployment (demo / free-tier EC2)

Status: Accepted
Date: 2026-09-01

## Context

Linka needs a public, always-on demo deployment. The target is one AWS
free-tier EC2 instance (Ubuntu, 1 vCPU, **1 GB RAM**, no swap by default).
This is explicitly a demo, not the billion-message production topology the
code is *designed* for (PgBouncer, many stateless replicas, managed
Postgres, real S3 + CDN, Kubernetes CronJobs).

The repo already had only `docker-compose.yml` — a dev-fixtures file
(Postgres/Redis/MinIO on offset ports, no app container, no volumes for
Postgres, hard-coded credentials). There was no `Dockerfile`.

## Decision

1. **One `Dockerfile`, multi-stage**, for the FastAPI app. Stage 1 builds a
   venv from `requirements.txt`; stage 2 is `python:3.13-slim` + the venv
   only, running as a non-root user. No build toolchain in the final image.

2. **A separate `docker-compose.prod.yml`.** The existing `docker-compose.yml`
   stays exactly as-is for local dev/tests. The prod file adds the `app`
   service, a `caddy` reverse proxy (automatic HTTPS, WebSocket upgrade,
   serves the static PoC), named volumes for **all** stateful services, per
   service `mem_limit`, healthchecks, `restart: unless-stopped`, and **no
   host port mappings** for Postgres/Redis/MinIO (only Caddy's 80/443 are
   published).

3. **The app runs a single Uvicorn process** (`--workers 1`). The background
   consumers (send / fan-out / receipt workers, routing heartbeat) live in
   the app's FastAPI `lifespan`, so one process = one full set of workers,
   which is the whole system at demo scale. `SNOWFLAKE_MACHINE_ID` and
   `SERVER_ID` are pinned in `.env` because there is exactly one instance.

4. **Object storage stays MinIO in-compose** (no AWS S3 budget). Switching
   to real S3 later is an env-only change (`S3_ENDPOINT_URL=` empty +
   real keys) — see `.claude_docs/storage_and_media.md`.

5. **Environment split:** every secret / host-specific value comes from a
   git-ignored `.env` file, generated from the committed
   `deploy/env.production.example`. Nothing prod-specific is baked into an
   image or a compose file.

6. **2 GB swap is mandatory** on the host (documented in
   `deploy/README.md`); `docker build` is expected to run elsewhere / be
   pulled, not built on the 1 GB box.

7. **Partition maintenance** (ADR 0005/0006) runs from the host crontab via
   `docker compose exec app python -m scripts.partition_maintenance` —
   `deploy/partition-maintenance.prod.crontab`. Still exactly one scheduler.

## Consequences

- Not horizontally scalable and not HA — acceptable and intended for a demo.
- Postgres is tuned tiny (`deploy/postgres.prod.conf`: `shared_buffers=128MB`,
  `max_connections=50`). The app pool is cut to `DB_POOL_SIZE=5`.
- Losing the box loses the data unless the named volumes are backed up; a
  `pg_dump` cron is recommended in `deploy/README.md`.
- The OTP code is a deliberate open stub (any code verifies, once one has
  been requested) — see `.claude_docs/deployment.md`. Do **not** promote
  this compose file to a real user-facing environment without closing that.
