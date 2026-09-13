"""Embeds the messages seeded by scripts.seed_mock_data with Gemini and builds
the IVFFlat index (ADR 0042).

Requires scripts.seed_mock_data to have been run first (default sizing: 3
users, 4 chats, 1,000 messages total - this script embeds those same rows,
it does not insert a second batch).

Steps:
  1. Select up to 1,000 text messages with no embedding yet.
  2. Embed them via Gemini's batchEmbedContents, 100 texts/request, with a 4s
     sleep between requests (free-tier ~15 RPM).
  3. UPDATE messages.embedding per row.
  4. Build the IVFFlat index - deliberately done here, AFTER the data exists,
     never in scripts/init_db.py (see modules/vector_search/ddl.py + ADR 0042:
     IVFFlat needs representative rows to compute useful cluster centroids).

Usage:
    python3 -m scripts.seed_vector_data
    python3 -m scripts.seed_vector_data --limit 500

Dev-only local embedding cache (ADR 0043): set VECTOR_EMBED_CACHE=1 to skip
re-calling Gemini for text already embedded in a previous run (keyed on
model+dim+text, stored in .dev_cache/, never committed). Saves quota across
repeated reseeds of the same mock-data phrase pool.
    VECTOR_EMBED_CACHE=1 python3 -m scripts.seed_vector_data
    python3 -m scripts.seed_vector_data --clear-cache
"""

import argparse
import asyncio
import time

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select

from config import settings
from infra.db.connection import dispose_engine, session_scope
from modules.messaging.models import Message
# Message.chat / Message.sender are string-referenced relationships
# ("Chat" / "User", which themselves reference "Participant") - these imports
# are what registers every class on the ORM mapper registry before the query
# below configures Message's mapper (same import set as scripts/init_db.py).
from modules.chats.models import chat as _chat  # noqa: F401
from modules.chats.models import participant as _participant  # noqa: F401
from modules.users.models import User  # noqa: F401
from modules.vector_search import dev_cache, gemini_client
from modules.vector_search.crud import update_embeddings
from modules.vector_search.ddl import ensure_ivfflat_index

# The free-tier quota is 100 embed sub-requests/minute, and each item inside
# one batchEmbedContents call counts individually - so a batch of 90 (not
# 100) leaves margin, and consecutive batches need to wait out the *minute*
# window, not a few seconds (12 batches for the default 1,000-message seed).
GEMINI_BATCH_SIZE = 90
GEMINI_REQUEST_SLEEP_SECONDS = 65


async def _fetch_unembedded_messages(session, limit: int) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            select(Message.id, Message.content)
            .where(
                Message.type == 1,
                Message.embedding.is_(None),
                Message.content.is_not(None),
                Message.deleted_at.is_(None),
                Message.purged_at.is_(None),
            )
            .order_by(Message.id)
            .limit(limit)
        )
    ).all()
    return [(r.id, r.content) for r in rows]


async def _embed_and_write_back(session, rows: list[tuple[int, str]]) -> None:
    total = len(rows)
    for start in range(0, total, GEMINI_BATCH_SIZE):
        chunk = rows[start : start + GEMINI_BATCH_SIZE]
        request_no = start // GEMINI_BATCH_SIZE + 1
        total_requests = (total + GEMINI_BATCH_SIZE - 1) // GEMINI_BATCH_SIZE
        print(f"Embedding batch {request_no}/{total_requests} ({len(chunk)} messages)...")
        misses_before = dev_cache.misses
        if dev_cache.CACHE_ENABLED:
            vectors = await dev_cache.embed_batch_cached([content for _id, content in chunk])
        else:
            vectors = await gemini_client.embed_batch([content for _id, content in chunk])
        await update_embeddings(session, [(mid, vec) for (mid, _content), vec in zip(chunk, vectors)])
        # Only a batch that actually hit Gemini needs to respect the
        # per-minute quota window - an all-cache-hit batch made no live call.
        made_live_call = not dev_cache.CACHE_ENABLED or dev_cache.misses > misses_before
        if request_no < total_requests and made_live_call:
            time.sleep(GEMINI_REQUEST_SLEEP_SECONDS)


async def main(limit: int) -> None:
    if not settings.GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is not set - cannot embed seed messages.")

    if dev_cache.CACHE_ENABLED:
        print(f"Local embedding cache enabled ({dev_cache.CACHE_PATH}).")

    async with session_scope() as session:
        rows = await _fetch_unembedded_messages(session, limit)
        if not rows:
            print("No un-embedded text messages found - run scripts.seed_mock_data first, or already done.")
            return

        print(f"Requesting embeddings for {len(rows)} messages from Gemini (this takes a few minutes)...")
        await _embed_and_write_back(session, rows)

        print(f"Building IVFFlat index (lists={settings.VECTOR_IVFFLAT_LISTS})...")
        await ensure_ivfflat_index(await session.connection())
        await session.commit()

    await dispose_engine()
    if dev_cache.CACHE_ENABLED:
        print(f"Cache: {dev_cache.hits} hits / {dev_cache.misses} misses (Gemini calls saved: {dev_cache.hits}).")
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--clear-cache", action="store_true", help="Delete the local embedding cache before running.")
    args = parser.parse_args()

    if args.clear_cache:
        dev_cache.clear_cache()
        print(f"Cleared local embedding cache ({dev_cache.CACHE_PATH}).")

    asyncio.run(main(args.limit))
