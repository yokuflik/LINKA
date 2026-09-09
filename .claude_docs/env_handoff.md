# Environment & Follow-up Tasks — Handoff

Written 2026-09-09 after the ADR 0029 (config `settings` accessor) + ADR 0030
(feature-local API schemas) refactor. Those two are **done and merged into the
working tree** (454 tests pass; the 1 failing `test_message_service.py` fan-out
test is a **pre-existing** order-dependent pollution bug on clean `main`, not
caused by this work).

This file lists what a fresh session should pick up next. Nothing here has been
started. Read `CLAUDE.md` + the routing rule first; add an ADR before the schema
/ infra changes (tasks C and D).

---

## Context you need

- **The Realtime/WebSocket layer is being rewritten in Rust.** Do **not** touch
  `realtime/ws_router.py`, `realtime/` in general, WebSocket tests
  (`tests/realtime/**`), or WS-specific config. The config accessor work was
  deliberately kept out of `realtime/` for this reason.
- Running any DB-backed test drops+recreates the dev DB. Re-run
  `python3 -m scripts.init_db` then `python3 -m scripts.seed_mock_data`
  afterwards (env: `DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:5433/test_db`,
  `REDIS_URL=redis://localhost:6380/0`).

---

## A. Fix `.gitignore` (the `*.md` blanket ignore)  — ✅ DONE (2026-09-09)

`*.md` line removed; targeted `/scratch/` + `*.local.md` + `id_service/target/`
added instead. Files staged for a `chore: stop gitignoring docs/ADRs` commit
(user commits). **B also done**: ADR index in `CLAUDE.md` collapsed to
one-liners, sorted; `0026-scheduled-messages.md` renumbered to **0031** (header
+ all non-`realtime/` code comments + `.claude_docs/` + plan updated — residual
`ADR 0026` comment in `realtime/fanout/scheduled_worker.py` left for the Rust
rewrite); phantom **ADR 0008** written (real AWS S3 in prod); rows **0029/0030/
0031** added. Original task text kept below for the record.

---

## A. Fix `.gitignore` (the `*.md` blanket ignore)  — infra, no ADR needed

`.gitignore` currently contains a bare `*.md` line (under "IDEs & Editors").
Effect: `.claude_docs/**` and `docs/adr/**` are gitignored; only ~14 `.md`
files are tracked (force-added). New ADRs and doc updates the workflow *requires*
are invisible to `git status` and never committed.

**Do:**
1. Remove the `*.md` line.
2. Add targeted ignores instead — e.g.:
   ```
   # transient scratch notes only
   /scratch/
   *.local.md
   ```
3. `git add .claude_docs/ docs/adr/ *.md` and review what comes in. Expect
   ~25 previously-untracked ADRs and ~4 `.claude_docs/` sub-files
   (`realtime_and_redis.md`, `database_schema.md`, `storage_and_media.md`, …).
4. Sanity-check nothing secret is caught (there shouldn't be — `.env*` has its
   own rules above).
5. Commit as a standalone change: `chore: stop gitignoring docs/ADRs`.

---

## B. Clean up the `CLAUDE.md` ADR index  — docs, no ADR needed

Problems in the "INDEX — docs/adr/" table:
- **Duplicate number:** `0026-client-side-e2e-encryption.md` **and**
  `0026-scheduled-messages.md` both exist on disk. Renumber the scheduled-
  messages one to **0031** (0029 + 0030 are now taken by this refactor);
  update its filename, its `# ADR NNNN` header, and every reference
  (`grep -rn "ADR 0026" .` — the scheduled-messages ones vs the E2E ones) and
  the `.claude_docs/backend_services_and_api.md` "Scheduled messages (ADR 0026)"
  heading.
- **Phantom ADR:** the memory / docs reference "ADR 0008" (S3 / "no MinIO")
  but `docs/adr/0008-*.md` does not exist. Either write it or drop the
  references.
- **Rows are unsorted** (…0024, 0016, 0020, 0022, 0025, 0026, 0027, 0028,
  0026, 0019). Sort ascending.
