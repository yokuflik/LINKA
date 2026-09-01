"""
Time-partition management for `messages` (RANGE by created_at, weekly) and
`message_receipt_log` (RANGE by occurred_at, daily). See ADR 0005.

One idempotent script. Sub-commands:

    python3 -m scripts.manage_partitions --ensure   [--dry-run]
    python3 -m scripts.manage_partitions --report   [--dry-run]
    python3 -m scripts.manage_partitions --cold     [--dry-run]

--ensure   creates every missing dated partition from the current period up to
           `now + buffer` (config.MESSAGE_PARTITION_PRECREATE_WEEKS /
           RECEIPT_LOG_PRECREATE_DAYS) for both tables. Indexes are inherited
           from the partitioned parent, so nothing else is needed. Safe to run
           repeatedly - `CREATE TABLE IF NOT EXISTS ... PARTITION OF`.
--report   prints, per parent: each attached partition with its range, an
           estimated row count (pg_class.reltuples), on-disk size, plus the
           exact row count still sitting in the DEFAULT partition (any non-zero
           value there is an alert - a row landed with no dated partition).

--cold     freezes every `messages` partition whose whole range is older than
           config.MESSAGE_PARTITION_COLD_AFTER_MONTHS: VACUUM FREEZE, optional
           move to config.MESSAGE_PARTITION_COLD_TABLESPACE, then
           autovacuum_enabled=false. Idempotent - a partition already carrying
           autovacuum_enabled=false (and, if a target tablespace is set, already
           on it) is skipped. `message_receipt_log` is excluded (it has a
           retention DROP via scripts/prune_receipt_log.py instead).

--dry-run  --ensure / --cold log what they would do without executing it;
           --report is read-only regardless.

Dev has only the catch-all DEFAULT partition; running --ensure there simply
adds real dated partitions alongside it. The DEFAULT partition is always kept
as a safety net and is never created/dropped by this script.
"""
import argparse
import asyncio
import calendar
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import (
    MESSAGE_PARTITION_COLD_AFTER_MONTHS,
    MESSAGE_PARTITION_COLD_TABLESPACE,
    MESSAGE_PARTITION_INTERVAL,
    MESSAGE_PARTITION_PRECREATE_WEEKS,
    RECEIPT_LOG_PARTITION_INTERVAL,
    RECEIPT_LOG_PRECREATE_DAYS,
)
from database.connection import DATABASE_URL


class TableSpec:
    """One partitioned parent and how its time partitions are shaped."""

    def __init__(self, parent: str, interval: str, precreate: int, partition_column: str = "created_at"):
        self.parent = parent
        self.interval = interval  # "week" | "day"
        self.precreate = precreate  # how many whole intervals to keep ahead
        self.partition_column = partition_column  # RANGE key

    @property
    def default_partition(self) -> str:
        return f"{self.parent}_default"


TABLE_SPECS = [
    TableSpec("messages", MESSAGE_PARTITION_INTERVAL, MESSAGE_PARTITION_PRECREATE_WEEKS, "created_at"),
    TableSpec("message_receipt_log", RECEIPT_LOG_PARTITION_INTERVAL, RECEIPT_LOG_PRECREATE_DAYS, "occurred_at"),
]


# --- boundary math (pure, no DB) -------------------------------------------

def _interval_delta(interval: str) -> timedelta:
    if interval == "week":
        return timedelta(weeks=1)
    if interval == "day":
        return timedelta(days=1)
    raise ValueError(f"unsupported partition interval: {interval!r}")


def period_start(interval: str, moment: datetime) -> datetime:
    """Lower bound of the partition that contains `moment`.

    week -> Monday 00:00 UTC of that ISO week. day -> 00:00 UTC of that day.
    """
    m = moment.astimezone(timezone.utc)
    start = datetime(m.year, m.month, m.day, tzinfo=timezone.utc)
    if interval == "week":
        start -= timedelta(days=start.weekday())  # Monday == 0
    elif interval != "day":
        raise ValueError(f"unsupported partition interval: {interval!r}")
    return start


