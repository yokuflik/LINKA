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
# Time-partition manager (ADR 0005). The DEFAULT partitions below stay as a
# safety net; this additionally pre-creates the real dated partitions.
from scripts.manage_partitions import ensure_partitions
# Registers every model on Base.metadata - importing database.connection alone
# doesn't import the model modules themselves.
from database.models import (  # noqa: F401
    chat,
    media_blob,
    message,
    message_receipt_log,
    participant,
    private_chat_pair,
    user,
    user_settings,
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
                # Per-user chat pinning (see database/models/participant.py).
                # NULL = not pinned; sorted above un-pinned chats by pinned_at DESC.
                "ALTER TABLE participants ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMPTZ",
                # Per-user chat mute (see database/models/participant.py, ADR 0004).
                # NULL = not muted; a future timestamp = muted until then.
                "ALTER TABLE participants ADD COLUMN IF NOT EXISTS muted_until TIMESTAMPTZ",
                # Per-user settings (privacy, ...) - one JSONB blob per user.
                # create_all makes this on a fresh DB; spelled out here so an
                # already-initialised dev DB picks it up without a --drop.
                "CREATE TABLE IF NOT EXISTS user_settings ("
                "  user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,"
                "  settings JSONB NOT NULL DEFAULT '{}'::jsonb,"
                "  updated_at TIMESTAMPTZ DEFAULT now()"
                ")",
                # Content-addressed media blob index (ADR 0010). Spelled out
                # so an already-initialised dev DB picks it up without --drop.
                "CREATE TABLE IF NOT EXISTS media_blob ("
                "  sha256 TEXT PRIMARY KEY,"
                "  storage_key TEXT NOT NULL,"
                "  bucket TEXT NOT NULL,"
                "  kind TEXT NOT NULL,"
                "  mime TEXT NOT NULL,"
                "  size BIGINT NOT NULL,"
                "  ref_count BIGINT NOT NULL DEFAULT 0,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                "  uploaded_at TIMESTAMPTZ"
                ")",
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_media_blob_storage_key "
                "ON media_blob (storage_key)",
            ):
                await conn.execute(text(ddl))
            # Real dated partitions on top of the DEFAULT safety net (ADR 0005).
            await ensure_partitions(conn)
            print(
                "Created all tables (+ messages_default / message_receipt_log_default "
                "partitions, + dated partitions, + media/receipt columns)."
            )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(drop="--drop" in sys.argv))