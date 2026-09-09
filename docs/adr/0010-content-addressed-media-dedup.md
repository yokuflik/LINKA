# ADR 0010 — Content-addressed media dedup (upload once, reference many)

Status: Accepted
Date: 2026-09-02

## Context

Every media message today gets a **fresh random object key** (`build_object_key`
mints a new Snowflake per upload ticket). A viral video forwarded by 10 000
users is stored 10 000 times. At the target scale (tens of billions of
messages) the object store, not Postgres, becomes the cost centre.

The dedup must live in the **backend**, not the storage server: MinIO / S3 has
no "reject a duplicate" primitive, and we want to skip the upload transfer
entirely, not just deduplicate at rest.

## Decision

Introduce a **content-addressed blob index**. The client hashes the file
(`sha256` over the raw bytes) and the server keys stored objects by that hash.

### New table `media_blob`

| column | type | notes |
|---|---|---|
| `sha256` | `TEXT` PK | lowercase hex, 64 chars |
| `storage_key` | `TEXT NOT NULL` | key in the private media bucket |
| `bucket` | `TEXT NOT NULL` | |
| `kind` | `TEXT NOT NULL` | image/video/audio/file — first uploader wins |
| `mime` | `TEXT NOT NULL` | authoritative, from the storage HEAD |
| `size` | `BIGINT NOT NULL` | authoritative |
| `ref_count` | `BIGINT NOT NULL DEFAULT 0` | messages pointing at this blob |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `uploaded_at` | `TIMESTAMPTZ` | NULL until the object is confirmed in storage |

Not partitioned — one row per distinct file, orders of magnitude smaller than
`messages`.

### Object key

Media keys become **deterministic from the hash**:
`build_media_blob_key(kind, sha256, mime)` → `{h2}/{kind}/{sha256}{ext}` where
`h2` = first 2 hex of the sha256 (write spreading, same rationale as before).
Two racing uploads of identical bytes therefore target the *same* key —
idempotent, last write wins, bytes identical. Avatars keep the old
Snowflake-keyed scheme (no dedup there).

### Upload-ticket flow (`POST /chats/{chat_id}/messages/upload-ticket`)

Request gains `sha256` (64-hex, validated). Response gains
`already_uploaded: bool`.

1. `sha256` in `media_blob` **and** `uploaded_at` set → return
   `{storage_key, already_uploaded: true}`, **no presigned PUT**. Client skips
   the upload and sends the WS message straight away.
2. Otherwise → `INSERT ... ON CONFLICT (sha256) DO NOTHING` a row with
   `uploaded_at = NULL`, return a presigned PUT for the deterministic key with
   **`x-amz-checksum-sha256` pinned into the signature** (base64 of the digest).
   Storage rejects a body whose bytes don't match the claimed hash. Where the
   backend is configured without checksum support (`S3_ENFORCE_UPLOAD_CHECKSUM
   = false`), the signature omits it and integrity falls back to the
   HEAD size check already done at send time.

### Send-path validation (`services/messaging/media_validation._validate_media`)

Unchanged HEAD verification, plus:
- look up the blob row by `storage_key`; a media key with no `media_blob` row
  is rejected (the client must go through the ticket endpoint).
- on first confirmed use, set `uploaded_at`, `mime`, `size` from the HEAD.
- `UPDATE media_blob SET ref_count = ref_count + 1`.

`messages` schema is **unchanged** — `media_key` still stores the object key;
many message rows now legitimately share one key.

### ref_count / garbage collection

`ref_count` is incremented on send. **No decrement and no object deletion yet**
— consistent with the existing "no lifecycle-deletion of storage objects"
known gap. When GC is built it decrements on message hard-delete and sweeps
`ref_count = 0 AND uploaded_at < now() - grace`.

## Consequences

- A forwarded/re-sent file uploads **once**; subsequent sends are a single
  indexed `SELECT` + `UPDATE`.
- Dedup is **global across all users**. A private file uploaded independently
  by two users resolves to one object — acceptable (each already holds the
  bytes) and standard (WhatsApp/Telegram behave the same).
- Object keys are no longer unguessable-by-Snowflake, but the bucket stays
  private and every GET is a short-lived presigned URL, so knowing a key is
  worthless without also knowing the hash of content you already have.
- New table needs an `init_db.py` `CREATE TABLE IF NOT EXISTS` entry
  (no migrations).
