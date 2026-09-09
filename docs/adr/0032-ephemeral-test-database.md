# 32. Ephemeral per-run test database

Date: 2026-09-09

## Status

Accepted

## Context

`tests/conftest.py` pointed both `session_factory` and (via the
`DATABASE_URL` env var it sets at import) `infra.db.connection.engine` at
`test_db` on `localhost:5433` — the **same** database a developer seeds with
`scripts.init_db` + `scripts.seed_mock_data` for manual PoC testing.

`session_factory` runs `Base.metadata.create_all` on setup and
`drop_all` on teardown for every function-scoped test. Net effect: running
any DB-backed test silently wipes the developer's seeded database, forcing a
re-init + re-seed afterwards. This is called out as a hazard in `CLAUDE.md`
and the `.claude_docs/` sub-files.

## Decision

Each test **session** creates its own throwaway Postgres database and drops
it on the way out. The developer database is never opened by the test suite.

- `TEST_DATABASE_URL` stays the **server** coordinate (same host / user /
  password / port). The per-run database name is derived from it:
  `test_db` → `test_db_<uuid4 hex>`.
- `conftest.py` rewrites `os.environ["DATABASE_URL"]` to the ephemeral URL
  **at the top of the module, before any project import**, so
  `infra.db.connection` (which reads `DATABASE_URL` once, at import) builds
  its engine against the ephemeral database. The REST-API tests, which go
  through `main.app` → `infra.db.connection`, therefore also hit the
  ephemeral database.
- A session-scoped, autouse fixture (`_ephemeral_database`) connects to the
  server's default `postgres` database with `AUTOCOMMIT`, runs
  `CREATE DATABASE test_db_<hex>` on setup and
  `DROP DATABASE test_db_<hex> WITH (FORCE)` on teardown.
- `session_factory` is unchanged in shape (still function-scoped
  `create_all` / `drop_all`, still yields a `sessionmaker` bound to an
  engine — `session_factory.kw["bind"]` keeps working for
  `tests/scripts/test_manage_partitions.py`). It just points at the
  ephemeral database now.
- The existing `CREATE TABLE ... PARTITION OF ... DEFAULT` step after
  `create_all` is kept (the `messages` / `message_receipt_log` RANGE
  partition parents still need a DEFAULT child).
- Redis is untouched — `redis_db` already flushes a dedicated test DB.

## Consequences

- Running the test suite no longer disturbs the developer's seeded DB. The
  re-init / re-seed dance in `CLAUDE.md` and `.claude_docs/` can be dropped
  once this lands (done in the same change).
- CI / local dev needs a Postgres role allowed to `CREATE DATABASE`
  (`test_user` in the compose file already is). Documented in
  `tests/README.md`.
- One extra `CREATE DATABASE` / `DROP DATABASE` per test session (~ms). The
  per-function `create_all` / `drop_all` cost is unchanged.
- A hard-killed session (SIGKILL) can leak a `test_db_<hex>` database. It is
  harmless (next run makes its own) and greppable by prefix; `tests/README.md`
  notes the one-liner to drop stragglers.
