# Scheduled Messages — Implementation Plan

**Goal:** a "Schedule message" option in the `[+]` attach menu (left of the text
input) in `MessageInput.js`. User picks a future time; the message is stored
server-side and sent automatically at that time as if the user had sent it then.
Support editing, listing, and cancelling scheduled messages before they fire.

Backend change required → **ask before implementing** (CLAUDE.md rule 10). This
doc is the plan only, no code.

---

## 0. ADR first (CLAUDE.md rule 7)

New `docs/adr/0031-scheduled-messages.md`. Key decisions to record:

- **Storage**: a new non-partitioned `scheduled_messages` table (not the RANGE-
  partitioned `messages` table — a scheduled row is not a real message yet, has
  no Snowflake id semantics tied to `created_at`, and is low-volume).
- **Trigger mechanism**: a Redis **sorted set** `scheduled_messages:due` (score =
  unix epoch of `scheduled_for`) polled by a single in-app background task
  (`realtime/fanout/scheduled_worker.py`), mirroring the existing worker model in
  `main.py` lifespan. No external cron (consistent with ADR 0001's "no in-app
  scheduler" only applied to *partition* maintenance; the send workers already
  run in-process). Rejected: `pg_cron`, Celery beat, APScheduler — all add infra.
- **Delivery**: at fire time the worker calls the **existing async send path** —
  `send_queue.enqueue_outgoing_message(...)` — so scheduled messages get the same
  idempotency, media HEAD-validation, fan-out, receipts, and push as a live send.
  The scheduled row is deleted (or marked `sent`) after enqueue.
- **Time semantics**: client sends an absolute UTC ISO-8601 `scheduled_for`;
  server stores it verbatim (same pattern as `Participant.muted_until`, ADR 0004).
  Client owns the picker UI and timezone conversion.
- **Media**: the media is uploaded normally at *schedule* time (upload ticket +
  PUT already done before the schedule call), and the `media_blob` is
  **ref-counted +1** on schedule so a `purge`/GC can't remove the bytes before
  the message fires. Deref on cancel. (ADR 0010 interaction.)
- **Limits**: max N pending scheduled messages per user (e.g. 100); min lead time
  (e.g. +10s); max lead time (e.g. +1 year). Rate-limit the schedule endpoint.

---

## 1. Database schema (`database_schema.md` + `scripts/init_db.py`)

New model `modules/messaging/models.py` → `ScheduledMessage`:

| column | type | notes |
|---|---|---|
| `id` | BigInteger PK | Snowflake id (minted via `infra.ids.snowflake.next_id`, sync path — not a partition key, so batch-safe; a plain autoincrement is also fine) |
| `chat_id` | BigInteger, FK `chats.id` ON DELETE CASCADE | |
| `sender_id` | BigInteger, FK `users.id` ON DELETE CASCADE | the scheduling user |
| `scheduled_for` | TIMESTAMPTZ, NOT NULL, indexed | absolute UTC fire time |
| `type` | SMALLINT, default 1 | 1=text … 5=file (no system) |
| `content` | Text, nullable | |
| `media_key` / `media_mime` / `media_size` / `media_name` / `media_duration_seconds` / `media_blur_hash` | nullable | same shape as `Message`; captured at schedule time |
| `reply_to_message_id` | BigInteger, nullable | |
| `client_message_id` | Text, NOT NULL | generated at schedule time; reused as the send idempotency key when it fires (prevents a double-fire on worker crash/retry) |
| `status` | SMALLINT, default 0 | 0=pending, 1=sent, 2=cancelled, 3=failed |
| `created_at` | TIMESTAMPTZ, server_default now() | |
| `updated_at` | TIMESTAMPTZ | bumped on edit |
| `last_error` | Text, nullable | populated on status=failed |

Indexes: `ix_scheduled_messages_sender_id_scheduled_for`,
`ix_scheduled_messages_status_scheduled_for` (worker recovery scan).

**No migration** (project rule): `init_db.py` non-`--drop` path gets a
`CREATE TABLE IF NOT EXISTS scheduled_messages (...)`; a deployed DB needs it run
once by hand (documented in `deploy/README.md`).

CRUD → `modules/messaging/crud.py` (or a new `crud_scheduled.py`):
`create_scheduled`, `get_scheduled_by_id`, `list_pending_for_user`,
`list_due(now, limit)`, `update_scheduled` (content/time), `set_status`,
`delete_scheduled`, `count_pending_for_user`.

---

## 2. Redis due-set (`realtime_and_redis.md`)

