# Object Storage, Media & Avatars

Read this before touching media attachments, avatars, presigned URLs, or the `modules/media/` subpackage.

Design principle: **the app server never touches file bytes** — clients upload/download directly against storage via presigned URLs; FastAPI only mints URLs and records object keys.

## Infra
- **MinIO** container `test_minio` (S3 API 9100, console 9101, creds `linka_dev`/`linka_dev_secret`).
- `config.py` `S3_*` block: endpoint/keys, `S3_BUCKET_MEDIA` (private) / `S3_BUCKET_AVATARS` (public-read), URL expiries, `MAX_UPLOAD_BYTES_*` per kind, `ALLOWED_UPLOAD_MIME` (dict by kind), `*_BY_KIND` lookups.
- `init_storage.py` auto-runs from `main.py` lifespan (warning, not crash, if unreachable).

## `modules/media/` (kept out of business logic)
- `client.py` — `signing_client()` (cached plain-boto3, **only** `generate_presigned_url`, local HMAC, safe sync from async). `async_session()`/`client_kwargs()` (aioboto3) for **every network call** (`head_object`, `delete_object`, bucket ops) — never a sync boto3 network call. `build_object_key(kind, mime)` → `{h2}/{kind}/{snowflake}{ext}` where `h2` = 2 hex of `sha1(id)`.
- `media_service.py` — the only module other services import from. `create_upload_ticket(kind, mime, size)` validates against limits, returns presigned PUT with `Content-Type` **and** `Content-Length` pinned into the signature. `download_url(key)` = presigned GET. `public_avatar_url(key)` = plain concat, no signing. Async: `object_metadata` (HEAD, **not for the message hot path**), `object_exists`, `delete_object`, `ensure_buckets`.
- `errors.py` — `MediaValidationError` (400), `MediaNotFoundError` (404), `StorageUnavailableError` (503), mapped in `main.py`.

## Content-addressed dedup (ADR 0010)
- A viral file forwarded by N users is stored **once**. Client computes `sha256` of the raw bytes and sends it with the upload-ticket request (`{kind, mime_type, size_bytes, sha256}`).
- **`media_blob` table** (`modules/media/models.py`, `modules/media/crud.py`, not partitioned): `sha256` PK → `storage_key` (unique idx), `bucket`, `kind`, `mime`, `size`, `ref_count`, `created_at`, `uploaded_at` (NULL until confirmed in storage).
- Media object keys are now **deterministic from the hash**: `build_media_blob_key(kind, sha256, mime)` → `{h2}/{kind}/{sha256}{ext}` (`h2` = first 2 hex of the sha256). Avatars keep the old Snowflake key (`build_object_key`, no dedup).
- **`POST .../upload-ticket`**: if `sha256` is a `media_blob` row with `uploaded_at` set → returns `{storage_key, already_uploaded: true}` and **no presigned PUT** (client skips upload, sends WS message straight away). Else → `reserve_blob` (INSERT ON CONFLICT DO NOTHING, `uploaded_at` NULL) + presigned PUT via `media_service.build_media_upload_ticket`, with `x-amz-checksum-sha256` pinned into the signature (`config.S3_ENFORCE_UPLOAD_CHECKSUM`, default on) so storage rejects mismatched bytes.
- **Send path** (`media_validation._validate_media`, now takes `session`): a media key with no `media_blob` row → `MediaValidationError`. On confirmed use: HEAD-verify, then `confirm_and_ref` stamps `uploaded_at`/authoritative `mime`+`size` and bumps `ref_count`.
- `ref_count` incremented only — no decrement / no object GC yet (same as the known gap below). `create_upload_ticket` still exists, used by avatars.
- `init_db.py` creates `media_blob` via `CREATE TABLE IF NOT EXISTS` + unique idx on `storage_key`.

