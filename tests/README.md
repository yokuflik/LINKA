# Running the tests

```bash
docker compose up -d          # test_db (5433), test_redis (6380), test_minio (9100)
REDIS_URL="redis://localhost:6380/0" python3 -m pytest
```

`DATABASE_URL` does **not** need to be set — `tests/conftest.py` derives an
ephemeral per-run database from the fixed server coordinate
(`localhost:5433`, `test_user`/`test_password`).

## Ephemeral test database (ADR 0032)

Each `pytest` session creates a throwaway `test_db_<uuid hex>` database,
runs the whole suite against it, and drops it on teardown. **The suite never
opens the developer's seeded `test_db`** — you can seed it once
(`python3 -m scripts.init_db && python3 -m scripts.seed_mock_data`) and keep
it for manual PoC testing across as many test runs as you like.

Requirements: the Postgres role must be allowed to `CREATE DATABASE`
(`test_user` in `docker-compose.yml` already is).

### Stragglers

A `kill -9` mid-session can leak a `test_db_<hex>` database. Harmless, but to
clean up:

```bash
PGPASSWORD=test_password psql -h localhost -p 5433 -U test_user -d postgres -tc \
  "SELECT datname FROM pg_database WHERE datname LIKE 'test_db\_%'" \
  | xargs -r -n1 -I{} psql -h localhost -p 5433 -U test_user -d postgres -c 'DROP DATABASE IF EXISTS "{}" WITH (FORCE)'
```

## Redis

`redis_db` flushes a dedicated Redis DB (`6380/0`) before and after each test
that requests it — unaffected by the above.
