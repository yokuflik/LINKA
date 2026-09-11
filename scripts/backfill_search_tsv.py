"""Backfill messages.content_tsv for rows that predate the search feature (ADR 0040).

New rows get their vector from the `trg_messages_content_tsv` trigger; this fills
in everything already in the table, one committed batch at a time so it never
holds a long transaction or a table-wide lock on the partitioned `messages`.

Usage:
    python3 -m scripts.backfill_search_tsv [--batch-size 5000] [--dry-run]

Safe to interrupt and re-run - it only ever touches rows still NULL.
"""
import argparse
import asyncio

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from infra.db.connection import DATABASE_URL

# One UPDATE per batch. `ctid` keyed so Postgres walks the heap in physical
# order; the trigger recomputes the vector on write, so we just re-set content
# to itself is NOT enough (UPDATE OF content only fires when the value changes) -
# set content_tsv directly instead.
_BATCH_SQL = text(
    """
    WITH batch AS (
        SELECT ctid
        FROM messages
        WHERE content_tsv IS NULL
        LIMIT :batch_size
    )
    UPDATE messages m
    SET content_tsv = to_tsvector(
        'simple',
        coalesce(m.content, '') || ' ' || coalesce(m.media_name, '')
    )
    FROM batch
    WHERE m.ctid = batch.ctid
    """
)

_COUNT_SQL = text("SELECT count(*) FROM messages WHERE content_tsv IS NULL")


async def main(batch_size: int, dry_run: bool) -> None:
    engine = create_async_engine(DATABASE_URL)
    total_done = 0
    try:
        async with engine.begin() as conn:
            remaining = (await conn.execute(_COUNT_SQL)).scalar_one()
        print(f"{remaining} message rows need a search vector.")
        if dry_run or remaining == 0:
            return
        while True:
            # One committed transaction per batch - never a long-held lock.
            async with engine.begin() as conn:
                result = await conn.execute(_BATCH_SQL, {"batch_size": batch_size})
            moved = result.rowcount or 0
            total_done += moved
            print(f"  backfilled {moved} (running total {total_done})")
            if moved == 0:
                break
    finally:
        await engine.dispose()
    print(f"Done. {total_done} rows backfilled.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.batch_size, args.dry_run))