## Media messages
- Kinds + caps: `image` 5 MB, `audio` 5 MB, `video` 20 MB, `file` 20 MB. `Message.type` 2/3/4/5 ↔ kind via `MEDIA_KIND_BY_MESSAGE_TYPE`.
- MIME whitelist (`config.ALLOWED_UPLOAD_MIME`): `image`/`video`/`audio`/`avatar` locked to known sets. **`file` = `set()` — the "allow any non-empty MIME" sentinel**; the empty-set check is applied in **three** places (all must stay in sync): `media_service._validate_upload_request`, `modules/messaging/media_validation.py`'s `_validate_media`, and `api/schemas.py`'s `MediaUploadTicketIn` validator (this last one 422s, not 400s). Size cap + `mime` non-empty still enforced everywhere. A browser reporting no `file.type` falls back to `application/octet-stream`.
- New `Message` columns (all nullable): `media_key`, `media_mime`, `media_size`, `media_name`, `media_duration_seconds`, `media_blur_hash`. `media_url` on `MessageOut` / `new_message` is a **presigned GET attached at read time**, not a column (`modules/messaging/read_api.py`).
- **`media_blur_hash` (ADR 0014):** tiny ThumbHash (base64, ≤64 chars) computed by the **sender's browser** at send time (image thumbnail / video first frame). Client renders it instantly on chat open and only fetches the real bytes on tap. Travels on the WS `send_message` `media.blur_hash` (validated in `_validate_media`: charset `^[A-Za-z0-9+/=]*$`, len ≤ 64). Also stored on `media_blob.blur_hash` (nullable) on first confirmed use so a deduped re-send/forward reuses it; `send.py` backfills `media_blur_hash` from the blob when the payload omits it. NULL for file/audio/text/system and pre-feature sends.
- `POST /chats/{chat_id}/messages/upload-ticket` `{kind, mime_type, size_bytes}` → ticket. Participant-only. Client PUTs bytes, then sends the WS message.
- WS `send_message`: `message_type` 2/3/4/5 + `media: {key, name?, duration_seconds?}`. `_validate_media` HEADs the object and re-checks real content-type/size against per-kind limits — the client key is never trusted.
- No REST send/edit/delete for messages — WebSocket-only by design.

## Avatars (user + group)
- Same direct-to-storage model. `avatars` bucket, `avatar` upload kind, same MIME/size rules. `User.profile_pic_url` / `Chat.profile_pic_url` store the **object key**, resolved to a public URL in `UserOut`/`ChatOut` `model_validator` (absolute `http(s)://` values pass through untouched — seed data).
- **Inline avatar thumbnail (ADR 0016, supersedes 0015):** `User.profile_pic_preview` / `Chat.profile_pic_preview` (nullable Text) hold a real image — a ~256px JPEG (q 0.82) as a `data:image/jpeg;base64,…` URI (~10–30 KB) computed by the **uploader's browser** (`useMediaPlaceholder.encodeAvatarPreview`) and sent in the commit body (`AvatarCommitIn.preview` / `CreateGroupChatIn.avatar_preview`). Sized to look sharp at the circle it's actually rendered in, not a blur. `avatar_service._clean_preview` validates it (`startswith("data:image/")`, len ≤ `MAX_AVATAR_PREVIEW_LENGTH` = 65536; bad → dropped, non-fatal). Pre-2026-09-10 avatars keep their old low-res ~64px preview — no backfill. A new avatar always replaces the old preview (`write_preview=True`); `clear_*` nulls it. **No dedup/backfill** — avatars are not content-addressed. Echoed on `UserOut`/`ChatOut` (`profile_pic_preview`, plain passthrough) and on the transient `profile_updated` / `chat_updated` events. The frontend renders this small image **directly** in every `<Avatar>` (no blur); the full-res image loads once, in a full-screen lightbox, on tap. **No back-compat migration** — `init_db.py` drops the old `profile_pic_blur_hash` column (mock data).
- `avatar_service` — `set_avatar` / `set_group_avatar` / `clear_*` HEAD-verify the object, re-check limits, best-effort delete the previous object. Group variants wrapped by `chat_service` with a `ROLE_ADMIN` check + broadcast system message.
- **Bucket CORS (ADR 0016 device cache):** `ensure_buckets()` also `put_bucket_cors` on the avatars bucket — `_AVATARS_CORS_CONFIG` = `GET`/`HEAD`, `AllowedOrigins ["*"]` (avatars are already public-read), 1 h `MaxAge`. Non-fatal on failure (logged, `<img>` still works without it). This lets the PoC `fetch()` full-res avatars cross-origin (`file://` → MinIO/S3) so `poc/composables/useAvatarCache.js` can keep them in the browser's **Cache Storage** (`linka-avatar-fullres-v1`, keyed by URL origin+path). Production S3 buckets are provisioned by IaC — the same CORS rule must be set there (see `deploy/README.md`); this call is a best-effort no-op against them.
- User endpoints (`modules/users/router.py`, act on caller): `POST /users/me/avatar/upload-ticket`, `PUT /users/me/avatar {storage_key}`, `DELETE /users/me/avatar`.
- Group endpoints (`modules/chats/router.py`, admin/owner): `POST /chats/{id}/avatar/upload-ticket`, `PUT`, `DELETE`.
- `POST /chats/groups` accepts optional `avatar_storage_key` (one-shot creation-with-photo; upload via `POST /chats/groups/avatar/upload-ticket` first). A forged/missing key aborts creation.
- `PATCH /users/me` and `PATCH /chats/{id}` **cannot** set the avatar (untrusted raw key, no cleanup).

