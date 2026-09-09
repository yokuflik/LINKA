# ADR 0031 — Scheduled messages

Status: Accepted
Date: 2026-09-09

## Context

Users want to compose a message now and have it delivered automatically at a
future time ("send tomorrow 9am"). Entry point in the PoC: a new **"Schedule
message"** item in the composer's `[+]` attach menu (`MessageInput.js`).

Today the send path is WebSocket-only and fully async (FANOUT_REWRITE_PLAN steps
1–4): `send_message` → `send_queue.enqueue_outgoing_message` (XADD
`message_send_stream`) → `SendWorker` → `process_outgoing` (idempotency,
`_validate_media` HEAD, persist, `enqueue_fanout`). There is no server-side
timer / scheduler of any kind — the only cron is partition maintenance, run
off-host (ADR 0006).

## Decision

A scheduled message is **not** a `messages` row until it fires. It lives in a new
low-volume table and is turned into a real message at fire time by re-using the
existing async send path.

### Storage — `scheduled_messages` (not partitioned)

New model `modules/messaging/models.py::ScheduledMessage`, CRUD in
`modules/messaging/crud/crud_scheduled.py`.

| column | type | notes |
|---|---|---|
| `id` | BigInteger PK | Snowflake, minted via `infra.ids.snowflake.next_id` (sync path; not a `created_at` partition key so batching is irrelevant) |
| `chat_id` | BigInteger FK `chats.id` ON DELETE CASCADE | |
| `sender_id` | BigInteger FK `users.id` ON DELETE CASCADE | scheduling user |
| `scheduled_for` | TIMESTAMPTZ NOT NULL | absolute UTC; client converts from local |
| `type` | SMALLINT default 1 | 1=text…5=file; **6 (system) rejected** |
| `content` | Text nullable | |
| `media_key`/`media_mime`/`media_size`/`media_name`/`media_duration_seconds`/`media_blur_hash` | nullable | captured at schedule time, same shape as `Message` |
| `reply_to_message_id` | BigInteger nullable | FK-less (same as `Message`) |
| `client_message_id` | Text NOT NULL | generated at schedule time; **reused as the send idempotency key** when it fires → a worker crash/retry can't double-send |
| `status` | SMALLINT default 0 | 0=pending, 1=sent, 2=cancelled, 3=failed |
| `last_error` | Text nullable | set when `status=3` |
| `created_at` | TIMESTAMPTZ server_default now() | |
| `updated_at` | TIMESTAMPTZ nullable | bumped on edit/status change |

Indexes: `ix_scheduled_messages_sender_scheduled` (`sender_id, scheduled_for`),
`ix_scheduled_messages_status_scheduled` (`status, scheduled_for` — reconcile scan).

**No migration** (project rule). `scripts/init_db.py` non-`--drop` path runs
`CREATE TABLE IF NOT EXISTS scheduled_messages (...)` + the two indexes; a
deployed DB needs it run once by hand (documented in `deploy/README.md`).

### Trigger — Redis ZSET + in-process poll worker

- Redis key `scheduled_messages:due` — `ZADD {id}` score = `scheduled_for` epoch
  seconds on create; `ZREM` on cancel; re-`ZADD` on reschedule.
- New `realtime/fanout/scheduled_worker.py`, one task per process, started in
  `main.py` lifespan next to `send_task` / `fanout_task`. **Poll-based, not a
  stream consumer** — volume is tiny and a ZSET range query is the natural fit.
  - Every `SCHEDULED_POLL_INTERVAL_SECONDS` (5): `ZRANGEBYSCORE
    scheduled_messages:due -inf <now> LIMIT 0 SCHEDULED_WORKER_BATCH`.
  - Per id: `ZREM` (claim) → `SELECT ... FOR UPDATE SKIP LOCKED`, bail unless
    `status=0` → re-check `is_participant(chat_id, sender_id)` (else
    `status=3, last_error="no longer a participant"`) →
    `send_queue.enqueue_outgoing_message(...)` with the stored
    `client_message_id` → `status=1`.
  - Transient failure (DB/Redis) → leave `status=0`, re-`ZADD` with score
    `now + backoff`; after `SCHEDULED_MAX_FIRE_ATTEMPTS` → `status=3`.
