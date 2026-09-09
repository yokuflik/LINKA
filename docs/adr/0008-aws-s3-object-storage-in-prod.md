# ADR 0008 — Real AWS S3 for object storage in the demo deploy (no MinIO)

Status: Accepted
Date: 2026-09-09 (retro-recorded; decision predates this file)

## Context

ADR 0007 provisioned the single-host demo with **MinIO running in the compose
file** as the S3-compatible object store, on the assumption that there was no
AWS S3 budget. It also noted that switching to real S3 later is an env-only
change: blank `S3_ENDPOINT_URL`, supply real keys/region.

That switch was subsequently made. The live deploy
(`https://16-171-249-95.sslip.io/`, t3.micro EC2) runs against a **real AWS S3
bucket**, not MinIO. MinIO was dropped from the running stack to reclaim
~120 MB of RAM on the 1 GB host and to remove the Caddy hairpin for
app→storage calls. This decision was never written up; ADR 0028 already
references "real AWS S3 per ADR 0008" as if it existed.

## Decision

- **Production object storage is AWS S3.** The `minio` service is not run in
  `docker-compose.prod.yml` on the live host.
- Configuration is env-only, exactly as ADR 0007 anticipated:
  `S3_ENDPOINT_URL` empty (SDK default endpoint), real
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / region, real bucket names
  for media and avatars.
- **Local development and CI keep MinIO** (compose service `test_minio`,
  ports 9100/9101). The `modules/media` client is endpoint-agnostic, so the
  only difference between environments is env vars.
- Presigned PUT/GET (including the `x-amz-checksum-sha256` pin from ADR 0010)
  works unchanged against real S3.

## Consequences

- Frees ~120 MB on the demo host; removes a moving part from the prod stack.
- Storage now costs real money — bounded by the per-user quota in ADR 0028.
- No object lifecycle / GC beyond the ADR 0021 purge path (unchanged).
- `deploy/README.md` and `.claude_docs/deployment.md` "MinIO on the app box"
  notes are historical; the live stack is S3.
