# ADR 0021 — Hard "delete forever" (message purge) + S3 object GC on last deref

Status: Accepted
Date: 2026-09-07

## Context

Message delete today is a **soft delete** (ADR-less, `crud_message.soft_delete_message`):
`deleted_at` is stamped, the row and its `content` / `media_key` stay, and the
original sender can `restore_message` with no time limit. Media objects are never
removed — `media_blob.ref_count` is incremented only, "no object GC" is a
documented known gap (`.claude_docs/storage_and_media.md`).

Requirement: after a message is soft-deleted, its sender should be able to
**purge** it — wipe the text to nothing, make restore impossible, and for a
media message actually remove the bytes from S3 when nothing else references them.

## Decision

New WS action **`purge_message`** `{chat_id, message_id}`, sender-only, and the
message **must already be soft-deleted** (`deleted_at IS NOT NULL`). Not
reversible.

### DB

- New nullable column **`messages.purged_at TIMESTAMPTZ`**. No migration —
  `ALTER TABLE messages ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ` in
  `init_db.py` (non-`--drop`).
- `crud_message.purge_message(session, chat_id, message_id)`:
  overwrites `content=NULL`, `media_key/media_mime/media_size/media_name/`
  `media_duration_seconds/media_blur_hash = NULL`, `reply_to_message_id=NULL`,
  `is_edited=False`, stamps `purged_at=now()` (keeps `deleted_at` set). Row is
  **never physically removed** (partition PK, cheap at scale — same reasoning as
  soft delete). Returns the pre-purge `media_key` so the service can deref the
  blob. Chat-list preview: leaves the existing `🚫 Message deleted` tombstone.
- `undelete_message` gains a `purged_at IS NULL` guard → restore of a purged
  message is a no-op.

### Media blob GC (closes the known gap, for this path only)

- `crud_media_blob.deref_blob(session, storage_key) -> int` — atomic
  `ref_count = GREATEST(ref_count - 1, 0)` … `RETURNING ref_count`.
- `services/storage/media_service.purge_media_blob(session, storage_key)` —
  `deref_blob`; if the new count is `0`, `delete_object(storage_key)` from
  `S3_BUCKET_MEDIA` (best-effort, logged) **and** delete the `media_blob` row.
  Two purges racing to 0 → the object delete / row delete is idempotent
  (`delete_object` on a missing key is a no-op; row delete by PK is safe).
- No change to the upload / send ref path.

### Real-time

- `message_purged` event `{chat_id, message_id}` fanned out on the chat
  (`realtime_service.publish_event`), same channel as `message_deleted`.
- All clients blank the bubble permanently and drop the "Restore" / "Delete
  forever" menu options. A media `<img>`/`<video>` whose object is now gone will
  404 — expected; the client shows the tombstone regardless.

### History / read API

- `read_api.get_message_history` already filters `deleted_at IS NULL` for normal
  loads, so a purged (still-deleted) row is not returned. `include_deleted`
  callers see `content=None`, no media.

### Frontend

- `MessageContextMenu` gains **"Delete forever"** (`@purge`), shown only when
  `canPurge` = own message (`sender_id === currentUserId`) **and**
  `deleted_at != null` **and** `purged_at == null`. Red, below "Restore".
- `window.confirm`-style guard (existing toast/modal pattern) before sending.
- `useWsRouter` `message_purged` → set `m.purged_at`, clear content/media on the
  row, re-render as the deleted tombstone.

## Consequences

- True unsend for the sender's own already-deleted messages; irreversible by
  design.
- First real S3 object deletion path in the system. Still **no** general
  lifecycle GC (orphan sweeper, user-deletion cascade) — out of scope.
- `message_receipt_log` rows for a purged message are left to the 30-day
  retention prune (no id-targeted delete).