- **Postgres is the source of truth.** On worker startup and every
  `SCHEDULED_RECONCILE_INTERVAL_SECONDS` (60): `SELECT id, scheduled_for FROM
  scheduled_messages WHERE status=0` → re-`ZADD` all (self-heals a Redis flush,
  a missed add, or a fire time that elapsed during downtime — an overdue row is
  simply "due" and fires on the next poll).
- Two processes racing the same tick: the `ZREM` claim + `FOR UPDATE SKIP
  LOCKED` + the reused-`client_message_id` idempotency in `process_outgoing`
  together guarantee exactly one real message.

### Delivery path

At fire time the worker calls `send_queue.enqueue_outgoing_message` — **the same
entry point `_handle_send_message` uses**. Scheduled messages therefore get
identical idempotency, media HEAD-validation, fan-out, receipts, offline push,
and ordering. They bypass only the *WebSocket* per-user rate limiter (they are
server-originated); the send stream + `send_message_burst` window still pace the
downstream fan-out if a user scheduled many for the same instant.

### Media (ADR 0010 interaction)

The client uploads the file normally at **schedule** time (upload-ticket + PUT,
exactly as for a live media send) and passes the `media` block to the schedule
endpoint. To stop a concurrent `purge_message` of an identical earlier message
from deleting the bytes before this one fires:

- On schedule: `crud_media_blob.confirm_and_ref(sha-less: by storage_key)` **+1
  ref**.
- On cancel / permanent failure: `media_service.purge_media_blob` (deref; last
  deref → S3 `delete_object` + row delete — same helper as ADR 0021).
- On fire: `process_outgoing` → `_validate_media` refs the blob again for the
  real message; the worker then derefs its schedule-time ref. Net lifecycle ref
  delta = +1, matching one real message.

### REST API (not WebSocket)

Scheduling is management state (like chat pin/mute, ADR 0004), so it is REST, in
`modules/messaging/router.py`, thin over `modules/messaging/scheduled_service.py`:

| method | path | body / query |
|---|---|---|
| `POST` | `/chats/{chat_id}/scheduled-messages` | `{client_message_id, scheduled_for, message_type?, content?, media?{key,name?,duration_seconds?,blur_hash?}, reply_to_message_id?}` → `ScheduledMessageOut` 201 |
| `GET` | `/scheduled-messages?chat_id=` | pending only, `scheduled_for` asc → `[ScheduledMessageOut]` |
| `PATCH` | `/scheduled-messages/{id}` | `{scheduled_for?, content?}` — sender-only, pending-only |
| `DELETE` | `/scheduled-messages/{id}` | 204 — sender-only, pending-only |

Validation (`scheduled_service`): participant check; `scheduled_for` ∈
`[now + SCHEDULED_MIN_LEAD_SECONDS (10), now + SCHEDULED_MAX_LEAD_DAYS (365)]`;
`count_pending_for_user < SCHEDULED_MAX_PENDING_PER_USER (100)`; content length
via the existing `_check_content_length`; `message_type != 6`. Media is checked
leniently (bytes were just PUT) — the authoritative HEAD is at fire time.

Errors → `main.py` mapping: `ScheduledTimeInvalidError` 400,
`ScheduledLimitExceededError` 409, `NotAParticipantError` 403,
`ScheduledMessageNotFoundError` 404. Rate limit: new `scheduled_write` sliding
bucket (20 / 60 s per user) in the router, per the step-7 pattern.

`ScheduledMessageOut` in `api/schemas.py`: ids as `IdStr`, `scheduled_for` ISO,
`media_url` presigned GET (preview a scheduled photo), `status`, `content`,
`message_type`, `chat_id`.

### Real-time / multi-device

New user-channel events on `user_events:{sender_id}` (same mechanism as
`chat_pin_changed`):

