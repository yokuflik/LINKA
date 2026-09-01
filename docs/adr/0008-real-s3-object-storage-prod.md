# ADR 0008 — Real AWS S3 for object storage in the demo deploy

Status: Accepted
Date: 2026-09-02

Supersedes decision 4 of ADR 0007 ("Object storage stays MinIO in-compose").

## Context

ADR 0007 ran MinIO as a compose service on the 1 GB free-tier EC2 box. In
practice:

- MinIO's `mem_limit` was 256 MB (~120 MB typical) on a host whose ceilings
  already summed past 1 GB and leaned on swap.
- Browser upload/download needs MinIO reachable at a public HTTPS origin.
  Caddy could not path-prefix it (breaks S3 SigV4), so it needed its own
  subdomain + cert — extra DNS and TLS for a self-hosted store.
- AWS S3 Free Tier (5 GB, 20k GET, 2k PUT / month) covers demo traffic at no
  cost.

## Decision

1. **No MinIO container.** Removed the `minio` service, its `depends_on`
   entry on `app`, and the `minio_data` volume from
   `docker-compose.prod.yml`.

2. **Real AWS S3**, `eu-north-1`. `S3_ENDPOINT_URL` is left **empty** so
   boto3 / aioboto3 use the default AWS endpoints. `services/storage/client.py`
   already passes `endpoint_url=S3_ENDPOINT_URL or None`; `addressing_style:
   "path"` is harmless against AWS.

3. **Two buckets**, same region:
   - `linka-media` — **private** ("Block all public access" on). Served only
     via presigned GET (`DOWNLOAD_URL_EXPIRY_SECONDS`).
   - `linka-avatars` — **public-read** (bucket policy grants `s3:GetObject`
     to `*`). `S3_AVATARS_PUBLIC_BASE_URL` is the virtual-host URL
     `https://linka-avatars.s3.eu-north-1.amazonaws.com` (no trailing slash;
     the code appends `/{key}` and keys are `{h2}/avatar/{id}{ext}`).

4. **Bucket CORS is required.** The browser PUTs/GETs cross-origin from the
   SITE origin to `*.amazonaws.com`. Each bucket needs a CORS rule allowing
   `GET, PUT, HEAD` from the SITE origin, `ETag` exposed. Without it the
   browser blocks every upload.

5. **IAM user `linka-app`** with an inline policy granting `s3:PutObject`,
   `s3:GetObject`, `s3:HeadObject`, `s3:DeleteObject` on
   `arn:aws:s3:::linka-media/*` and `arn:aws:s3:::linka-avatars/*`. Access
   key id / secret go in `.env` as `S3_ACCESS_KEY` / `S3_SECRET_KEY`.

## Consequences

- Frees ~120 MB on the host; no MinIO subdomain / cert.
- Object data now lives in AWS, outside the `pg_dump` backup. Acceptable:
  media is regenerable demo content, not source of truth.
- A misconfigured bucket CORS or a public `linka-media` bucket is now the
  most likely media bug — documented in `deploy/README.md`.
- Still a demo: no lifecycle expiry, no CloudFront in front of avatars.
