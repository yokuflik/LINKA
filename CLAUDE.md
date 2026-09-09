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
| `.claude_docs/frontend.md` | PoC structure (`poc/index.html` + `components/*.js` + `composables/*.js`), running it, syntax-check-after-edit rule, `$emit` chaining rule, `useWsRouter.js` live-event handling, optimistic send flow. |

# INDEX — `docs/adr/` (Architecture Decision Records)

Per Core Behavior Rule 7, review these before proposing any systemic change, and add a new numbered ADR before writing code for a significant architectural / schema / infrastructure decision.

| ADR | Title | Status |
|---|---|---|
| `docs/adr/0001-redis-pubsub-fanout-routing.md` | Redis pub/sub fan-out routing layer and queue workers | Accepted |
| `docs/adr/0002-user-settings-jsonb.md` | Per-user settings as a single extensible JSONB blob | Accepted |
| `docs/adr/0003-read-receipts-privacy.md` | Read-receipts (blue-tick) privacy: asymmetric per-reader 1:1 mask, groups exempt | Accepted |
| `docs/adr/0004-chat-mute.md` | Per-user chat mute as `Participant.muted_until`; client owns durations, server only suppresses offline push | Accepted |
| `docs/adr/0005-time-partition-management.md` | Weekly `messages` / daily `message_receipt_log` partitions, managed by a standalone Python script (not `pg_partman`); DEFAULT kept as safety net; online DEFAULT migration | Accepted |
| `docs/adr/0006-partition-maintenance-cron.md` | Partition-maintenance scheduling: committed `deploy/partition-maintenance.crontab` + `scripts/partition_maintenance.sh` wrapper, run on exactly one host (no in-app scheduler) | Accepted |
| `docs/adr/0007-single-host-docker-compose-deploy.md` | Demo deploy: one 1 GB host, `Dockerfile` (multi-stage) + `docker-compose.prod.yml` (app + Caddy + db + redis + minio), single Uvicorn process, git-ignored `.env` | Accepted |
| `docs/adr/0009-firebase-phone-auth.md` | Real phone verification via Firebase Phone Auth (client-side SMS + reCAPTCHA); server verifies the ID token manually against Google JWKS (`POST /auth/firebase/verify`, no `firebase-admin`). Open OTP stub closed; `DEV_AUTH_WHITELIST` (`1`–`5`) skips verification. Frontend country-code picker + loose E.164 validator | Accepted |
| `docs/adr/0010-content-addressed-media-dedup.md` | Upload-once media: client sends `sha256`, backend keys objects by hash in a new `media_blob` table; a known hash skips the upload entirely. `x-amz-checksum-sha256` pinned into the presigned PUT. `ref_count` tracked, no GC yet | Accepted |
| `docs/adr/0012-transport-hardening-and-rate-limiting.md` | Phase 1 comms security: Caddy owns TLS + security headers + coarse per-IP ceilings, app owns per-user/identity limits in Redis (never in-process). Sliding-window limiter, `client_ip` proxy-trust helper, WS 5-connection cap (evict oldest), HSTS/CSP/TrustedHost, CORS fix, `/ws` `Origin` check. Content encryption + semantic search deferred. Plan: `COMMS_SECURITY_PLAN.md` | Accepted |
| `docs/adr/0013-chat-service-domain-split.md` | Split `chat_service.py` (534 lines) into a `modules/chats/` package (errors/common/notifications/creation/listing/preferences/group_details/membership) with `chat_service.py` kept as a thin re-export facade; mirrors the `message_service` → `modules/messaging/` pattern. Pure code move, monkeypatch surface preserved | Accepted |
| `docs/adr/0014-media-blur-placeholder.md` | Lazy media loading: sender's browser computes a ThumbHash (base64, ≤64 chars) of an image / video first frame, carried on WS `send_message` `media.blur_hash` → nullable `Message.media_blur_hash` + `MediaBlob.blur_hash` (backfilled on dedup). Client renders the blur instantly on chat open, fetches real bytes only on tap. No server-side image processing. Plan: `MEDIA_BLUR_PLACEHOLDER_PLAN.md` | Accepted |
| `docs/adr/0017-unique-usernames-and-user-search.md` | Unique lowercase `users.username` (`^[a-z][a-z0-9_]{2,31}$`, plain unique btree index, reason-coded validation), auto-assigned to every new account by `user_service.generate_free_username` (login response carries `is_new_user`); signup toggle + OTP `intent` check removed. Change guarded by `username_changed_at` cooldown (14d) + `reserved_usernames` grace hold (14d). `GET /users/username-available` (advisory, `username_check` bucket). Search is **exact-match only** — no LIKE/prefix/trigram (anti-harvest). `GET /users/by-username` (exact, returns `UserOut`, `list_read` bucket) added for the PoC New-chat modal; `PublicUserOut` (phone-less) still deferred. `UserOut` + `profile_updated` carry `username` | Accepted |
| `docs/adr/0015-lazy-avatar-blur-placeholder.md` | Same as 0014 for avatars: uploader's browser computes a ThumbHash of the picked image, sent in the avatar-commit body → nullable `User.profile_pic_blur_hash` / `Chat.profile_pic_blur_hash`. Every `<Avatar>` renders blur-only. **Superseded by ADR 0016** (blur unrecognisable at avatar size). | Superseded |
| `docs/adr/0021-hard-delete-message-purge.md` | Hard "delete forever" (`purge_message` WS action): sender-only, message must already be soft-deleted → nulls content/media, stamps `messages.purged_at`, blocks restore; media blob dereffed → real S3 `delete_object` + blob-row delete on last ref (first object-GC path). Fans out `message_purged`; all clients blank the bubble. No migration (`ADD COLUMN IF NOT EXISTS purged_at`). | Accepted |
| `docs/adr/0023-username-change-quota.md` | Username-change quota: up to 3 changes per rolling 14 days (env `USERNAME_CHANGE_MAX_PER_WINDOW` / `USERNAME_CHANGE_WINDOW_DAYS`), tracked in a capped `users.username_change_log` JSONB timestamp ring; `_cooldown_active` → `_change_quota_exceeded`; `reason` code stays `cooldown`; supersedes the ADR 0017 single-timestamp cooldown. No migration (`ADD COLUMN IF NOT EXISTS`); existing users get 3 fresh changes. | Accepted |
| `docs/adr/0018-drop-display-name-username-only-identity.md` | Remove `users.display_name` entirely (model / CRUD / `UserOut` / `UserProfileUpdateIn` / `profile_updated` payload); no migration. Peers shown as `username || phone_number` everywhere (PoC + system-message text). `about_text` kept at all levels (user + group). Mock seed regenerated as `(phone, username)` pairs. | Accepted |
| `docs/adr/0016-avatar-inline-thumbnail-preview.md` | Replace the avatar ThumbHash blur with a real ~64px JPEG `data:` URI (`User/Chat.profile_pic_preview`, renamed column, mock data wiped — no migration). Uploader's browser downscales via `canvas.toDataURL`; `avatar_service._clean_preview` validates `data:image/` + len ≤ 8192. `<Avatar>` renders it directly (no blur); tap still opens the full-res lightbox. Message-media blur (ADR 0014) unchanged. | Accepted |
| `docs/adr/0020-client-side-message-forward.md` | "Forward" is implemented entirely in the PoC frontend (`useForward.js` + `ForwardModal.js`): a picked-target multi-select (private + groups, client-side substring filter) plus an exact username/phone "People" hit. `confirmForward` re-sends the message via `send_message` per target (paced ~500ms, no outbox); media reuses the original object by recovering its storage key from the presigned `media_url` and passing `media.key` (backend re-refs the `media_blob`, ADR 0010 — no re-upload). No backend change, no "Forwarded" label (needs a field). Search debounce cut 3s/2s → 1.5s. | Accepted |
| `docs/adr/0022-feature-based-module-layout.md` | Feature-based modular monolith: layer dirs (`services/`, `routers/`, `database/`, `utils/`) replaced by `infra/` (db/redis/ids/ratelimit), `realtime/` (connection_manager, realtime_service, ws_connection_registry, presence, notification, `ws_router`, `fanout/`), `modules/<feature>/` (auth, users, settings, chats, messaging, receipts, media — each owns `models.py`/`crud.py`/service/`router.py`), `api/` (`dependencies.py`, `schemas.py`). Imports still repo-root (`main:app` unchanged). Pure `git mv` + AST import rewrite; `chats`/`messaging` re-export facades (`service.py`) kept. Dockerfile/compose/deploy untouched. | Accepted |
| `docs/adr/0019-config-package-split.md` | Split `config.py` (~506 lines, ~139 settings) into a `config/` package: `app_settings` / `auth_settings` / `redis_settings` / `security_settings` (transport hardening + all WS/REST rate limits) / `username_settings` / `storage_settings` / `messaging_settings` (send + fan-out streams, routing TTLs, receipt log) / `partition_settings`. `config/__init__.py` is a `from .<sub> import *` facade (each sub-module has explicit `__all__`), so `from config import X` and `import config` / `config.X` are unchanged; `config.py` deleted (no coexistence). Pure code move. `tests/test_config.py` reloads `config.app_settings` (a package reload returns cached sub-modules). | Accepted |

---

# Running locally

```bash
docker compose up -d                                    # test_db (5433), test_redis (6380), test_minio (9100 API / 9101 console)
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" python3 -m scripts.init_db
python3 -m scripts.init_storage
DATABASE_URL="..." REDIS_URL="redis://localhost:6380/0" uvicorn main:app --reload
```

Open `poc/index.html` directly. OTP codes print to the server console — no real SMS/FCM.

**Testing caveat:** any DB-backed test wipes the dev DB (`drop_all` on teardown). Dump first or re-run `init_db` + `seed_mock_data`. MinIO is unaffected. When manually verifying against dev Postgres, leave the schema created (not dropped).

# Reference plan files (root)

`FANOUT_REWRITE_PLAN.md` (steps 1–4 all landed), `poc/composables/REFACTOR_PLAN.md` (complete).