def partition_suffix(interval: str, start: datetime) -> str:
    """`y2026w07` for a weekly partition, `y2026m08d29` for a daily one."""
    if interval == "week":
        iso = start.isocalendar()
        return f"y{iso.year}w{iso.week:02d}"
    return f"y{start.year}m{start.month:02d}d{start.day:02d}"


def wanted_partitions(spec: TableSpec, now: datetime) -> list[tuple[str, datetime, datetime]]:
    """(name, lower, upper) for every partition that should exist right now:
    the current period through `now + spec.precreate` intervals, inclusive."""
    delta = _interval_delta(spec.interval)
    start = period_start(spec.interval, now)
    horizon = start + spec.precreate * delta
    out: list[tuple[str, datetime, datetime]] = []
    cur = start
    while cur <= horizon:
        upper = cur + delta
        name = f"{spec.parent}_{partition_suffix(spec.interval, cur)}"
        out.append((name, cur, upper))
        cur = upper
    return out


def partitions_between(spec: TableSpec, lo: datetime, hi: datetime) -> list[tuple[str, datetime, datetime]]:
    """(name, lower, upper) for every period partition needed to cover the
    closed span [lo, hi] - used to build the historical partitions the current
    DEFAULT rows will move into."""
    delta = _interval_delta(spec.interval)
    out: list[tuple[str, datetime, datetime]] = []
    cur = period_start(spec.interval, lo)
    while cur <= hi:
        upper = cur + delta
        out.append((f"{spec.parent}_{partition_suffix(spec.interval, cur)}", cur, upper))
        cur = upper
    return out


def _pg_ts(dt: datetime) -> str:
    # dt is always tz-aware UTC and generated by us, so a plain inlined
    # literal is safe (mirrors scripts/prune_receipt_log.py).
    return dt.strftime("%Y-%m-%d %H:%M:%S+00")


def cold_cutoff(now: datetime, months: int) -> datetime:
    """A `messages` partition whose upper bound is <= this instant is cold:
    `now` shifted back `months` calendar months (day clamped to month length)."""
    y, m = now.year, now.month - months
    while m <= 0:
        m += 12
        y -= 1
    d = min(now.day, calendar.monthrange(y, m)[1])
    return now.replace(year=y, month=m, day=d)