- `scheduled_message_sent {id, chat_id, message_id}` — client removes the row
  from its Scheduled list; the normal `new_message` echo renders the bubble.
- `scheduled_message_failed {id, chat_id, reason}` — client marks the row failed
  + toasts a friendly line.
- `scheduled_message_changed {id, chat_id, op}` (`op` ∈ created/updated/cancelled)
  — emitted by the REST handlers so the user's other tabs refresh the list.

### Frontend (PoC)

- `MessageInput.js` — third attach-menu item **"⏰ Schedule message"** →
  `$emit('open-schedule')`; pre-fills the typed text if the composer is non-empty.
- New `components/ScheduleMessageModal.js` + `composables/useScheduleMessage.js`
  (`SettingsModal` styling): text, optional media (reuse `useMediaUpload`'s
  prepare+upload portion, refactored into a reusable "return media block" fn),
  a `datetime-local` picker + presets (In 1 hour / Tonight 8pm / Tomorrow 9am),
  local→UTC on submit.
- A **"⏰ N scheduled"** chip above the composer when the active chat has pending
  scheduled messages → opens the list (Edit / Cancel per row). `GET
  /scheduled-messages?chat_id=` on chat open.
- `useWsRouter.js` handles the three new events. Graceful error UX per
  `.claude_docs/frontend.md` (friendlyError / InlineAlert / toast).
- Syntax-check `index.html`'s inline scripts after editing (project hard rule).

### Config knobs (`config/messaging_settings.py`)

`SCHEDULED_POLL_INTERVAL_SECONDS` (5), `SCHEDULED_WORKER_BATCH` (100),
`SCHEDULED_RECONCILE_INTERVAL_SECONDS` (60), `SCHEDULED_MAX_FIRE_ATTEMPTS` (5),
`SCHEDULED_FIRE_BACKOFF_SECONDS` (30), `SCHEDULED_MAX_PENDING_PER_USER` (100),
`SCHEDULED_MIN_LEAD_SECONDS` (10), `SCHEDULED_MAX_LEAD_DAYS` (365),
`SCHEDULED_WRITE_RATE_MAX` (20) / `SCHEDULED_WRITE_RATE_WINDOW_SECONDS` (60).

## Edge cases

| case | handling |
|---|---|
| Chat / sender account deleted before fire | `ON DELETE CASCADE` removes the row; worker load misses it. |
| Sender left / removed from group before fire | worker participant re-check → `status=3` + `scheduled_message_failed`. |
| Fire time elapsed during server downtime | reconcile re-adds it as overdue → fires immediately on next poll (delay accepted, documented). |
| Duplicate fire (crash after enqueue, before `status=1`) | reused `client_message_id` → `process_outgoing` `MessageAlreadySentError` → no second message; worker still sets `status=1`. |
| Editing a media scheduled message | caption/time only; changing the file = cancel + reschedule. |
| Reply target deleted before fire | send proceeds; `reply_to_message_id` is FK-less; client renders "Original message". |
| Redis `scheduled_messages:due` flushed | reconcile scan rebuilds it within ≤60 s. |
| User schedules 100 for the same minute | worker batches; send stream + `send_message_burst` pace fan-out. |
| Clock skew between app and Redis | scores and `now()` both come from the app process; Redis stores numbers only. |

## Consequences

- First **in-process scheduler** in the system (partition maintenance stays
  off-host — different concern, different failure profile). One extra background
  task per app process; the ZSET poll is O(log N + due) every 5 s.
- Scheduled sends reuse the entire live send path — no parallel delivery code.
- New `scheduled_messages` table is unpartitioned; expected to stay small
  (bounded by `SCHEDULED_MAX_PENDING_PER_USER`). Sent/cancelled/failed rows are
  **kept** for the user's history view; a later retention prune can DELETE
  `status != 0 AND updated_at < now() - 30d` (out of scope here).
- Media bytes are pinned (+1 ref) from schedule to fire, so a scheduled photo
  survives the sender purging the original message.
