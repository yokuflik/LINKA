"""
Nightly IVFFlat centroid refresh for semantic search (ADR 0042).

REINDEX INDEX CONCURRENTLY rebuilds ix_messages_embedding_ivfflat's cluster
centroids from the table's current data without holding the lock a plain
REINDEX would (no blocking of reads/writes on messages) - the only downside
is it takes longer and needs ~2x the index's disk space during the rebuild.
CONCURRENTLY cannot run inside a transaction block, so the statement is sent
on its own connection with AUTOCOMMIT isolation, mirroring how
scripts.partition_maintenance prints a start/ok/FAILED line with elapsed time
and exits non-zero on failure so cron can alert.

Usage:
    python3 -m scripts.reindex_vectors
"""
import asyncio
import logging
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from infra.db.connection import DATABASE_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [reindex_vectors] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

INDEX_NAME = "ix_messages_embedding_ivfflat"


async def _run() -> None:
    engine = create_async_engine(DATABASE_URL)
    try:
        # AUTOCOMMIT: REINDEX ... CONCURRENTLY is rejected inside a transaction
        # block; engine.connect() with this isolation level issues the
        # statement standalone, one statement per connection, no BEGIN/COMMIT.
        conn = await engine.connect()
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        async with conn:
            await conn.execute(text(f"REINDEX INDEX CONCURRENTLY {INDEX_NAME}"))
    finally:
        await engine.dispose()


def main() -> int:
    started = time.monotonic()
    logger.info("start index=%s", INDEX_NAME)
    try:
        asyncio.run(_run())
    except Exception as exc:  # cron wants a nonzero exit + one log line, not a traceback
        logger.error(
            "FAILED index=%s after %.1fs: %r",
            INDEX_NAME, time.monotonic() - started, exc,
        )
        return 1
    logger.info("ok index=%s in %.1fs", INDEX_NAME, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