## Per-user storage quota (ADR 0028)
- Hard cap `config.STORAGE_QUOTA_BYTES` (default 1 GiB) per user. New column `users.storage_bytes_used BIGINT NOT NULL DEFAULT 0` (`init_db.py` `ADD COLUMN IF NOT EXISTS`, no migration). Existing users start at 0 — no backfill.
- **Counted per-ref, not per-object**: every confirmed media send adds `media_size`, deduped (`already_uploaded`) sends included. A fairness cap, not a bytes-at-rest / billing figure.
- **Enforcement**: `POST /chats/{chat_id}/messages/upload-ticket` (`modules/messaging/router.py`), *before* the dedup branch — `get_storage_bytes_used + body.size_bytes > STORAGE_QUOTA_BYTES` → `StorageQuotaExceededError` → HTTP **413** `{detail, reason: "storage_quota_exceeded"}` (`main.py` handler). Not enforced on the WS send path (runs in the fan-out worker, off the request path).
- **Counter**: `+size` in `media_validation._validate_media` (now takes `sender_id`, called from `send.process_outgoing`) via `crud_user.add_storage_usage`; `−size` in `edit_delete.purge_message` (ADR 0021) using the pre-purge `media_size`/`sender_id`, floored at 0. Soft delete does **not** refund — only an irreversible purge frees space.
- Small TOCTOU (parallel tickets before send) accepted for a demo hard-cap.
- PoC: `core.js` `friendlyError` maps 413 / `reason: storage_quota_exceeded` → "Storage full — delete some files to upload more." Both `useMediaUpload.js` ticket call sites already route through it.

## Object GC (ADR 0021 — "delete forever")
- `crud_media_blob.deref_blob(session, storage_key) -> int` (atomic `ref_count = GREATEST(ref_count-1, 0)`, RETURNING) + `delete_blob_row`. Called only from `modules/messaging/edit_delete.py::purge_message`: on the last deref → `media_service.delete_object(key)` (best-effort, logged) + drop the blob row. This is the **only** path that deletes a stored object.

## Scheduled-message media ref lifecycle (ADR 0031)
- A scheduled media message uploads its bytes normally **at schedule time** (upload-ticket + PUT, exactly like a live media send) and passes the `media` block to `POST /chats/{id}/scheduled-messages`.
- **On schedule**: `scheduled_service` refs the blob **+1** (`crud_media_blob.confirm_and_ref` by storage_key) so a concurrent `purge_message` of an identical earlier message can't `delete_object` the bytes before this one fires.
- **On cancel / permanent failure**: `media_service.purge_media_blob(storage_key)` (deref; last deref → S3 `delete_object` + blob-row delete — same helper as ADR 0021).
- **On fire**: `scheduled_worker` → `send_queue.enqueue_outgoing_message` → `process_outgoing` → `_validate_media` refs the blob again for the real message; the worker then derefs its schedule-time ref. Net lifecycle ref delta = +1, matching one real message.
- Editing a scheduled media message changes caption/time only; swapping the file = cancel + reschedule.

## Known gap
- No *general* lifecycle-deletion of storage objects (orphan sweeper, user-deletion cascade). Only the ADR-0021 purge path (and the ADR-0026 scheduled-cancel path, which reuses it) GCs an object.