- Each row is a dense 3–6 line paragraph duplicating the ADR body. Consider
  collapsing to `| ADR | Title | Status |` and letting the ADR file hold the
  rationale — this index is loaded every session.
- Add the two new rows: **0029** (centralized `settings` accessor) and
  **0030** (feature-local API schemas).

---

## C. Ephemeral test database in `tests/conftest.py`  — ✅ DONE (2026-09-09)

ADR 0032 written. `conftest.py` derives `test_db_<uuid4 hex>` from the fixed
server coordinate, rewrites `os.environ["DATABASE_URL"]` before any project
import (so `infra.db.connection` binds to it), and a session-scoped autouse
`_ephemeral_database` fixture `CREATE DATABASE` / `DROP DATABASE ... WITH
(FORCE)` via an AUTOCOMMIT engine on the server's `postgres` db.
`session_factory` unchanged in shape (`.kw["bind"]` still works). Runbook:
`tests/README.md`. CLAUDE.md + `database_schema.md` testing notes updated.
Original task text kept below for the record.

---

## C (original). Ephemeral test database in `tests/conftest.py`  — needs a short ADR

Today `tests/conftest.py::session_factory` runs `Base.metadata.create_all` then
`drop_all` against `TEST_DATABASE_URL`, which is the **same** database a
developer uses for manual PoC testing (`test_db` on `localhost:5433`). Every
test run wipes it.

**Target:** each test session creates and drops its own throwaway database (or
Postgres schema) so the dev DB is never touched.

**Approach (write it up as ADR 0032 first):**
- Session-scoped fixture: connect to the Postgres server (not a specific DB),
  `CREATE DATABASE test_db_<uuid4 hex>` (or `CREATE SCHEMA`), point the engine
  at it, `DROP DATABASE ... WITH (FORCE)` on teardown.
- `messages` / `message_receipt_log` are RANGE-partitioned — keep the existing
  `CREATE TABLE ... PARTITION OF ... DEFAULT` step after `create_all`.
- `_reset_shared_singletons_after_every_test` and `redis_db` are unaffected
  (Redis already uses a dedicated flush-per-test DB).
- Keep `TEST_DATABASE_URL` as the *server* coordinate; derive the per-run DB
  name from it.
- Watch the `os.environ.setdefault("DATABASE_URL", ...)` at the top of
  `conftest.py` — several modules read it once at import; the fixture must set
  the engine URL, not rely on re-import.
- CI: a single `createdb`-capable role is enough; document it in
  `deploy/README.md` or a new `tests/README.md`.

---

## D. Deferred refactor items 3 & 4 (from the original bottleneck analysis)

Not started. Each needs its own ADR before code.

### D1. Extract business logic from CRUD into the service layer
- `modules/messaging/crud.py` (448 lines) and
  `modules/chats/crud/crud_participant.py` (416 lines) mix parameterized SQL
  with business rules (receipt-privacy masking, media validation, role checks,
  chat-list denormalization).
- Target: `crud/*` = SQL only, returns rows/models, no branching on rules;
  `*/service` (or the split submodules) own all rules.
- `crud_participant.py` also wants splitting by concern
  (reads / writes / roles).
- Behavioral-risk change — do it with the ephemeral test DB (task C) in place
  so iteration is cheap.

### D2. Remove monkeypatching via DI  — ✅ DONE (2026-09-09)

