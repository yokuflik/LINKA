# ADR 0022 — Feature-based module layout (modular monolith)

Status: Proposed
Date: 2026-09-09

## Context

The tree grew layer-first (`services/`, `routers/`, `database/models/`, `database/crud/`)
with inconsistent granularity inside `services/`: flat `x_service.py` files
(`auth_service`, `user_service`, `presence_service`, `realtime_service`,
`avatar_service`, `notification_service`, `rate_limit_service`,
`connection_manager`) sit next to domain packages (`chats/`, `messaging/`,
`storage/`, `settings/`, `receipts/`, `fanout/`). Touching one feature means
editing 4–5 directories, `database/` conflates the engine/session with models and
CRUD, `utils/` mixes generated protobuf stubs with hand-written code, and
`routers/websocket.py` (540 lines) is a full dispatch engine mislabeled as a
"thin router".

## Decision

Reorganize into a **feature-based modular monolith**, imports still rooted at the
repo directory (no `src/` package, `uvicorn main:app` unchanged). Pure file
relocation via `git mv` + mechanical import rewrite — **no behavior change**.

Top-level packages:

- `infra/` — infrastructure only, no domain logic: `db/` (declarative `Base`,
  engine/session), `redis/` (client), `ids/` (Snowflake client + local generator +
  isolated `_generated/` protobuf stubs), `ratelimit/` (the limiter engine).
  Named `infra/`, **not** `platform/`, because `platform` shadows a stdlib module.
- `realtime/` — the cross-cutting real-time engine (not a single feature):
  `connection_manager`, `realtime_service` (redis pub/sub), `ws_connection_registry`,
  `presence_service`, `notification_service` (push stub), `ws_router` (← the old
  `routers/websocket.py`), `fanout/` (internals unchanged).
- `modules/<feature>/` — one folder per feature owning its `models.py`, `crud.py`,
  service logic, and `router.py`: `auth`, `users`, `settings`, `chats`,
  `messaging`, `receipts`, `media` (← `services/storage/`).
- `api/` — `dependencies.py` (shared FastAPI deps) and `schemas.py` (the whole of
  `routers/schemas.py`, moved wholesale — **not** split in this ADR).
- Unchanged: `config/`, `scripts/`, `main.py`, `tests/` (mirrors the new tree),
  `poc/`, `deploy/`, `docs/`, `.claude_docs/`, `proto/`, `id_service/`.

### Naming rule (going forward)

Every feature is a package under `modules/`. No `*_service.py` at any top level.
A feature's public surface is `modules/<x>/service.py` (or its `__init__`).

### Explicitly out of scope (separate follow-ups)

- Removing the `chats` / `messaging` re-export **facades** (`service.py`) and the
  module-global monkeypatch test style that forces them — kept verbatim here.
- Splitting `api/schemas.py` (368 lines) into per-module `schemas.py`.
- Extracting the exc→HTTP mapping out of `main.py`.
- Splitting the 6 files >300 lines.
- Converting to an installable `src/linka/` package (ADR-worthy on its own).

### Shared code

`crud_user` is used by both `auth` and `users`; it lands in `modules/users/crud.py`
and `modules/auth/` imports from there (username lookups are user lookups).
Model registration for `Base.metadata.create_all()` (currently explicit submodule
imports in `tests/conftest.py` and `scripts/init_db.py`) is updated to point at
the new `modules/*/models.py` paths — import-list edit, no aggregator module added.

## Consequences

- ~90 files move (history preserved via per-file `git mv`); ~110 files get
  mechanical import edits in one follow-up commit.
- Non-Python touch points: `tests/conftest.py`, `scripts/*` (incl. `gen_proto.sh`
  output dir + stub-import patch line), and 6 `.claude_docs/` sub-files.
- **Not touched:** `Dockerfile`, `docker-compose*.yml`, `deploy/`, the
  `main:app` entrypoint, `poc/`.
- Verification: full `pytest` green + `uvicorn main:app` boots + `scripts.init_db`
  runs, before and after.
