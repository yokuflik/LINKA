# Deployment (demo / single-host)

Authoritative runbook: `deploy/README.md`. Decision record: ADR 0007
(single-host `docker compose`), building on ADR 0005/0006 (partitions).

## `id_service/` — Rust Snowflake ID service (ADR 0011)

Standalone Rust + `tonic` gRPC service that mints Snowflake IDs, replacing the
in-process `infra/ids/snowflake.py` generator under load. Same bit layout (epoch
2024-01-01Z, 41-bit ms / 10-bit node / 12-bit sequence), so `id_to_datetime*`
decoders are unchanged. Lock-free `AtomicU64` CAS loop. Contract:
`proto/snowflake.proto` — `SnowflakeService.NextId() → { string id }`.
**Unary only, no batch RPC** (batching would stale the id timestamp that Postgres
uses as the `created_at` partition-routing key). `NODE_ID` env (0..=1023,
required, process exits if missing/invalid) — unique per replica. Serves
`grpc.health.v1.Health` (SnowflakeService = SERVING) on the same port `50051`.
`id_service/Dockerfile` = multi-stage rust-slim → debian-slim. Python side:
`infra/ids/client.py` (`async next_id()`), enabled by setting app env
`ID_SERVICE_ADDR` (e.g. `id_service:50051`; empty → in-process generator).
`ID_SERVICE_TIMEOUT_SECONDS` (default 0.5) bounds each call; failure → local
fallback. Wired at the async call sites (auth/chat/message ids); `receipt_log` +
`storage/client` stay on the local sync generator by design. `scripts/gen_proto.sh`
regenerates the checked-in `infra/ids/_generated/snowflake_pb2*.py` stubs.
`docker-compose.prod.yml` runs it as service `id_service` (`id_service/Dockerfile`,
`NODE_ID=2`, `mem_limit: 32m`, `grpc_health_probe` healthcheck) and sets
`ID_SERVICE_ADDR=id_service:50051` + `depends_on` on the `app`.

## Topology

One free-tier EC2 box (Ubuntu, 1 vCPU, 1 GB RAM + 2 GB swap). Everything is
`docker compose -f docker-compose.prod.yml`:

```
caddy (80/443, only published ports)
  ├── SITE_ADDRESS  → app:8000        (/auth* /users* /chats* /ws /healthz /docs*)
  ├── SITE_ADDRESS  → /srv/poc         (static PoC, bind-mounted ./poc)
  └── S3_ADDRESS    → minio:9000       (browser presigned upload/download + avatars)
app (uvicorn, --workers 1)            — one process = one full set of lifespan workers
db (postgres:15-alpine, deploy/postgres.prod.conf, vol pgdata)
redis (7-alpine, --maxmemory 128mb volatile-lru --appendonly yes, vol redisdata)
minio (vol minio_data)
id_service (rust snowflake gRPC, :50051 internal, NODE_ID=2, no volume)
```

Not HA, not horizontally scalable — intended. `SNOWFLAKE_MACHINE_ID` and
`SERVER_ID` are pinned in `.env` (one instance); `id_service` `NODE_ID` must
differ from `SNOWFLAKE_MACHINE_ID`.

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
`SNOWFLAKE_MACHINE_ID`, `SERVER_ID`, `S3_*` (endpoint host == `S3_ADDRESS`).
Demo sizing: `DB_POOL_SIZE=5`, `REDIS_MAX_CONNECTIONS=50`, stream `MAXLEN=20000`,
`SEND_STREAM_SHARDS=1`, `FANOUT_STREAM_SHARDS=1`.
`GEMINI_API_KEY` (ADR 0042, semantic search) — optional; empty disables the
feature cleanly (`GET /search/semantic` → 503) rather than erroring.

## First boot / updates

`docker compose ... run --rm app python -m scripts.init_db` (create_all +
DEFAULT partitions + `ensure_partitions` + media/receipt ALTERs — no Alembic)
then `... init_storage` (buckets + public-read avatars). Re-run `init_db` after
a `git pull` that adds tables/columns/partitions.
**One-time, after the ADR 0042 `git pull`:** the `db` service image changed to
`pgvector/pgvector:pg15` (plain `postgres:15-alpine` has no `vector`
extension) — `docker compose pull db && docker compose up -d db` (same
Postgres 15, same `pgdata` volume, no dump/restore) *before* running `init_db`,
or `CREATE EXTENSION vector` fails the same way it does on an un-updated local
dev container.

## Cron (one host only)

`crontab deploy/partition-maintenance.prod.crontab` →
`deploy/partition-maintenance.prod.sh {ensure|prune-receipts|cold|report}`
which is `docker compose exec -T app python -m scripts.partition_maintenance …`.
Same crontab also does a nightly `pg_dump` → `/opt/linka/backups` (7-day keep);
the named volumes are otherwise the only copy of the data.