- Key `scheduled_messages:due` — `ZADD` `{id}` with score = `scheduled_for`
  epoch seconds, on every create; `ZREM` on cancel; re-`ZADD` on edit-time.
- Worker loop: `ZRANGEBYSCORE scheduled_messages:due -inf <now> LIMIT 0 N`,
  then for each id: `ZREM` (claim), load row, if still `pending` → enqueue the
  send, `set_status(sent)`. Use a short per-id lock (`SET nx ex`) so two
  processes racing the same tick don't double-enqueue (the reused
  `client_message_id` idempotency in `process_outgoing` is the backstop).
- **Recovery / source of truth is Postgres**: on worker startup and every ~60s,
  `SELECT id, scheduled_for FROM scheduled_messages WHERE status=0` and re-`ZADD`
  (self-heals a Redis flush / missed add). The ZSET is a fast index, not the
  authority.
- Config knobs (`config/messaging_settings.py`): `SCHEDULED_POLL_INTERVAL_SECONDS`
  (default 5), `SCHEDULED_WORKER_BATCH` (100), `SCHEDULED_RECONCILE_INTERVAL_SECONDS`
  (60), `SCHEDULED_MAX_PENDING_PER_USER` (100), `SCHEDULED_MIN_LEAD_SECONDS` (10),
  `SCHEDULED_MAX_LEAD_DAYS` (365).

---

## 3. Worker (`realtime/fanout/scheduled_worker.py`)

- Mirrors `realtime/fanout/worker.py` structure but poll-based (ZSET), not a
  stream consumer — simpler, low volume. `run_forever(stop_event)`,
  `drain_once(session)` exposed for tests.