def _bound_upper(bound: str | None) -> datetime | None:
    """Upper bound of a `FOR VALUES FROM (...) TO (...)` clause, as tz-aware UTC.
    None for the DEFAULT partition or an unparseable clause."""
    if not bound or "DEFAULT" in bound:
        return None
    try:
        hi = bound.split("TO (")[1].split(")")[0].strip().strip("'")
    except IndexError:
        return None
    return datetime.strptime(hi[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def cold_eligible(parts: list[tuple[str, str | None]], cutoff: datetime) -> list[str]:
    """Names of partitions (name, partition-bound-expr) whose whole range ends
    at or before `cutoff` - pure, DB-independent."""
    out = []
    for name, bound in parts:
        up = _bound_upper(bound)
        if up is not None and up <= cutoff:
            out.append(name)
    return out


# --- operations -----------------------------------------------------------

async def ensure_partitions(conn, now: datetime | None = None, dry_run: bool = False) -> list[str]:
    """Create every missing dated partition for both parents. Returns the list
    of partition names created (or, in dry-run, that would be created)."""
    now = now or datetime.now(timezone.utc)
    touched: list[str] = []
    for spec in TABLE_SPECS:
        for name, lo, hi in wanted_partitions(spec, now):
            ddl = (
                f'CREATE TABLE IF NOT EXISTS {name} PARTITION OF {spec.parent} '
                f"FOR VALUES FROM ('{_pg_ts(lo)}') TO ('{_pg_ts(hi)}')"
            )
            if dry_run:
                print(f"would create {name}  [{lo.date()} .. {hi.date()})")
            else:
                await conn.execute(text(ddl))
                print(f"ensured {name}  [{lo.date()} .. {hi.date()})")
            touched.append(name)
    return touched


async def _partition_rows(conn, parent: str) -> list[tuple[str, str | None, int, int]]:
    rows = (await conn.execute(text(
        """
        SELECT c.relname AS partition_name,
               pg_get_expr(c.relpartbound, c.oid) AS bound,
               c.reltuples::bigint AS est_rows,
               pg_total_relation_size(c.oid) AS bytes
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = :parent
        ORDER BY c.relname
        """
    ), {"parent": parent})).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}PB"


async def report(conn) -> None:
    """Print a per-parent partition table plus DEFAULT-partition row counts."""
    for spec in TABLE_SPECS:
        print(f"\n=== {spec.parent} ===")
        print(f"{'partition':<32} {'range':<26} {'est.rows':>12} {'size':>10}")
        parts = await _partition_rows(conn, spec.parent)
        for name, bound, est_rows, size in parts:
            rng = "DEFAULT" if not bound or "DEFAULT" in bound else _bound_range(bound)
            print(f"{name:<32} {rng:<26} {est_rows:>12,} {_fmt_bytes(size):>10}")

        default_rows = (await conn.execute(text(
            f"SELECT count(*) FROM ONLY {spec.default_partition}"
        ))).scalar_one()
        flag = "  <-- ALERT: rows with no dated partition" if default_rows else ""
        print(f"DEFAULT partition holds {default_rows:,} row(s){flag}")


def _bound_range(bound: str) -> str:
    # bound: FOR VALUES FROM ('2026-02-09 00:00:00+00') TO ('2026-02-16 00:00:00+00')
    try:
        lo = bound.split("FROM (")[1].split(")")[0].strip().strip("'")
        hi = bound.split("TO (")[1].split(")")[0].strip().strip("'")
        return f"{lo[:10]}..{hi[:10]}"
    except IndexError:
        return bound


async def _span_in_default(conn, spec: TableSpec) -> tuple[datetime, datetime] | None:
    row = (await conn.execute(text(
        f"SELECT min({spec.partition_column}), max({spec.partition_column}) "
        f"FROM ONLY {spec.default_partition}"
    ))).one()
    if row[0] is None:
        return None
    return row[0], row[1]


async def migrate_default(engine, batch_size: int = 10000, dry_run: bool = False) -> dict[str, int]:
    """One-time: drain each DEFAULT partition into real dated partitions.

    Per parent: DETACH the DEFAULT (it becomes a standalone table; new writes
    now route to dated partitions) -> create the historical partitions covering
    its data span plus the forward buffer -> move rows in `batch_size` chunks
    ordered by the partition column (DELETE ... RETURNING piped into INSERT, one
    committed tx per batch) -> re-ATTACH the emptied table as DEFAULT. Row
    counts are verified before/after.

    Idempotent-ish: a re-run on an already-empty DEFAULT is a no-op. Safe to
    run while the app serves traffic, but the DETACH/ATTACH each take a brief
    ACCESS EXCLUSIVE lock on the parent - schedule a maintenance window.
    """
    moved_by_parent: dict[str, int] = {}
    for spec in TABLE_SPECS:
        # Recover from a prior run that was interrupted between DETACH and
        # ATTACH: the standalone <parent>_default table lingers un-attached and
        # `CREATE TABLE IF NOT EXISTS ... PARTITION OF` never re-attaches it.
        async with engine.begin() as conn:
            exists, attached = (await conn.execute(text(
                """
                SELECT to_regclass(:qual) IS NOT NULL,
                       EXISTS (SELECT 1 FROM pg_inherits i
                               JOIN pg_class c ON c.oid = i.inhrelid
                               JOIN pg_class p ON p.oid = i.inhparent
                               WHERE p.relname = :parent AND c.relname = :child)
                """
            ), {"qual": spec.default_partition, "parent": spec.parent,
                "child": spec.default_partition})).one()
            if exists and not attached and not dry_run:
                print(f"{spec.parent}: re-attaching orphaned {spec.default_partition}")
                await conn.execute(text(
                    f"ALTER TABLE {spec.parent} ATTACH PARTITION {spec.default_partition} DEFAULT"
                ))

        async with engine.begin() as conn:
            span = await _span_in_default(conn, spec)
        if span is None:
            print(f"{spec.parent}: DEFAULT is empty, nothing to migrate")
            moved_by_parent[spec.parent] = 0
            continue

        lo, hi = span
        # Extend coverage to "now" so the DETACH gap has no uncovered dates
        # between the newest existing row and the forward buffer.
        hist = partitions_between(spec, lo, max(hi, datetime.now(timezone.utc)))
        print(f"{spec.parent}: DEFAULT spans {lo.isoformat()} .. {hi.isoformat()} "
              f"-> {len(hist)} historical partition(s)")

        if dry_run:
            for name, plo, phi in hist:
                print(f"  would create {name}  [{plo.date()} .. {phi.date()})")
            async with engine.begin() as conn:
                total = (await conn.execute(text(
                    f"SELECT count(*) FROM ONLY {spec.default_partition}"
                ))).scalar_one()
            print(f"  would move {total:,} row(s) in batches of {batch_size:,}")
            moved_by_parent[spec.parent] = 0
            continue

        # 1. count what we are about to move (DEFAULT still attached).
        async with engine.begin() as conn:
            before = (await conn.execute(text(
                f"SELECT count(*) FROM ONLY {spec.default_partition}"
            ))).scalar_one()

        # 2. DETACH first - it becomes a standalone table of the same name.
        #    Postgres refuses to create a partition that overlaps rows still in
        #    an attached DEFAULT, so every CREATE PARTITION must come after this.
        async with engine.begin() as conn:
            await conn.execute(text(
                f"ALTER TABLE {spec.parent} DETACH PARTITION {spec.default_partition}"
            ))

        # 3. historical partitions covering the detached data's span + the
        #    forward buffer, so both old and new writes have a home during the
        #    window between DETACH and the re-ATTACH below.
        async with engine.begin() as conn:
            for name, plo, phi in hist:
                await conn.execute(text(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {spec.parent} "
                    f"FOR VALUES FROM ('{_pg_ts(plo)}') TO ('{_pg_ts(phi)}')"
                ))
            await ensure_partitions(conn)

        # 4. drain in batches, one committed tx each.
        moved = 0
        while True:
            async with engine.begin() as conn:
                n = (await conn.execute(text(
                    f"""
                    WITH batch AS (
                        SELECT ctid FROM {spec.default_partition}
                        ORDER BY {spec.partition_column}
                        LIMIT {int(batch_size)}
                    ),
                    moved AS (
                        DELETE FROM {spec.default_partition}
                        WHERE ctid IN (SELECT ctid FROM batch)
                        RETURNING *
                    )
                    INSERT INTO {spec.parent} SELECT * FROM moved
                    """
                ))).rowcount
            if n == 0:
                break
            moved += n
            print(f"  {spec.parent}: moved {moved:,}/{before:,}")

        # 5. re-attach the emptied table as DEFAULT.
        async with engine.begin() as conn:
            await conn.execute(text(
                f"ALTER TABLE {spec.parent} ATTACH PARTITION {spec.default_partition} DEFAULT"
            ))
            leftover = (await conn.execute(text(
                f"SELECT count(*) FROM ONLY {spec.default_partition}"
            ))).scalar_one()

        if moved != before or leftover != 0:
            raise RuntimeError(
                f"{spec.parent}: migration mismatch - before={before}, moved={moved}, "
                f"leftover_in_default={leftover}"
            )
        print(f"{spec.parent}: migrated {moved:,} row(s), DEFAULT now empty")
        moved_by_parent[spec.parent] = moved

    return moved_by_parent


async def run_cold(engine, now: datetime | None = None, dry_run: bool = False) -> list[str]:
    """Freeze `messages` partitions older than the cold threshold.

    Per eligible partition: VACUUM (FREEZE, ANALYZE) (autocommit), optional
    ALTER TABLE ... SET TABLESPACE <cold>, then SET (autovacuum_enabled = false).
    Idempotent: a partition already flagged autovacuum_enabled=false (and on the
    target tablespace, when one is configured) is left alone. Returns the names
    frozen (or, in dry-run, that would be).
    """
    now = now or datetime.now(timezone.utc)
    spec = TABLE_SPECS[0]  # messages only
    assert spec.parent == "messages"
    cutoff = cold_cutoff(now, MESSAGE_PARTITION_COLD_AFTER_MONTHS)
    target_ts = MESSAGE_PARTITION_COLD_TABLESPACE.strip()

    async with engine.begin() as conn:
        rows = (await conn.execute(text(
            """
            SELECT c.relname,
                   pg_get_expr(c.relpartbound, c.oid) AS bound,
                   ts.spcname AS tablespace,
                   c.reloptions
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            LEFT JOIN pg_tablespace ts ON ts.oid = c.reltablespace
            WHERE p.relname = :parent
            ORDER BY c.relname
            """
        ), {"parent": spec.parent})).all()
        ts_exists = bool(target_ts) and (await conn.execute(text(
            "SELECT 1 FROM pg_tablespace WHERE spcname = :n"
        ), {"n": target_ts})).scalar_one_or_none() is not None

    if target_ts and not ts_exists:
        print(f"cold: warning - tablespace {target_ts!r} does not exist; "
              f"will VACUUM FREEZE + disable autovacuum only, no move")

    pending: list[str] = []
    for name, bound, cur_ts, relopts in rows:
        up = _bound_upper(bound)
        if up is None or up > cutoff:
            continue
        frozen_flag = bool(relopts) and "autovacuum_enabled=false" in relopts
        on_target = (not target_ts) or (not ts_exists) or (cur_ts == target_ts)
        if frozen_flag and on_target:
            continue
        pending.append(name)

    if not pending:
        print(f"cold: no messages partitions with upper bound <= {cutoff.date()} to freeze")
        return []

    if dry_run:
        for name in pending:
            move = f" -> tablespace {target_ts}" if target_ts and ts_exists else ""
            print(f"would freeze {name}{move} + set autovacuum_enabled=false")
        return pending

    frozen: list[str] = []
    for name in pending:
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"VACUUM (FREEZE, ANALYZE) {name}"))
        async with engine.begin() as conn:
            if target_ts and ts_exists:
                await conn.execute(text(f"ALTER TABLE {name} SET TABLESPACE {target_ts}"))
            await conn.execute(text(f"ALTER TABLE {name} SET (autovacuum_enabled = false)"))
        moved = f", moved to {target_ts}" if target_ts and ts_exists else ""
        print(f"cold: froze {name}{moved}")
        frozen.append(name)
    return frozen


async def main() -> None:
    parser = argparse.ArgumentParser(description="Manage messages / message_receipt_log time partitions.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ensure", action="store_true", help="create missing dated partitions up to the buffer horizon")
    group.add_argument("--report", action="store_true", help="print partition fill / DEFAULT state")
    group.add_argument("--migrate-default", action="store_true", help="one-time: drain DEFAULT partitions into dated ones")
    group.add_argument("--cold", action="store_true", help="freeze messages partitions older than the cold threshold")
    parser.add_argument("--dry-run", action="store_true", help="--ensure / --cold / --migrate-default: show the plan without writing")
    parser.add_argument("--batch-size", type=int, default=10000, help="--migrate-default: rows moved per committed transaction")
    args = parser.parse_args()

    engine = create_async_engine(DATABASE_URL)
    try:
        if args.migrate_default:
            await migrate_default(engine, batch_size=args.batch_size, dry_run=args.dry_run)
        elif args.cold:
            await run_cold(engine, dry_run=args.dry_run)
        else:
            async with engine.begin() as conn:
                if args.ensure:
                    await ensure_partitions(conn, dry_run=args.dry_run)
                else:
                    await report(conn)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
