"""
Retention for message_receipt_log: drop whole partitions older than
config.RECEIPT_LOG_RETENTION_DAYS. The coarse Participant.last_*_at columns
are unaffected and remain the fallback for "when did they last read" on an
old message.

In dev there is only the catch-all DEFAULT partition, so this is a no-op
there (it never drops the default). In production the table should be
carved into real time-range partitions (monthly) by a partition-management
job, and this script - run daily from cron - detaches + drops the ones that
have aged out.

Usage:
    python3 -m scripts.prune_receipt_log [--dry-run]
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import RECEIPT_LOG_RETENTION_DAYS
from database.connection import DATABASE_URL

_PARENT = "message_receipt_log"


async def main(dry_run: bool) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECEIPT_LOG_RETENTION_DAYS)
    engine = create_async_engine(DATABASE_URL)

    async with engine.begin() as conn:
        # Every attached partition of the parent, with the upper bound of its
        # range. A partition whose whole range ends at/before the cutoff can
        # be dropped. The DEFAULT partition has no bound and is always kept.
        rows = (await conn.execute(text(
            """
            SELECT c.relname AS partition_name,
                   pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = :parent
            """
        ), {"parent": _PARENT})).all()

        dropped = 0
        for partition_name, bound in rows:
            if bound is None or "DEFAULT" in (bound or ""):
                continue
            # bound looks like: FOR VALUES FROM ('2025-01-01...') TO ('2025-02-01...')
            try:
                upper = bound.split("TO (")[1].rstrip(") ").strip("'")
                upper_dt = datetime.fromisoformat(upper)
                if upper_dt.tzinfo is None:
                    upper_dt = upper_dt.replace(tzinfo=timezone.utc)
            except (IndexError, ValueError):
                print(f"skip {partition_name}: could not parse bound {bound!r}")
                continue

            if upper_dt <= cutoff:
                print(f"{'would drop' if dry_run else 'dropping'} {partition_name} (ends {upper_dt.isoformat()})")
                if not dry_run:
                    await conn.execute(text(f'ALTER TABLE {_PARENT} DETACH PARTITION {partition_name}'))
                    await conn.execute(text(f'DROP TABLE {partition_name}'))
                dropped += 1

        print(f"{'(dry run) ' if dry_run else ''}done - {dropped} partition(s) older than {cutoff.date()}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
