# Linka — Root Router & Global Rulebook

Real-time messaging platform (WhatsApp/Telegram-style), designed for tens-of-billions-of-messages scale. Backend-first; **no real client app**, only a single-file HTML/Vue PoC (`poc/`) for manual testing.

**Stack (summary):** FastAPI (REST + WebSocket) · PostgreSQL 15 / SQLAlchemy 2.0 async / asyncpg · Redis 7 · PyJWT · Pydantic v2 · pytest/pytest-asyncio/httpx · MinIO (S3-compatible storage).

---

# CORE BEHAVIOR RULES (always in force)

1. **No Yapping:** Be extremely concise. Output only the necessary code or commands, with a 1-sentence explanation maximum. Skip pleasantries, apologies, and verbose explanations.
2. **The 3-Strike Rule (Anti-Loop):** If you hit an error on the exact same issue 3 times in a row, DO NOT attempt another fix. Stop, state that you are stuck in a loop, and ask the user for guidance.
3. **Code Standards:** Write ALL source code comments exclusively in English.
4. **Targeted Edits:** For small tasks, minor features, or bug fixes, NEVER rewrite or output the entire file. Use targeted diffs or provide ONLY the specific code blocks/functions that change.
5. **Frontend Display Rule:** NEVER display the raw `user_id` in the UI (chat lists, message bubbles, headers). ALWAYS use `phone_number` or, preferably, the resolved human-readable name from the local contacts dictionary. `user_id` is strictly for backend logic / API calls.
6. **No Autonomous Visual Testing:** NEVER use browser tools, Puppeteer, or screenshots to test frontend changes. Do NOT spin up local servers to visually verify the UI. The user tests manually and reports back.
7. **Architecture Decision Records (ADR):** Whenever we make a significant architectural, database schema, or infrastructure decision, you MUST proactively generate a new ADR file in `docs/adr/` BEFORE writing the code. Always review existing ADRs before proposing systemic changes.
9. **Token & Bottleneck Alerts:** If asked to read/analyze/edit a file over ~300 lines, or you spot a token-wasting workflow bottleneck, STOP. Alert the user about the specific file/bottleneck, explain the cost, suggest a split strategy, and wait for a decision.
10. **Ask before backend changes** (user's standing request). Flag security-relevant or destructive changes clearly instead of silently reverting them.
11. When generating or modifying mock data, do not attempt to update, patch, or edit existing data structures. Instead, completely discard the old mock data and generate a fresh, complete set of mock data from scratch based on the current requirements. Do not output diffs or partial updates for mock data—always output the full new mock data block
---

# CONTEXT ROUTING RULE (mandatory)

This file is a **router only**. It does NOT contain domain detail.

**Before working on ANY specific task, you MUST read the relevant sub-file(s) in `.claude_docs/` first.** Do not write or edit code in a domain until you have read that domain's file in the current session. If a task spans multiple domains, read all the relevant files.

---

# AUTO-MAINTENANCE RULE (mandatory)

`.claude_docs/` is the persistent external brain. You are **required** to keep it current:

- **Every time a major architectural decision, schema change, new service/module, new WS action, or new API endpoint is made or discovered**, proactively update the specific sub-file(s) in `.claude_docs/` in the same task — before concluding it.
- If new information does not fit any existing sub-file, **create a new sub-file** in `.claude_docs/` and add it to the Index below.
- Keep each sub-file focused and under ~300 lines; split further if it grows past that.
- For a significant architectural / schema / infrastructure decision, first add a numbered ADR in `docs/adr/` (Rule 7), then update the affected `.claude_docs/` sub-file(s), then write the code.
- Update this root file ONLY for: core behavior rules, the routing/maintenance rules, or the two Indexes.

---

# INDEX — `.claude_docs/`

| File | Contents |
|---|---|
| `.claude_docs/backend_services_and_api.md` | `modules/` + `infra/` + `realtime/` + `modules/messaging/` + `modules/chats/` layout & facade contract, `modules/settings/` (per-user settings), `routers/`, auth / OTP caveat, group membership & roles, system messages (incl. `role_changed` JSON pattern), leaving/removing members, profile-edit propagation, backend known-gaps, working conventions. |
| `.claude_docs/realtime_and_redis.md` | All Redis usage. Async send path (`realtime/fanout/`, streams, workers, sharding), routing layer (`chat_instances`, `instance_inbox`, heartbeats), `connection_manager` inbox task, presence (subscribe-on-demand, 1:1 only), typing/recording indicator, receipt-log async writes. |
| `.claude_docs/database_schema.md` | Models & CRUD, no-migrations rule, partitioning, Snowflake-id-as-string rule, watermark receipt model, detailed `message_receipt_log`, chat-list denormalization, unread count, reply-to, edit/delete/restore, scripts, DB-test-wipes-dev-DB warning. |
| `.claude_docs/storage_and_media.md` | Object storage design principle, MinIO/config, `modules/media/` (`client.py`, `media_service.py`, `errors.py`), media messages (kinds/caps/MIME whitelist incl. the 3-place `file=set()` sentinel), `Message` media columns, presigned-URL flow, user & group avatars. |
| `.claude_docs/deployment.md` | Single-host demo deploy: `Dockerfile`, `docker-compose.prod.yml`, `deploy/` (Caddyfile, postgres.prod.conf, env example, prod cron), first-boot/update steps, memory budget, demo compromises (open OTP stub, in-box MinIO). Runbook: `deploy/README.md`. |
| `.claude_docs/security_and_rate_limiting.md` | Transport hardening + rate limiting (Phase 1 / ADR 0012, all 8 steps done). Layering (Caddy per-IP vs app per-user in Redis), the rate-limiter engine (fixed + sliding window, `client_ip` helper, `RateLimited`), every limit's key/window/enforcement point (auth/OTP, WS frame + per-action, WS connection cap `ws:conns:` zset evict-oldest, REST history/upload-ticket/detail/list), WS close codes, config knobs, deferred items. |
| `.claude_docs/env_handoff.md` | **Transient handoff.** Tasks A (`.gitignore`) + B (CLAUDE.md ADR index) DONE. Remaining: ephemeral test DB in `conftest.py` (needs ADR 0032) + deferred bottleneck items 3 (CRUD→service) & 4 (test DI). Delete once drained. |
| `.claude_docs/frontend.md` | PoC structure (`poc/index.html` + `components/*.js` + `composables/*.js`), running it, syntax-check-after-edit rule, `$emit` chaining rule, `useWsRouter.js` live-event handling, optimistic send flow. |

# INDEX — `docs/adr/` (Architecture Decision Records)

Per Core Behavior Rule 7, review these before proposing any systemic change, and add a new numbered ADR before writing code for a significant architectural / schema / infrastructure decision.

Rows are one-liners; the ADR file holds the full rationale (this index is loaded every session). Sorted ascending.

| ADR | Title | Status |
|---|---|---|
| `0001-redis-pubsub-fanout-routing.md` | Redis pub/sub fan-out routing layer and queue workers | Accepted |
| `0002-user-settings-jsonb.md` | Per-user settings as a single extensible JSONB blob | Accepted |
| `0003-read-receipts-privacy.md` | Read-receipt privacy: asymmetric per-reader 1:1 mask, groups exempt | Accepted |
| `0004-chat-mute.md` | Per-user chat mute as `Participant.muted_until`; client owns durations | Accepted |
| `0005-time-partition-management.md` | Weekly `messages` / daily `message_receipt_log` partitions via a standalone Python script; DEFAULT safety net | Accepted |
| `0006-partition-maintenance-cron.md` | Partition maintenance via committed crontab + wrapper on exactly one host (no in-app scheduler) | Accepted |
| `0007-single-host-docker-compose-deploy.md` | Demo deploy: one 1 GB host, multi-stage `Dockerfile` + `docker-compose.prod.yml`, single Uvicorn, git-ignored `.env` | Accepted |
| `0008-aws-s3-object-storage-in-prod.md` | Live deploy uses real AWS S3 (not MinIO); MinIO is local-dev / CI only. Env-only switch, per ADR 0007 | Accepted |
| `0009-firebase-phone-auth.md` | Real phone verification via Firebase Phone Auth; server verifies the ID token against Google JWKS. `DEV_AUTH_WHITELIST` skips it | Accepted |
| `0010-content-addressed-media-dedup.md` | Upload-once media keyed by client `sha256` in a `media_blob` table; `x-amz-checksum-sha256` pinned into the presigned PUT; `ref_count`, no GC | Accepted |
| `0011-rust-snowflake-id-service.md` | Snowflake ID generation moved to a standalone Rust gRPC service under load; unary-only | Accepted |
| `0012-transport-hardening-and-rate-limiting.md` | Phase 1 comms security: Caddy owns TLS/headers/per-IP ceilings, app owns per-user limits in Redis. Sliding-window limiter, WS 5-conn cap | Accepted |
| `0013-chat-service-domain-split.md` | Split `chat_service.py` into a `modules/chats/` package with a thin re-export facade; mirrors `modules/messaging/` | Accepted |
| `0014-media-blur-placeholder.md` | Sender-computed ThumbHash on WS `send_message` → `Message.media_blur_hash`; instant blur, fetch bytes on tap. No server-side image processing | Accepted |
| `0015-lazy-avatar-blur-placeholder.md` | ThumbHash blur for avatars. **Superseded by ADR 0016** (blur unrecognisable at avatar size) | Superseded |
| `0016-avatar-inline-thumbnail-preview.md` | Replace avatar blur with a real ~64px JPEG `data:` URI (`profile_pic_preview`); uploader downscales via canvas; `_clean_preview` validates | Accepted |
| `0017-unique-usernames-and-user-search.md` | Unique lowercase `users.username`, auto-assigned; exact-match search only (anti-harvest); `GET /users/by-username`; grace hold | Accepted |
| `0018-drop-display-name-username-only-identity.md` | Remove `users.display_name` entirely; peers shown as `username || phone`. **Partially reversed by ADR 0024** | Accepted |
| `0019-config-package-split.md` | Split `config.py` into a `config/` package (8 domain sub-modules + `from .x import *` facade); `from config import X` unchanged | Accepted |
| `0020-client-side-message-forward.md` | "Forward" implemented entirely in the PoC frontend; re-sends via `send_message` per target; media reuses the blob via `media.key`. No backend change | Accepted |
| `0021-hard-delete-message-purge.md` | `purge_message` WS action (sender-only, must be soft-deleted first): nulls content/media, stamps `purged_at`, real S3 `delete_object` on last ref | Accepted |
| `0022-feature-based-module-layout.md` | Feature-based modular monolith: `infra/` + `realtime/` + `modules/<feature>/` + `api/` replace layer dirs. Pure `git mv` + import rewrite | Accepted |
| `0023-username-change-quota.md` | Username-change quota: 3 per rolling 14 days via a capped `users.username_change_log` JSONB ring; supersedes the ADR 0017 single-timestamp cooldown | Accepted |
| `0024-optional-display-name.md` | Reintroduce an optional, free-form, any-language `display_name` over the unique username; sanitised, never searchable. Partially reverses ADR 0018 | Accepted |
| `0025-foreground-presence-gate.md` | Foreground-only presence: WS `presence_active {active}`; `presence:{uid}` means foreground connections; typing/recording gated on window active | Accepted |
| `0026-client-side-e2e-encryption.md` | Client-side E2E for **text**: browser AES-256-GCM + ECDH P-256. Server stores opaque ciphertext. `messages.is_encrypted`/`enc_header`, `user_public_keys` table, key-bundle endpoints. **Superseded by ADR 0037** | Superseded |
| `0027-e2e-encrypted-message-edits.md` | Extends ADR 0026 to `edit_message`: optional `enc`; plaintext edit of an encrypted row is rejected (`EncryptionRequiredError`). No schema change. **Superseded by ADR 0037** | Superseded |
| `0028-per-user-storage-quota.md` | Per-user hard storage quota `STORAGE_QUOTA_BYTES` enforced at `upload-ticket` → HTTP 413. `users.storage_bytes_used`, counted per-ref. Refunded only on purge | Accepted |
| `0029-centralized-settings-accessor.md` | Additive flat `config.settings` accessor over the ADR 0019 sub-modules; read-only, live resolution. Old `from config import X` kept for `realtime/` + `scripts/` + the test-monkeypatched modules | Accepted |
| `0030-feature-local-api-schemas.md` | Move the 34 Pydantic models out of `api/schemas.py` into `modules/<feature>/schemas.py`; `api/schemas.py` keeps only `IdStr`. No re-export shim | Accepted |
| `0031-scheduled-messages.md` | Unpartitioned `scheduled_messages` table + Redis due-ZSET polled by an in-process worker firing via the existing async send path; REST API; `scheduled_write` bucket. No migration | Accepted |
| `0032-ephemeral-test-database.md` | Test suite creates/drops a throwaway `test_db_<uuid>` per session; seeded dev DB never touched. `conftest.py` rewrites `DATABASE_URL` before import | Accepted |
| `0033-rust-websocket-gateway.md` | Replace only the FastAPI `/ws` endpoint with a standalone Rust `ws_gateway` speaking the existing Redis contract unchanged; Cargo workspace shares one `target/` with `id_service`; built off-host. Plan: `RUST_WS_GATEWAY_PLAN.md` | Accepted |
| `0033-inject-limits-into-services-and-routers.md` | Per-feature frozen `AuthPolicy` / `MessagingLimits` / `ScheduledLimits` / `ChatLimits`; services take keyword-only `policy=`/`limits=`, routers expose FastAPI deps; tests use `dependency_overrides` not `monkeypatch` | Accepted |
| `0034-encrypted-chat-list-preview.md` | Denormalise the encrypted last message's `{ct, header}` onto `chats.last_message_enc` (JSONB, kept in lockstep with `last_message_preview` in `crud_message`); `ChatOut` exposes it; client decrypts it for the sidebar preview on load. **Superseded by ADR 0037** | Superseded |
| `0035-poc-chat-store-singleton.md` | PoC frontend: `useChatStore` promoted to a module-level singleton `LinkaChatStore` (single source of truth for chats/messages/unread/buffered-messages + pure helpers); `useChatStore(ctx)` kept as a back-compat shim so `useChats` still merges the same refs onto `ctx` for the ~31 un-migrated composables. `useWsRouter` → pure `wsEvent → LinkaChatStore` mapper (state via `store.*`, sibling behaviour still off `ctx`), owns zero refs. `MessageList` drops its `messages` prop → `inject('chatStore')`. Frontend-only, no behaviour change | Accepted |
| `0036-internal-ws-bootstrap-endpoint.md` | `/internal/*` router on the Python app for the Rust `ws_gateway`: `ws-bootstrap` (`{user_id, chat_ids}` on connect, JWT-verified, plan step 5a) + `presence-authorized` / `typing-allowed` (Step 6) + `message/{edit,delete,restore,purge}` (ADR 0038). Edge-blocked (`/internal*` 404s at Caddy), `reqwest` no-TLS. Shared `privacy.online` rule → `realtime/presence_authz.py` | Accepted |
| `0037-async-receipt-processing.md` | `mark_delivered`/`read`/`played` become fire-and-forget `XADD receipt_log_stream` on the WS path (Python + Rust gateway); the existing `receipt_log` worker (`+ modules/receipts/apply.py`) does the coarse watermark + ADR 0003 privacy gate + live receipt event. No `/internal/mark-receipt` (would bottleneck the 1 Python proc). Receipts now eventually-consistent; non-voice `mark_played` silently dropped by the worker | Accepted |
| `0038-remove-legacy-python-websocket-layer.md` | **Deleted** `realtime/{ws_router,connection_manager,ws_connection_registry}.py` + tests + `LEGACY_WS_ENABLED` — the Rust `ws_gateway` is the only `/ws` (live since 2026-09-10). `presence_service.py` kept (read side live; write side = spec for `presence.rs`), plus `realtime_service.py` / `internal_router.py` / `presence_authz.py`. Edit/delete/restore/purge WS actions relayed via `POST /internal/message/*` + `message_ops.rs` | Accepted |
| `0039-drop-e2ee-server-side-cloud-model.md` | Permanently drop client-side E2EE (reverses ADR 0026/0027/0034) for a Telegram-style cloud model: plaintext `messages.content`, TLS/WSS only, server has full read access (enables future server-side FTS/vector search). Drops `messages.is_encrypted`/`enc_header`, `chats.last_message_enc`, `user_public_keys`, the public-key/key-bundle endpoints, `EncryptionRequiredError`, `poc/composables/useE2E.js`; `enc` stripped from Rust `ws_gateway` frames | Accepted |

---

# Running locally

```bash
docker compose up -d                                    # test_db (5433), test_redis (6380), test_minio (9100 API / 9101 console)
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" python3 -m scripts.init_db
python3 -m scripts.init_storage
DATABASE_URL="..." REDIS_URL="redis://localhost:6380/0" uvicorn main:app --reload

# /ws is the standalone Rust ws_gateway (ADR 0033/0038) - the Python app no
# longer serves /ws. Run it alongside uvicorn for the PoC to connect:
JWT_SECRET="dev-secret-change-me" REDIS_URL="redis://localhost:6380/0" \
  APP_INTERNAL_URL="http://localhost:8000" WS_GATEWAY_BIND="127.0.0.1:8081" \
  CORS_ALLOW_ORIGINS="*" cargo run -p ws_gateway
```

Open `poc/index.html` directly. OTP codes print to the server console — no real SMS/FCM.
In the browser console once: `localStorage.setItem('linka_ws_base','ws://localhost:8081')` so the PoC's WebSocket points at the gateway instead of `<apiBase>/ws` (prod uses Caddy to proxy `/ws` same-origin). `JWT_SECRET` must equal the app's `JWT_SECRET_KEY`.

**Testing:** the suite runs against an ephemeral per-run database (ADR 0032, `tests/README.md`) — the seeded dev DB and MinIO are left untouched. `DATABASE_URL` need not be set for tests.

# Reference plan files (root)

`FANOUT_REWRITE_PLAN.md` (steps 1–4 all landed), `poc/composables/REFACTOR_PLAN.md` (complete).
