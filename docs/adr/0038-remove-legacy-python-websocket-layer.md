# ADR 0038 — Remove the legacy Python WebSocket layer

Status: **Proposed** (blocked only on the Step 8 canary; the edit/delete gap below is fixed)
Date: 2026-09-10

## Context

ADR 0033 replaced the FastAPI `/ws` endpoint with the standalone Rust
`ws_gateway`. Plan steps 1–8 shipped the gateway (routing, fan-in, presence,
typing, async receipts per ADR 0037); the Python `/ws` is kept behind
`LEGACY_WS_ENABLED` / Caddy `/ws-legacy` as the canary rollback path.

Step 9 of the plan is to delete the dead Python layer once the gateway is
proven: `realtime/ws_router.py`, `realtime/connection_manager.py`,
`realtime/ws_connection_registry.py` and their tests, plus `LEGACY_WS_ENABLED`.

## The edit/delete gap (found during Step 9, now fixed — option 1)

`edit_message` / `delete_message` / `restore_message` / `purge_message` are
WS-only (`_HANDLERS` in `realtime/ws_router.py`, no REST) and the PoC uses all
four. The Rust `ClientFrame` enum had no variant for them.

**Fixed by ADR-0036-style internal endpoints (option 1):**
`POST /internal/message/{edit,delete,restore,purge}` (`realtime/internal_router.py`)
wrap `message_service.edit_message` / `delete_message` / `restore_message` /
`purge_message` — which already publish their own `message_edited` /
`message_deleted` / `message_restored` / `message_purged` fan-out. New
`ClientFrame::{EditMessage,DeleteMessage,RestoreMessage,PurgeMessage}` +
`crates/ws_gateway/src/message_ops.rs` relay each: `ws_edit` rate bucket → POST →
ack on 2xx, `forbidden` on 403 (`NotAParticipantError`), `bad_request` on 400
(`EncryptionRequiredError` / `MessageTooLongError`), `internal_error` otherwise.
Volume is low (edits/deletes are rare), so the per-frame HTTP hop that ruled out
`/internal/mark-receipt` is fine here. Covered by
`tests/realtime/test_internal_router.py`.

- **Rejected — move them to REST** (`PATCH /messages/{id}` etc. + change the
  PoC): cleaner long-term but a frontend change + new public API surface, for no
  gain over option 1 now.

## Decision (pending the canary)

Once the gateway has been canaried in production and is stable:

- Delete `realtime/{ws_router,connection_manager,ws_connection_registry}.py` +
  their test modules + `config.LEGACY_WS_ENABLED` + the `main.py` conditional
  mount.
- **Keep** `realtime/presence_service.py` (read side live for the fan-out
  worker's push-vs-live choice; write side stays as the executable spec for
  `crates/ws_gateway/src/presence.rs`, exercised by
  `tests/realtime/test_presence_service.py`), `realtime/realtime_service.py`
  (pub/sub used by every worker), `realtime/internal_router.py` +
  `realtime/presence_authz.py` (the `/internal/*` seam, now covered by
  `tests/realtime/test_internal_router.py`).
- Caddy: point `/ws` **and** `/ws-legacy` at `ws_gateway:8081`, drop
  `/ws-legacy` a release later.

## Consequences (when unblocked)

- The single Python process stops holding live sockets — memory + GIL freed.
- One implementation of the wire contract. `crates/common` golden-JSON +
  `test_presence_service.py` + `test_internal_router.py` pin Python↔Rust
  compatibility.
- Rollback to Python WS is no longer possible without a revert.

## Already landed on the way here

- `POST /internal/message/{edit,delete,restore,purge}` + gateway
  `message_ops.rs` (the edit/delete gap fix above).
- `tests/realtime/test_internal_router.py` — coverage for the ws-bootstrap /
  presence-authorized / typing-allowed / message-`*` rules that the
  (to-be-deleted) `test_ws_router.py` held.
- The Python `/ws` deletion was staged once and rolled back; this ADR stays
  Proposed until the canary.
