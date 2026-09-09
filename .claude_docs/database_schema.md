# Database Schema & Receipts Model

Read this before any change to models, CRUD, partitioning, unread counts, or the delivery/read/played receipt logic.

## Stack / migration rules
- PostgreSQL 15 / SQLAlchemy 2.0 async / asyncpg.
- **No migrations.** Schema only via `scripts/init_db.py`. `create_all` never alters an existing table — a new column on an existing dev DB needs a manual `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (init_db does this on non-`--drop` runs) or a `--drop` re-init.
- **Partition management is automated** (ADR 0005/0006): `messages` weekly + `message_receipt_log` daily dated partitions, created/frozen/pruned by `scripts/partition_maintenance.py` on a committed crontab. A DEFAULT partition is kept on both as a safety net (any row landing there is an alert). Dev gets dated partitions automatically via `init_db.py`. See the "Partition pruning in message reads" and "Scripts" sections below.

## Models (ADR 0022: `modules/<feature>/models.py` + `crud.py`; chats has `models/` + `crud/` subpkgs; `Base` in `infra/db/base.py`)
- **User**, **Chat**, **Participant**, **Message** (RANGE-partitioned by `created_at`)
- **ReservedUsername** (`reserved_usernames`, not partitioned) — see "Usernames" below.
- **PrivateChatPair** — race-free 1:1 dedup
- **MessageReceiptLog** — RANGE-partitioned by `occurred_at`, `(id, occurred_at)` PK, no FK
- **MediaBlob** (`media_blob`, not partitioned) — content-addressed dedup index (ADR 0010). `sha256` PK → `storage_key` (unique idx), `bucket`, `kind`, `mime`, `size`, `ref_count`, `created_at`, `uploaded_at` (NULL = ticket minted but bytes not yet confirmed). One row per distinct file; many `messages` rows share one `media_key`. Nullable `blur_hash` (ADR 0014) stored on first confirmed use, reused by deduped re-sends. CRUD: `crud_media_blob` (`get_blob_by_hash`/`get_blob_by_key`/`reserve_blob`/`confirm_and_ref`). See storage_and_media.md.
- **Message media columns** (all nullable): `media_key`/`media_mime`/`media_size`/`media_name`/`media_duration_seconds`, plus `media_blur_hash` (ADR 0014 — sender-computed ThumbHash base64, ≤64 chars, for lazy/tap-to-load image & video previews).
- **UserSettings** (`user_settings`) — 1:1 with users, `user_id` PK/FK `ON DELETE CASCADE`, single `settings JSONB` blob. Sparse storage: only keys the user changed. Canonical shape + defaults in `modules/settings/schema.py` (`DEFAULT_USER_SETTINGS`); reads deep-merge stored over defaults. Patches validated against that shape (unknown key / bad enum → `SettingsValidationError` → HTTP 400). Adding a setting = extend the schema module, no migration. Privacy defaults all `everyone`. See ADR 0002. CRUD: `crud_user_settings` (`get_settings_blob`, `upsert_settings_blob` via PG upsert). API: `GET`/`PATCH /users/me/settings`.

## Usernames (ADR 0017, 0018)
- **ADR 0018:** `users.display_name` was removed as an *identity* field. **ADR 0024 re-adds it as an optional presentation field:** `users.display_name VARCHAR(80)`, **nullable, no uniqueness, no index, never searchable**. Sanitised in `user_service.sanitize_display_name` (control/bidi/zero-width stripped, NFC, capped at `config.DISPLAY_NAME_MAX_LEN` = 50) before every write. Peer display = `display_name || username || phone_number`; system-message text stays `username || phone_number`. No migration — `init_db.py` adds `ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR(80)`; a deployed DB needs it run once by hand. `users.about_text` is unchanged.
- `users.username VARCHAR(32)` — `UNIQUE`, `NOT NULL`, **always stored lowercase**. Plain unique btree `ix_users_username` is the sole uniqueness authority (race-safe, DB-enforced); no CITEXT / functional index — writes are normalised first.
- Format `^[a-z][a-z0-9_]{2,31}$` (3–32, starts with a letter). Validation returns a machine `reason` code (`too_short`/`too_long`/`bad_chars`/`must_start_letter`/`reserved`/`taken`/`grace_hold`/`cooldown`) for the frontend hint. Reserved list in `config.USERNAME_RESERVED`.
- **Every new account gets one automatically** at creation (`user_service.generate_free_username` → `crud_user.create_user(username=...)`, required arg). `<adjective>_<noun>_<3–4 digits>` from an embedded word list, fallback `user_<base36>`, retried against the unique index.
- `users.username_changed_at TIMESTAMPTZ` (nullable) — NULL until the user changes it themselves (the initial auto-assignment does **not** stamp it). A change is refused (`cooldown`) until `username_changed_at + config.USERNAME_CHANGE_COOLDOWN_DAYS` (14).
- **`reserved_usernames`** — `username` PK (lowercase), `reserved_for_user_id BIGINT`, `released_at TIMESTAMPTZ`, `expires_at TIMESTAMPTZ` (`= released_at + USERNAME_RESERVED_GRACE_DAYS`, 14). While `expires_at > now()`: others refused (`grace_hold`), original owner may reclaim. Expiry checked passively (no cron required). `set_username` on a *change* stamps `username_changed_at` + inserts the old handle here.
- CRUD `crud_user`: `get_user_by_username` (exact point-lookup, **never LIKE/prefix**), `username_is_free` (not in `users`, not a live `reserved_usernames` row unless reserved for the caller), `set_username` (`UPDATE ... RETURNING`, `IntegrityError → UsernameTakenError`). Service `user_service`: `validate_username_format`, `generate_free_username`, `check_username_available`.
- Added in `init_db.py` (non-`--drop`): backfill NULL usernames per-row → `ALTER TABLE users ADD COLUMN IF NOT EXISTS username` / `username_changed_at`, `CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username`, `ALTER COLUMN username SET NOT NULL`, `CREATE TABLE IF NOT EXISTS reserved_usernames`.
- Search is **exact-match only** (ADR 0017): endpoint + PoC UI deferred; when built it returns `PublicUserOut` (no `phone_number`).

## IDs
- Every 64-bit Snowflake id crosses the wire (REST + WS, both directions) as a **JSON string**, never a number (JS loses precision past 2^53). Never `parseInt()`/`Number()` an id in the PoC. See `api/schemas.py`'s `IdStr`. Generator: `infra/ids/snowflake.py`.

## Watermark receipt model (drives every tick — O(1), group-size independent)
- `MessageStatus` (SENT/DELIVERED/READ/PLAYED) is **derived, never stored on `Message`**.
- `Participant` carries per-user watermarks: `last_delivered_message_id`, `last_read_message_id`, `last_played_message_id` (+ coarse `last_*_at` TIMESTAMPTZ, never expires).
- `Chat` carries the chat-wide rollup: `all_{delivered,read,played}_up_to_message_id` = `MIN(watermark)` across current participants, maintained by `crud_participant.recompute_chat_receipt_cursors` (one query, all three).
- `compute_message_status(message_id, chat, message_type=None)` compares id vs. those columns. PLAYED only unlocked when `message_type == AUDIO_MESSAGE_TYPE` (4).
- **Read-receipts privacy (ADR 0003, asymmetric per-reader):** in a 1:1 chat, READ/PLAYED from reader R are masked → DELIVERED on sender-facing surfaces iff `R.privacy.read_receipts == false` (R's own setting only; sender's is irrelevant). `modules/messaging/receipt_privacy.py`: `reader_hides_read_receipts` (fan-out suppress) / `read_receipts_hidden_for_message` (read views, keyed on the *other* participant). Watermarks still advance (mask is presentation-only). Groups exempt.
- `crud_message.create_message` bumps the sender's own three watermarks.
- `add_participant_to_chat` seeds a new member's watermarks at the chat's current `last_message_id`, not NULL — don't remove.
- WS actions: `mark_delivered`, `mark_read`, `mark_played` (all `{chat_id, message_id}`) → **enqueue to `receipt_log_stream`** (ADR 0037); the `receipt_log` worker advances the watermark and fans out `delivery_receipt`/`read_receipt`/`played_receipt` (carry `occurred_at`). `update_last_*_message` are **forward-only** — a redundant/behind re-mark returns `None` (the worker then writes no row / fires no event).
- Exposed: `MessageOut.status`, `ChatOut.last_message_status` (only from `chat_service.get_chat_list`; `None` from POST/PATCH chat endpoints).

## Detailed receipt log (separate history layer, read only by the per-message "info" view)
- **`message_receipt_log`**: append-only, RANGE-partitioned by `occurred_at`. Columns `chat_id, user_id, kind (2/3/4), up_to_message_id, occurred_at`. **One row per acknowledgement action**, not per (message,user).
- "When did U read msg X" = `occurred_at` of U's earliest row with `up_to_message_id >= X`. "Who read X in a group" = every current participant with such a row.
- Writes async (see realtime_and_redis.md). No log row for the sender's own message.
- Read API: `GET /chats/{chat_id}/messages/{message_id}/receipts` → `MessageReceiptsOut`. Any participant may view any message. `counts` always present (denominator = participants excluding sender); when not `truncated`, also `{delivered,read,played}_by` + `pending`. Group > `RECEIPT_NAMED_LIST_MAX_MEMBERS` (256) → `truncated: true`, counts only. `played_*` only for `type == 4`. Redis-cached 10s.
- Retention `RECEIPT_LOG_RETENTION_DAYS = 30`; coarse `Participant.last_*_at` is the fallback for older messages.
- Cron: `scripts/prune_receipt_log.py` (daily, DETACH+DROP old partitions).

## Chat list denormalization
- `Chat.last_message_preview` (first ~120 chars), `Chat.last_message_status`, `last_message_id`, `last_message_at` denormalized onto `Chat` — resolving per listed chat would hit the RANGE-partitioned `messages` with no partition to prune to.
- `last_message_preview` kept in sync on edit/delete, but only when the edited/deleted message is the chat's current `last_message_id`.
- **System messages (`sender_id = NULL`, `type = 6`) never overwrite `last_message_preview`** (`create_message` guards on `sender_id is not None`); they still bump `last_message_at`/`last_message_id`.
- `crud_message.build_last_message_preview(content, type)` → "📷 Photo"/"🎥 Video"/"🎤 Voice message"/"📎 File" for a caption-less media message.

## Partition pruning in message reads (id → created_at predicate)
- `messages` is RANGE-partitioned by `created_at`, but callers only ever have a Snowflake `id`. `infra/ids/snowflake.py` decodes an id back to wall-clock time: `id_to_timestamp_ms`, `id_to_datetime`, `id_to_datetime_range(lo, hi, *, skew)`.
- `crud_message` injects a `created_at` predicate derived from the id(s) so Postgres prunes whole weekly partitions instead of probing every one:
  - `get_message_by_id` — `created_at BETWEEN id_time(message_id) ± skew` → down to the one partition holding the id.
  - `get_chat_messages` (when `before_id` given) — `created_at <= id_time(before_id) + skew` → skips partitions newer than the cursor.
  - `count_unread_messages` (when `last_read_message_id` given) — `created_at >= id_time(cursor) - skew` → skips all history older than the watermark (the big win: forward count from an ancient cursor).
- `skew` = `config.MESSAGE_PARTITION_QUERY_SKEW_HOURS` (default 1h). Covers the gap between in-process id minting and the server-side `created_at` default; the predicate is always a safe superset, never drops a matching row.
- `message_receipt_log` is already queried by `occurred_at` directly — no equivalent shim needed.
- **Tests fabricating message ids must mint them off a real base** (`next_id() & ~0x3FFFFF`); a bare `90010` decodes to 2024 and gets pruned out of a `now()` row's partition. See `_mid()` in `tests/modules/messaging/test_crud_message.py`.

## Unread count
- `crud_message.count_unread_messages(session, chat_id, last_read_message_id)` — `COUNT(*) WHERE chat_id AND id > cursor AND sender_id IS NOT NULL AND deleted_at IS NULL`, uses `ix_messages_chat_id_id`.
- `chat_service.get_chat_list` attaches it per chat to `Participant.unread_count` (per-viewer); `GET /chats` returns `ChatListItemOut.unread_count`.

## Pinned chats (per-user)
- `Participant.pinned_at` (nullable TIMESTAMPTZ). NULL = not pinned. Per-user, no limit on count.
- CRUD `crud_participant.set_chat_pinned(session, chat_id, user_id, pinned)` — sets `pinned_at = now()` (re-pin refreshes → jumps to top) or NULL. Returns updated `Participant` / `None` if not a member.
- `get_user_chats` ordering: `pinned_first ASC` (0 pinned / 1 not), then `COALESCE(pinned_at, epoch) DESC`, then `Chat.last_message_at DESC`, `Chat.id DESC`. `before` cursor still keyed only on `(last_message_at, chat_id)` — pinned chats surface on page 1 (no cursor) and are filtered by the cursor on later pages, so no double-listing.
- Column added in `init_db.py` via `ALTER TABLE participants ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMPTZ`.

## Muted chats (per-user)
- `Participant.muted_until` (nullable TIMESTAMPTZ). NULL = not muted; a future timestamp = muted until then; "forever" = a far-future timestamp the client sends. **Client owns the duration menu** (8h/1d/1w/forever) — server stores whatever timestamp it's given. See ADR 0004.
- CRUD `crud_participant.set_chat_muted(session, chat_id, user_id, muted_until)` — set/clear, RETURNING; `None` if not a member.
- **Only server-side effect**: `modules/messaging/send.py::fan_out_message` drops recipients whose `muted_until > now()` from `offline_ids` before `notification_service.send_push`. Real-time fan-out unchanged (client filters). No cron/cleanup — expiry is passive.
- `chat_service.set_chat_muted` publishes `{event: "chat_mute_changed", chat_id, muted_until}` on `user_events:{user_id}` for multi-device sync (same mechanism as `chat_pin_changed`).
- Exposed: `ChatListItemOut.muted_until` (from `Participant.muted_until`, only via `get_chat_list`). Endpoints `PUT`/`DELETE /chats/{chat_id}/mute` (`PUT` body `{muted_until}`).
- Column added in `init_db.py` via `ALTER TABLE participants ADD COLUMN IF NOT EXISTS muted_until TIMESTAMPTZ`.

## Reply-to-message
- `Message.reply_to_message_id` (nullable, FK-less). Threaded through `crud_message.create_message` → `process_outgoing`/`fan_out_message` → `realtime/ws_router.py`. Returned on every `MessageOut` and on the live `new_message` fan-out event.

## Message edit / delete / restore / purge
- Soft delete never removes the row. `edit_message`/`delete_message`/`restore_message`/`purge_message` WS actions via `message_service` (`modules/messaging/edit_delete.py`).
- `restore_message` (original sender only, no time limit) clears `deleted_at` and fans out `message_restored` (carries `content`/`type`/`media_url`/`is_edited`). No-op once `purged_at` is set.
- **Hard "delete forever" (ADR 0021):** `purge_message` — sender-only, message must already be soft-deleted (`deleted_at IS NOT NULL`, `purged_at IS NULL`). `crud_message.purge_message` nulls `content` + all `media_*` + `reply_to_message_id`, stamps nullable `messages.purged_at` (row kept). Returns the pre-purge `media_key`; the service `deref_blob`s it (`crud_media_blob`, floors `ref_count` at 0) and on the last deref `delete_object` from S3 + `delete_blob_row` — the first real object GC path. Fans out `message_purged {chat_id, message_id}`; all clients blank the bubble permanently. `purged_at` column added in `init_db.py` (`ALTER TABLE messages ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ`).

## Scripts (`scripts/`)
- `init_db.py` (create/drop schema, no Alembic), `init_storage.py` (buckets), `seed_mock_data.py` (5 users, chats, backdated history; re-runnable), `prune_receipt_log.py`.
- `partition_maintenance.py` — single cron entrypoint (ADR 0006): `python3 -m scripts.partition_maintenance {ensure|report|cold|prune-receipts} [--dry-run]`. Wraps `manage_partitions` / `prune_receipt_log`, prints a start + ok/FAILED line, exits non-zero on failure. Schedule committed in `deploy/partition-maintenance.crontab` (+ `scripts/partition_maintenance.sh` env wrapper), installed on exactly one host — never per replica. No in-app scheduler.
- `manage_partitions.py` (ADR 0005, WIP — PARTITION_MANAGEMENT_PLAN.md): idempotent time-partition manager for `messages` (weekly) + `message_receipt_log` (daily). `--ensure` pre-creates dated partitions to `now + buffer` (`config.MESSAGE_PARTITION_PRECREATE_WEEKS` / `RECEIPT_LOG_PRECREATE_DAYS`); `--report` prints per-partition range/size/est-rows + exact DEFAULT row count (non-zero = alert); both `--dry-run`. Names: `messages_y2026w07`, `message_receipt_log_y2026m08d29`. DEFAULT partitions kept as safety net. `init_db.py` (non-`--drop` runs) calls `ensure_partitions()` after the DDL, so a fresh dev DB gets dated partitions automatically. `--migrate-default [--batch-size N] [--dry-run]` is the one-time online drain of an existing DEFAULT into dated partitions: **DETACH first** (Postgres blocks creating a partition overlapping rows in an attached DEFAULT) → create historical + forward partitions → move rows in committed batches (`ctid`-ordered `DELETE … RETURNING` → `INSERT INTO parent`) → re-ATTACH the emptied table as DEFAULT → verify counts. Recovers automatically if a prior run was interrupted between DETACH and ATTACH (re-attaches the orphaned `<parent>_default`). `--cold` freezes `messages` partitions whose whole range is older than `config.MESSAGE_PARTITION_COLD_AFTER_MONTHS` (default 12): `VACUUM FREEZE`, optional `SET TABLESPACE config.MESSAGE_PARTITION_COLD_TABLESPACE` (empty ⇒ skip move; missing tablespace ⇒ warn + skip move), then `autovacuum_enabled=false` (also the idempotency marker). `message_receipt_log` is excluded — it uses the retention DROP in `prune_receipt_log.py`. Not yet wired into cron (step 8).

## Client-side E2E encryption (ADR 0026)
- `messages.is_encrypted` (BOOLEAN NOT NULL DEFAULT FALSE) + `messages.enc_header` (JSONB, nullable). Added via `ADD COLUMN IF NOT EXISTS` in `scripts/init_db.py` (no migration). When `is_encrypted`, `content` is base64 AES-256-GCM ciphertext and `enc_header` = `{v, alg, iv, eph_pub (ECDH P-256 public JWK), wraps:{user_id:{ek,iv}}}` — all opaque to the server. Set by `crud.create_message(enc_header=...)`, which derives `is_encrypted = enc_header is not None`.
- `build_last_message_preview(content, type, is_encrypted=False)` → constant `ENCRYPTED_MESSAGE_PREVIEW` ("🔒 Encrypted message") when encrypted; the server never derives a plaintext preview.
- **`chats.last_message_enc`** (JSONB, nullable — ADR 0034, `ADD COLUMN IF NOT EXISTS`). When the last previewable message is encrypted, `{"ct": <base64 ciphertext>, "header": <enc_header>}` — the same opaque blob every recipient already got over the socket — so the client can decrypt the sidebar preview on load. `crud_message` sets it in lockstep with `last_message_preview` on every write path (send / edit-of-last / soft-delete / purge / undelete); `NULL` whenever that message is plaintext, deleted, purged, or absent. Never read server-side. Exposed as `ChatOut.last_message_enc`.
- `user_public_keys` table (`modules/users/models.py::UserPublicKey`): `user_id` PK/FK→users ON DELETE CASCADE, `public_key` JSONB (public EC/P-256 JWK), `algo` VARCHAR(32), `fingerprint` TEXT (SHA-256 hex of canonical JWK), `created_at`, `updated_at`. One current key/user; `crud.upsert_public_key` (pg_insert on_conflict). `user_service.validate_public_key` rejects a JWK carrying `d`.

## Scheduled messages (ADR 0031)
- **`scheduled_messages`** — NOT partitioned, low-volume (bounded by `SCHEDULED_MAX_PENDING_PER_USER`=100/user). A scheduled message is not a `messages` row until it fires. Model `modules/messaging/models.py::ScheduledMessage` (+ `ScheduledMessageStatus` IntEnum), CRUD `modules/messaging/crud_scheduled.py` (flat module, not a `crud/` package — the ADR's `crud/crud_scheduled.py` path predates the flat layout).
- Columns: `id` (BigInteger PK, Snowflake via `infra.ids.client.next_id`), `chat_id` FK `chats.id` ON DELETE CASCADE, `sender_id` FK `users.id` ON DELETE CASCADE, `scheduled_for` TIMESTAMPTZ NOT NULL (absolute UTC), `type` SMALLINT (1–5; 6/system rejected), `content` Text, `media_key`/`media_mime`/`media_size`/`media_name`/`media_duration_seconds`/`media_blur_hash` (nullable, same shape as `Message`), `reply_to_message_id` BigInteger (FK-less), `client_message_id` Text NOT NULL (**reused as the send idempotency key at fire time** — a worker retry can't double-send), `status` SMALLINT (0=pending/1=sent/2=cancelled/3=failed), `last_error` Text, `fire_attempts` SMALLINT (transient-failure counter, capped by `SCHEDULED_MAX_FIRE_ATTEMPTS`), `created_at`, `updated_at`.
- Indexes: `ix_scheduled_messages_sender_scheduled` (`sender_id, scheduled_for`), `ix_scheduled_messages_status_scheduled` (`status, scheduled_for` — reconcile scan).
- CRUD: `create_scheduled`, `get_scheduled_by_id`, `get_scheduled_for_update` (`FOR UPDATE SKIP LOCKED` — worker claim), `list_pending_for_user(user_id, chat_id?)`, `count_pending_for_user`, `list_all_pending` (reconcile scan), `update_scheduled` (time / caption; `set_content` flag), `cancel_scheduled` (→ status 2), `set_status(id, status, last_error?)`, `bump_fire_attempts`. Rows are kept, never physically deleted by CRUD.
- **No migration**: `init_db.py` (non-`--drop`) runs `CREATE TABLE IF NOT EXISTS scheduled_messages (...)` + the two indexes. A deployed DB needs it run once by hand (`deploy/README.md`).
- Sent/cancelled/failed rows are kept for the user's history view; a later prune can DELETE `status != 0 AND updated_at < now() - 30d` (not built).
- Trigger + worker + delivery: see realtime_and_redis.md. REST API: see backend_services_and_api.md. Media ref lifecycle: see storage_and_media.md.

## Testing note
- 200+ tests against real Postgres/Redis/MinIO containers, no mocks.
- **Ephemeral test DB (ADR 0032):** the suite creates a throwaway `test_db_<uuid hex>` per `pytest` session and drops it on teardown; the seeded dev DB and MinIO are left untouched. `conftest.py` derives the ephemeral name from the fixed server coordinate — `DATABASE_URL` need not be set. Runbook + straggler cleanup: `tests/README.md`.
- The `test_message_service.py` fan-out tests have a **pre-existing** order-dependent pollution bug (fails on clean `main` too); the failing test shifts run to run.
