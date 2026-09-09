# 5. Time-partition management for `messages` and `message_receipt_log`

Date: 2026-08-29

## Status

Accepted

## Context

`messages` (RANGE by `created_at`) and `message_receipt_log` (RANGE by
`occurred_at`) are both created with a **single DEFAULT partition** by
`scripts/init_db.py`. That is fine for dev but collapses at scale: every
row lands in one physical table, no partition pruning happens, and old
data can never be detached cheaply. At the target scale (tens of billions
of messages) we need real dated partitions plus automated creation,
retention, and cold-storage management.

`scripts/prune_receipt_log.py` already DETACH+DROPs old `message_receipt_log`
partitions on a daily cron — but nothing *creates* the dated partitions it
assumes exist.

## Decision

### Partition layout

| Topic | Decision |
|---|---|
| `messages` granularity | **Weekly** (ISO week, aligned to Monday 00:00 UTC) |
| `message_receipt_log` granularity | **Daily** (30-day retention → ~35 live partitions) |
| HASH sub-partitioning (chat_id) | Not now — future option if a weekly partition exceeds ~1B rows |
| `messages` retention | Infinite; old partitions → cold tablespace + `VACUUM FREEZE` + `autovacuum_enabled=false` |
| DEFAULT partition | Kept as a safety net on both tables; any row that lands in it raises an alert |
| Naming | `messages_y2026w07` · `message_receipt_log_y2026m08d29` |
| Pre-create buffer | `messages`: 6 weeks ahead · `message_receipt_log`: 10 days ahead |
| Cold threshold | `messages` partition older than 12 months → cold |

Weekly for `messages` balances partition count (~52/year, unbounded but
manageable for a decade+) against per-partition row volume. Daily for
`message_receipt_log` matches its fixed 30-day retention — each drop
reclaims exactly one day.

### Engine: standalone Python script, not `pg_partman`

We use a **standalone, idempotent Python script** (`scripts/manage_partitions.py`),
not the `pg_partman` extension.

- The dev/CI Postgres image is `postgres:15-alpine`, which does not bundle
  `pg_partman`. Its background worker also needs `shared_preload_libraries`
  and a server restart, and many managed Postgres providers disallow the
  extension entirely — so relying on it would fragment our environments.
- We already own this pattern: `prune_receipt_log.py` is a cron-driven
  Python maintenance script hitting the same two tables. `manage_partitions.py`
  is its creation-side counterpart and can eventually absorb it.
- Our needs are narrow (create-ahead, report, cold-freeze, one-time
  DEFAULT migration) and map to a few dozen lines of idempotent DDL. The
  flexibility cost of not using `pg_partman` is low; the portability win is
  high.

Trade-off: we hand-roll boundary math (ISO-week / day alignment) and must
schedule the script ourselves (ADR step 8). Both are covered by tests and
a P1 alert if the future-partition buffer runs low.

### Migration path (plan step 5)

The existing DEFAULT partitions already hold dev data and, in a future
prod cutover, would hold live rows. Migration runs **online**, during a
maintenance window, via `manage_partitions.py --migrate-default`:

1. Create the empty historical dated partitions covering the DEFAULT's
   date span.
2. `DETACH` the DEFAULT partition (it becomes a plain table; new writes
   now hit the real partitions or fail into a fresh empty DEFAULT).
3. Move rows in `--batch-size` batches by `created_at` / `occurred_at`
   (`INSERT … SELECT` + `DELETE`) while the app keeps running.
4. `ATTACH` an empty DEFAULT back as the safety net.
5. Verify row counts before/after.

In dev the shortcut is `--drop` + `init_db` + `seed_mock_data`; the script
supports both paths.

### Why DEFAULT stays

Even with full automation, a clock bug, a missed cron run, or a backdated
insert could produce a row with no matching partition. Without a DEFAULT
that `INSERT` fails outright. The DEFAULT absorbs it instead, and
`--report` counts its rows so monitoring can alert — a caught anomaly
rather than a write outage.

## Consequences

- New config keys in `config.py` (plan step 2), no logic.
- New `scripts/manage_partitions.py` with `--ensure` / `--report` /
  `--cold` / `--migrate-default` subcommands, all `--dry-run` capable.
- `init_db.py` keeps creating the DEFAULT partitions and additionally
  calls `manage_partitions --ensure`.
- New cron jobs (plan step 8); `--ensure` failure is P1.
- Message-read CRUD gains a derived `created_at BETWEEN …` predicate
  (from the page's Snowflake-id range) so queries actually prune
  (plan step 6). `message_receipt_log` already filters on `occurred_at`.
- `.claude_docs/database_schema.md`'s "no automated partition management"
  gap is closed on completion (plan step 9).