**IVFFlat reindex (ADR 0042):** `crontab deploy/reindex-vectors.crontab` (or
merge its one line into the crontab above) runs `scripts/reindex_vectors.sh`
→ `python -m scripts.reindex_vectors` nightly at 03:00 UTC. Issues
`REINDEX INDEX CONCURRENTLY ix_messages_embedding_ivfflat` on an
`AUTOCOMMIT`-isolation connection (required — `CONCURRENTLY` is rejected
inside a transaction block) so the rebuild never locks `messages`; recomputes
centroids as the embedded corpus grows. Only meaningful once
`ensure_ivfflat_index` has actually been run (see `modules/vector_search/ddl.py`
— not created by `init_db.py` on an empty table).

## Caddy security block + transport hardening (ADR 0012 / step 3 — DONE)

`deploy/Caddyfile` site block has a `header` directive: `Strict-Transport-Security
"max-age=86400"` (short, **no** `includeSubDomains`/`preload` on shared
sslip.io), `X-Content-Type-Options nosniff`, `X-Frame-Options DENY`,
`Referrer-Policy strict-origin-when-cross-origin`, a CSP tuned for the PoC,
`-Server`. `request_body { max_size 2MB }` sits inside `handle @api` (uploads
bypass the API via presigned PUT). No `rate_limit` — stock `caddy:2-alpine` has
no such plugin; the coarse per-IP REST backstop (1000 / 180 s) is a `main.py`
HTTP middleware instead. App also gained `TrustedHostMiddleware`
(`ALLOWED_HOSTS`), the CORS fix (`CORS_ALLOW_ORIGINS` list; `*` → credentials
off), and a `/ws` `Origin` check (`4403`). `docker-compose.prod.yml` `app`
service is pinned to `cpus: 1.0`. New env in `deploy/env.production.example`:
`ALLOWED_HOSTS`, `TRUSTED_PROXY_IPS`, `API_IP_BACKSTOP_*`; `CORS_ALLOW_ORIGINS`
now also gates the WS handshake. Full detail:
`.claude_docs/security_and_rate_limiting.md`.

## Rust WS gateway build/deploy (ADR 0033 — LIVE in production since 2026-09-10)

`crates/ws_gateway` (repo-root Cargo workspace, alongside `id_service`) serves
`/ws`. **Never `cargo build` on the t3.micro** — `lto=fat` + `codegen-units=1`
needs >1 GB and OOMs. `deploy/ws_gateway.Dockerfile` builds a static
`x86_64-unknown-linux-musl` binary → `distroless/static:nonroot` (~2.2 MB image).

**Deploy (dev machine):**
```
docker buildx build --platform linux/amd64 -f deploy/ws_gateway.Dockerfile -t linka-ws-gateway:latest --load .
docker save linka-ws-gateway:latest | gzip | ssh <host> 'gunzip | docker load'
```
**Deploy (server):**
```
cd /opt/linka/LINKA && git pull
docker compose -f docker-compose.prod.yml build app          # app ONLY — never --build (compiles the gateway on the box → OOM)
docker compose -f docker-compose.prod.yml run --rm app python -m scripts.init_db   # picks up new tables/columns
docker compose -f docker-compose.prod.yml up -d              # NOT --build; uses the loaded gateway image
docker compose -f docker-compose.prod.yml restart caddy      # re-reads the bind-mounted Caddyfile
```
Healthcheck = the binary's own `ws_gateway healthcheck` subcommand (no shell in
distroless). Service: `mem_limit: 64m`, `cpus: 0.5`, env
`REDIS_URL`/`JWT_SECRET=${JWT_SECRET_KEY}`/`CORS_ALLOW_ORIGINS`/`APP_INTERNAL_URL=http://app:8000`,
`SERVER_ID` unset (self-gen UUID). `WS_GATEWAY_IMAGE` in `.env` overrides the
image tag (unset ⇒ `linka-ws-gateway:latest`, which `docker load` provides).

Caddy (`deploy/Caddyfile`): `/ws` → `ws_gateway:8081`; `/ws-legacy` →
`app:8000` (rewrite `* /ws`) for rollback; `/internal*` → `404`; `@api` matcher
includes `/scheduled-messages*` (the top-level `scheduled_router`).
`.dockerignore` excludes `target/`.

**`ALLOWED_HOSTS`:** the gateway calls `http://app:8000/internal/*` with
`Host: app`. `main.py` always appends `app` / `localhost` / `127.0.0.1` to
`ALLOWED_HOSTS` (never edge-routable), so `.env` needs only the public
hostname(s). Before this fix every `/internal/*` call 400'd (`Invalid host
header`) → `ws-bootstrap` failed → `chats=0` → no live delivery.

**Rollback:** the Python WS layer was deleted (ADR 0038, 2026-09-10) — there is
no `/ws-legacy` fallback any more. Rollback = `git revert` the gateway commits +
redeploy `app`.

## Known demo compromises

- **OTP is an open stub**: any 6-digit code verifies once one has been
  requested (`modules/auth/service.py` `verify_otp_and_login`, the
  `# or stored_code != code` line). No SMS. Close this before any real users.
- **Live deploy uses real AWS S3, not MinIO (ADR 0008)** — `S3_ENDPOINT_URL`
  blank, real keys/region/bucket. MinIO is local-dev / CI only (`test_minio`).
  The historical "MinIO on the app box" plan (ADR 0007) freed ~120 MB when
  dropped.
- `mem_limit` ceilings sum > 1 GB; swap covers the overlap.