ADR 0033. Per-feature frozen dataclasses: `modules/auth/limits.py::AuthPolicy`,
`modules/messaging/limits.py::{MessagingLimits,ScheduledLimits}`,
`modules/chats/limits.py::ChatLimits`. Service fns take a keyword-only
`policy=` / `limits=` (default = the module's `DEFAULT_*`); routers expose
`get_auth_policy` / `get_messaging_limits` / `get_scheduled_limits` /
`get_chat_limits` as FastAPI deps. Route tests use
`app.dependency_overrides` (+ an autouse `_clear_dependency_overrides`
fixture); service-unit tests pass `AuthPolicy(...)` / `MessagingLimits(...)`
directly. `monkeypatch` remains only for non-config collaborator stubs
(`notification_service.send_push`, `chat_service.realtime_service.publish_event`).
`realtime/` callers pass nothing and get the defaults (untouched, Rust rewrite).
347 pass; the 2 `test_message_service.py` fan-out failures are the known
pre-existing pollution (identical set on clean `main`).

### D2b. Finish the ADR 0029 migration (Task 4)  — ✅ DONE (2026-09-09)

Last flat `from config import NAME` / `import config` consumers under
`modules/` + `tests/` moved to `settings.X`: `modules/auth/{service,firebase}.py`,
`modules/auth/limits.py`, `modules/messaging/limits.py`, `modules/chats/limits.py`,
`modules/messaging/{router,scheduled_service}.py`, `modules/users/service.py`,
`tests/modules/users/test_user_service.py`. `tests/modules/auth/test_firebase_auth.py`
now patches `config.auth_settings.FIREBASE_PROJECT_ID` (accessor resolves live)
instead of a module global. `config/__init__.py` flat re-exports shrunk from
`from .<sub> import *` to an explicit 64-name allow-list — only what `realtime/`
+ `scripts/` + `infra/` still consume. 452 pass; the 3 remaining full-run
failures (`test_message_service` fan-out x2, `test_send_queue` duplicate) are
pre-existing Redis-timing flakes — all pass in isolation and on clean `main`.

Original task text kept below for the record.

### D2 (original). Remove monkeypatching in `tests/api/test_rest_api.py` via DI
- 26 `monkeypatch` calls; the pattern is `monkeypatch.setattr(<consumer
  module>, "<CONFIG_NAME>", value)` — it works only because the consumer does
  `from config import CONFIG_NAME` binding the name locally.
- **Because of this, ADR 0029 deliberately did NOT migrate these seven modules
  to `settings.X`** (migrating them would break the patches):
  `modules/auth/service.py`, `modules/auth/router.py`,
  `modules/messaging/router.py`, `modules/messaging/scheduled_service.py`, and
  the `modules/chats/service.py` + `modules/messaging/service.py` re-export
  facades (which keep `from config import MAX_INITIAL_GROUP_MEMBERS` /
  `MAX_MESSAGE_CONTENT_LENGTH` / `RECEIPT_NAMED_LIST_MAX_MEMBERS` alive on
  purpose — submodules read them back off the facade at call time).
- Target: pass a settings object / individual limits as function or dependency
  parameters; tests use `app.dependency_overrides` and constructor args instead
  of `monkeypatch.setattr`.
- **When D2 lands, finish the ADR 0029 migration**: convert those seven modules
  to `settings.X`, then the flat `from config import X` re-export facade in
  `config/__init__.py` can be dropped for everything except `realtime/` (Rust
  rewrite) and `scripts/`.

---

## State after this session (for reference)

**ADR 0029 — `config.settings`:**
- New `config/_accessor.py`: `settings` object, live attribute resolution off
  the 8 sub-modules, read-only. Exported from `config/__init__.py`. Old
  `from config import X` / `import config; config.X` untouched.
- Migrated to `settings.X`: `main.py`, `modules/users/{router,crud,avatar_service}.py`,
  `modules/chats/router.py`, `modules/messaging/{crud,media_validation,receipts}.py`,
  `modules/media/{client,media_service}.py`, `modules/receipts/{receipt_log,worker}.py`.
- `modules/media/media_service.py` now **explicitly** re-exports
  `S3_BUCKET_AVATARS` / `S3_BUCKET_MEDIA` (they are default `bucket=` args and
  callers/tests use `media_service.S3_BUCKET_*`) — documented, intentional.
- `modules/users/service.py` left as `import config; config.X` (already the
  good pattern, no giant block).

**ADR 0030 — feature-local schemas:**
- `api/schemas.py` reduced to just `IdStr` (+ `__all__`). No re-export shim
  (would cycle with the feature schemas).
- New: `modules/{auth,users,media,chats,messaging}/schemas.py`. Import edges:
  auth→users, chats→users, chats/users/messaging→media, all→`api.schemas.IdStr`.
  Acyclic (`media_service` imports no `schemas.py`).
- The 4 `modules/*/router.py` updated to import from the new locations.
- `tests/` never imported `api.schemas`, so no test import changes were needed.
