"""
Creates every table (and the DEFAULT partition the messages table needs)
against DATABASE_URL. There are no migrations yet - this is the dev/manual-
testing equivalent of what tests/conftest.py does automatically per test.

Usage:
    python3 -m scripts.init_db
    python3 -m scripts.init_db --drop
"""
import asyncio
import sys
import os
from dotenv import load_dotenv

# 1. טוען את משתני הסביבה מקובץ ה-.env לפני שמייבאים מודולים שדורשים אותם
load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from database.base import Base
from database.connection import DATABASE_URL
# Registers every model on Base.metadata - importing database.connection alone
# doesn't import the model modules themselves.
from database.models import (  # noqa: F401
    chat,
    message,
    message_receipt_log,
    participant,
    private_chat_pair,
    user,
)


async def main(drop: bool) -> None:
    engine = create_async_engine(DATABASE_URL)

    async with engine.begin() as conn:
        if drop:
            await conn.run_sync(Base.metadata.drop_all)
            print("Dropped all tables.")
        else:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS messages_default PARTITION OF messages DEFAULT"
            ))
            # Same story for message_receipt_log (RANGE by occurred_at). In
            # production this DEFAULT partition should be replaced by real
            # monthly partitions + a partition-creation cron + the retention
            # prune (scripts/prune_receipt_log.py) - see CLAUDE.md's
            # no-migrations / no-partition-management gap.
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS message_receipt_log_default "
                "PARTITION OF message_receipt_log DEFAULT"
            ))
            # create_all never ALTERs an existing table - add media columns
            # explicitly so an already-initialised dev DB picks them up
            # without a --drop (see CLAUDE.md "no DB migrations").
            for ddl in (
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_key TEXT",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_mime TEXT",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_size BIGINT",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_name TEXT",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_duration_seconds BIGINT",
                # Voice-recording "played" receipt watermarks (see
                # MessageStatus.PLAYED / crud_participant.recompute_chat_receipt_cursors).
                "ALTER TABLE participants ADD COLUMN IF NOT EXISTS last_played_message_id BIGINT",
                "ALTER TABLE chats ADD COLUMN IF NOT EXISTS all_played_up_to_message_id BIGINT",
                # Coarse per-participant "last acknowledged at" timestamps
                # (see database/models/participant.py) - the never-expiring
                # fallback next to the 30-day message_receipt_log.
                "ALTER TABLE participants ADD COLUMN IF NOT EXISTS last_delivered_at TIMESTAMPTZ",
                "ALTER TABLE participants ADD COLUMN IF NOT EXISTS last_read_at TIMESTAMPTZ",
                "ALTER TABLE participants ADD COLUMN IF NOT EXISTS last_played_at TIMESTAMPTZ",
            ):
                await conn.execute(text(ddl))
            print(
                "Created all tables (+ messages_default / message_receipt_log_default "
                "partitions, + media/receipt columns)."
            )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(drop="--drop" in sys.argv))