- Fire path per due row:
  1. reload row `FOR UPDATE SKIP LOCKED`, bail if not `pending`.
  2. verify sender still `is_participant(chat_id, sender_id)` — if not,
     `set_status(failed, "no longer a participant")`, skip.
  3. `send_queue.enqueue_outgoing_message(chat_id, sender_id, client_message_id,
     content, type, reply_to_message_id, media_key, media_name,
     media_duration_seconds, media_blur_hash)`.
  4. `set_status(sent)`; deref is **not** needed — the real message now holds the
     blob ref via `process_outgoing`'s `_validate_media` → `confirm_and_ref`.
     (Careful: our schedule-time +1 ref must be released exactly once here so net
     ref delta over the lifecycle is +1, matching one real message. Simplest:
     **don't** add an extra ref at schedule time; instead rely on media having a
     confirmed `media_blob` row and document that a user purging the *source*
     media before the scheduled send is an accepted edge — OR add the ref and
     deref here. Decide in the ADR; leaning "add ref at schedule, deref on fire
     and on cancel".)
  5. transient failure (DB/Redis) → leave `pending`, re-`ZADD` with a short
     backoff score, let the next poll retry; cap retries → `failed`.
- Fan-out `message_failed` is already surfaced to the sender by the existing send
  worker if the enqueued send fails permanently downstream. Additionally emit a
  new `scheduled_message_sent` / `scheduled_message_failed` user-channel event so
  the client can update its "Scheduled" list live.
- Register in `main.py` lifespan alongside `send_task` / `fanout_task`.

---

## 4. REST API (`modules/messaging/router.py` + `backend_services_and_api.md`)

WebSocket is message-send-only by design; scheduling is management state, so REST
fits better (like chat pin/mute). New routes:

| method | path | body / query | returns |
|---|---|---|---|
| `POST` | `/chats/{chat_id}/scheduled-messages` | `{client_message_id, scheduled_for, message_type?, content?, media?{key,name?,duration_seconds?,blur_hash?}, reply_to_message_id?}` | `ScheduledMessageOut` (201) |
| `GET` | `/scheduled-messages` | `?chat_id=` optional filter | `[ScheduledMessageOut]` (pending only, ordered by `scheduled_for`) |
| `PATCH` | `/scheduled-messages/{id}` | `{scheduled_for?, content?}` (sender only, pending only) | `ScheduledMessageOut` |
| `DELETE` | `/scheduled-messages/{id}` | — | 204 (sender only, pending only) |

- Service layer `modules/messaging/scheduled_service.py`:
  `schedule_message`, `list_scheduled`, `reschedule`, `cancel_scheduled`.
  Validation: participant check, `scheduled_for` within
  `[now+MIN_LEAD, now+MAX_LEAD]`, `count_pending_for_user < MAX_PENDING`,
  content length via existing `_check_content_length`, media via a
  **non-fatal** existence check (or defer full HEAD to fire time — reuse
  `_validate_media` but tolerate it, since bytes were just PUT).
- Errors → `main.py` mapping: `ScheduledLimitExceededError` 429/409,
  `ScheduledTimeInvalidError` 400, `NotAParticipantError` 403,
  `ScheduledMessageNotFoundError` 404.
- Rate limit: new `scheduled_write` bucket (e.g. 20/60s per user) in
  `modules/messaging/router.py`, following the step-7 pattern.
- `ScheduledMessageOut` schema in `api/schemas.py` — ids as `IdStr`,
  `scheduled_for` ISO, `media_url` presigned GET (so the client can preview a
  scheduled photo), `status`.

---

## 5. Media interaction (`storage_and_media.md`, ADR 0010)

- Client flow unchanged up to the point of send: pick file → downscale/ThumbHash
  → sha256 → upload-ticket → PUT. Instead of `send_message` it calls
  `POST /chats/{id}/scheduled-messages` with the same `media` block.
- On schedule: `crud_media_blob.confirm_and_ref` (idempotent for an already-
  confirmed blob) **+1 ref** so a concurrent `purge_message` of an identical
  earlier message can't `delete_object` the bytes.
- On cancel / failure: `deref_blob` (floors at 0; last deref → S3 delete + row
  delete, same as purge).
- On fire: the real `process_outgoing` refs again for the actual message; the
  scheduled worker derefs its schedule-time ref → net +1, correct.

---

## 6. Frontend (`frontend.md`, PoC)

### 6.1 `components/MessageInput.js`
- Add a third entry to the `[+]` attach menu: **"⏰ Schedule message"** →
  `$emit('open-schedule')`. (Menu currently: Photos & Videos, Documents.)
- Optional: also allow scheduling the *currently typed text* — if
  `messageInput.trim()` is non-empty when the menu opens, the schedule modal
  pre-fills it.

### 6.2 New `components/ScheduleMessageModal.js` + `composables/useScheduleMessage.js`
- Modal style matches `SettingsModal` / `NewChatModal`.
- Fields: message text (pre-filled from composer or empty), optional media
  preview (if launched from a picked file — reuse `useMediaUpload` pipeline),
  a `datetime-local` picker + quick presets ("In 1 hour", "Tonight 8pm",
  "Tomorrow 9am"). Convert local → UTC ISO on submit.
- `useScheduleMessage` state: `showScheduleModal`, `scheduleForm
  {content, scheduled_for, media}`, `scheduleBusy`, `scheduleError`,
  `scheduledMessages` (list), `openScheduleModal`, `submitSchedule`,
  `loadScheduledMessages`, `editScheduled`, `cancelScheduled`.
- `submitSchedule`: if media, run the existing upload pipeline first
  (`useMediaUpload` — refactor its "prepare + upload, return media block"
  portion so both live-send and schedule can call it), then
  `POST /chats/{id}/scheduled-messages`. Graceful error UX per `frontend.md`
  (friendlyError, InlineAlert in the modal, toast on success:
  "Message scheduled for <relative time>").

### 6.3 Scheduled list UI
- A small **"⏰ N scheduled"** chip above the composer (or in the chat header
  menu) when `scheduledMessages` for the active chat is non-empty → opens a
  list view (reuse the modal) showing each pending message, its fire time,
  Edit / Cancel buttons.
- `GET /scheduled-messages?chat_id=` on chat open (in `useChatOpen.selectChat`
  or lazily when the chip is tapped).

### 6.4 Live updates (`composables/useWsRouter.js`)
- Handle new user-channel events:
  - `scheduled_message_sent {id, chat_id, message_id}` → remove from
    `scheduledMessages`; the normal `new_message` echo renders the bubble.
  - `scheduled_message_failed {id, chat_id, reason}` → mark the list row failed,
    toast "A scheduled message couldn't be sent: <friendly reason>".
- Multi-device: the `POST`/`PATCH`/`DELETE` responses don't auto-sync other
  tabs. Optional: echo `scheduled_message_changed` on `user_events:{user_id}`
  (same mechanism as `chat_pin_changed`) so other devices refresh their list.

### 6.5 Syntax-check `index.html` inline scripts after any edit there
(per `frontend.md` hard rule — the two-script `node --check` procedure).

---

## 7. Edge cases & decisions to lock in the ADR

| case | handling |
|---|---|
| Chat deleted before fire | FK `ON DELETE CASCADE` removes the scheduled row; worker's `pending` load just misses it. |
| Sender removed from group before fire | worker participant re-check → `status=failed`, notify sender. |
| Sender's account deleted | `ON DELETE CASCADE`. |
| Scheduled time in the past by the time worker runs (clock skew / downtime) | fire immediately (it's "due"). |
| Server down across the fire time | on restart, reconcile scan finds it overdue → fires at once (accept the delay; document it). |
| Duplicate fire (worker crash after enqueue, before `set_status`) | reused `client_message_id` → `process_outgoing` idempotency → `MessageAlreadySentError` → no double message. |
| Editing content of a media scheduled message | allowed for the caption only; changing the media = cancel + reschedule. |
| Reply target message deleted before fire | send proceeds; `reply_to_message_id` is FK-less already, client renders "Original message". |
| Huge backlog fires at once (user scheduled 100 for the same minute) | worker batches; the send queue + `send_message_burst` limiter naturally paces fan-out; scheduled sends bypass the *WS* per-user limiter (they're server-originated) but still go through the send stream. |
| Timezone: user in a different tz than server | client always sends absolute UTC; server never interprets local time. |

---

## 8. Testing (`database_schema.md` testing note — DB tests wipe dev DB)

- `tests/modules/messaging/test_scheduled_service.py` — schedule/list/edit/cancel,
  limit + lead-time validation, participant check.
- `tests/modules/messaging/test_scheduled_worker.py` — `drain_once` fires a due
  message (assert it lands on `message_send_stream`), skips non-participant,
  idempotent on double-run, reconcile re-populates the ZSET.
- `tests/test_scheduled_messages_api.py` — the 4 REST routes incl. auth (403 for
  a non-sender editing), 404, rate limit.
- Media: one test that a scheduled photo refs the blob and a cancel derefs it.
- Re-run `init_db` + `seed_mock_data` after (dev DB wiped).

---

## 9. Docs to update in the same task (AUTO-MAINTENANCE rule)

- `docs/adr/0031-scheduled-messages.md` — **write first**.
- `.claude_docs/database_schema.md` — `ScheduledMessage` model + CRUD + no-migration note.
- `.claude_docs/backend_services_and_api.md` — new routes, `scheduled_service`, worker, rate-limit bucket, known-gaps update.
- `.claude_docs/realtime_and_redis.md` — `scheduled_messages:due` ZSET + poll worker + reconcile.
- `.claude_docs/storage_and_media.md` — schedule-time ref/deref lifecycle.
- `.claude_docs/security_and_rate_limiting.md` — `scheduled_write` bucket.
- `.claude_docs/frontend.md` — attach-menu entry, `ScheduleMessageModal`, `useScheduleMessage`, new WS events.
- `CLAUDE.md` — add ADR 0031 to the index table.
- `deploy/README.md` — the one-time `CREATE TABLE scheduled_messages` ALTER for the live DB.

---

## 10. Suggested build order

1. ✅ **DONE** — ADR 0031 + `.claude_docs` stubs (database_schema / realtime_and_redis / backend_services_and_api / storage_and_media / security_and_rate_limiting / frontend) + CLAUDE.md index entry.
2. Model + CRUD + `init_db.py` DDL.
3. `scheduled_service.py` + errors + `main.py` error mapping.
4. REST router + `ScheduledMessageOut` schema + rate-limit bucket.
5. `scheduled_worker.py` + Redis due-set + `main.py` lifespan wiring + reconcile.
6. ✅ **DONE** — Backend tests: `tests/modules/messaging/test_scheduled_service.py`,
   `test_scheduled_worker.py`, `test_scheduled_media.py`, `tests/test_scheduled_messages_api.py`.
7. ✅ **DONE** — PoC: `useMediaUpload` exposes `prepareMediaBlock(file, kind, {chatId, durationSeconds?, onBlurHash?})` (shrink + ThumbHash + sha256 + upload-ticket + PUT → `{key,name,mime,size,blur_hash,duration_seconds,file}`) + `mediaKindForMime`; `sendMediaMessage` now calls it.
8. ✅ **DONE** — PoC: `useScheduleMessage` + `ScheduleMessageModal`/`ScheduleListModal` + `MessageInput` "⏰ Schedule message" menu entry + `open-schedule` emit; "⏰ N scheduled" chip above the composer; list Edit/Cancel; `useWsRouter` `scheduled_message_sent`/`_failed` handling; list loads on chat open via `watch(activeChatId)`. Text + media schedule via `prepareMediaBlock`.
9. ~~scheduled-list UI + `useWsRouter` events + chip~~ (folded into step 8).
10. Manual test pass; syntax-check inline scripts; deploy (DB ALTER first).
