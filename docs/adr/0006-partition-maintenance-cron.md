# 6. Partition-maintenance scheduling: a committed crontab run on one host

Date: 2026-08-29

## Status

Accepted

## Context

ADR 0005 established `scripts/manage_partitions.py` (`--ensure` / `--report` /
`--cold` / `--migrate-default`) and `scripts/prune_receipt_log.py` as the
mechanism for time-partition upkeep, but left *how they get run on a schedule*
to a later step (plan step 8). The project has no scheduling infrastructure:
`docker-compose.yml` is dev-only (Postgres / Redis / MinIO), and there is no
app-embedded scheduler.

Constraints:

- These jobs must run **exactly once cluster-wide** per tick. `--ensure`
  creating a partition twice is harmless (idempotent), but running the drain
  or `--cold` concurrently from many replicas is wasteful and lock-heavy.
- `--ensure` failing is a **P1** (writes will fall into DEFAULT within
  `MESSAGE_PARTITION_PRECREATE_WEEKS`); the schedule must be observable.
- No new runtime dependency should be added to the app process. An in-app
  scheduler (APScheduler/Celery-beat) would run per replica and needs
  leader-election to be safe — rejected as disproportionate.

## Decision

- Commit the schedule as **`deploy/partition-maintenance.crontab`** plus a
  single wrapper **`scripts/partition_maintenance.sh`** that loads the
  environment (`DATABASE_URL`, …) and dispatches to one sub-command, logging
  start/exit-code to stdout (for the platform's log collector) and exiting
  non-zero on failure.
- The crontab is installed on **one** scheduling context — a dedicated cron
  host, a single Kubernetes `CronJob` per line, or an equivalent — **never
  per app replica**. This ADR does not mandate which; it mandates "exactly
  one".
- Schedule (UTC):

  | Cron | Command | Purpose |
  |---|---|---|
  | `10 0 * * *` | `partition_maintenance.sh ensure` | pre-create dated partitions to the buffer horizon |
  | `30 0 * * *` | `partition_maintenance.sh prune-receipts` | DETACH+DROP `message_receipt_log` partitions past retention |
  | `0 1 * * 0` | `partition_maintenance.sh cold` | freeze `messages` partitions older than the cold threshold |
  | `*/15 * * * *` | `partition_maintenance.sh report` | emit partition fill / DEFAULT state for monitoring |

  `ensure` runs before `prune-receipts` so a buffer partition is never briefly
  missing. `report` runs frequently and cheaply so alerting has fresh data.
- **Alerting (left to the platform, documented here):** page if `ensure`
  exits non-zero, if `report` shows any rows in a DEFAULT partition, or if
  `report` shows fewer than 2 future `messages` partitions.
- `--migrate-default` is **not** scheduled — it is a one-time online
  migration, run by hand in a maintenance window (ADR 0005).

## Consequences

- Deploying Linka for real now includes "install `deploy/partition-maintenance.crontab`
  on exactly one scheduler". The repo carries the schedule; the platform
  carries the runner and the paging rules.
- The wrapper is the single place that knows how to invoke the Python module,
  so the crontab stays declarative and testable (the sub-command dispatch is
  covered by a shell-free unit that asserts each keyword maps to the right
  `manage_partitions` / `prune_receipt_log` entrypoint).
- No change to the app image or its dependencies.
