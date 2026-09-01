"""
Single cron entrypoint for time-partition upkeep (ADR 0006). Maps one keyword
to one maintenance action so `deploy/partition-maintenance.crontab` stays
declarative. Prints a start line, an ok/FAILED line with elapsed time, and
exits non-zero on failure so cron / the platform can alert.

Usage:
    python3 -m scripts.partition_maintenance {ensure|report|cold|prune-receipts} [--dry-run]
"""
import asyncio
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy.ext.asyncio import create_async_engine

from database.connection import DATABASE_URL
from scripts import manage_partitions, prune_receipt_log

JOBS = ("ensure", "report", "cold", "prune-receipts")


async def _run(job: str, dry_run: bool) -> None:
    if job == "prune-receipts":
        await prune_receipt_log.main(dry_run)
        return

    engine = create_async_engine(DATABASE_URL)
    try:
        if job == "ensure":
            async with engine.begin() as conn:
                await manage_partitions.ensure_partitions(conn, dry_run=dry_run)
        elif job == "report":
            async with engine.begin() as conn:
                await manage_partitions.report(conn)
        elif job == "cold":
            await manage_partitions.run_cold(engine, dry_run=dry_run)
        else:  # pragma: no cover - guarded by main()
            raise ValueError(f"unknown job {job!r}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    positional = [a for a in argv if not a.startswith("-")]
    if len(positional) != 1 or positional[0] not in JOBS:
        print(f"usage: python3 -m scripts.partition_maintenance {{{'|'.join(JOBS)}}} [--dry-run]",
              file=sys.stderr)
        return 2

    job = positional[0]
    started = time.monotonic()
    print(f"[partition_maintenance] start job={job} dry_run={dry_run}")
    try:
        asyncio.run(_run(job, dry_run))
    except Exception as exc:  # cron wants a nonzero exit + one log line, not a traceback
        print(f"[partition_maintenance] FAILED job={job} after "
              f"{time.monotonic() - started:.1f}s: {exc!r}", file=sys.stderr)
        return 1
    print(f"[partition_maintenance] ok job={job} in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
