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

from infra.db.base import Base
from infra.db.connection import DATABASE_URL
# Time-partition manager (ADR 0005). The DEFAULT partitions below stay as a
# safety net; this additionally pre-creates the real dated partitions.
from scripts.manage_partitions import ensure_partitions
# Message search schema: content_tsv column + trigger + btree_gin index (ADR 0040).
from modules.search.ddl import apply_search_ddl
# Semantic vector search (ADR 0042): pgvector extension + embedding column.
from modules.vector_search.ddl import apply_vector_ddl, ensure_vector_extension
# Agent knowledge base FTS (ADR 0046 decision 4): content_tsv trigger + index.
from modules.agents.knowledge_ddl import apply_knowledge_ddl
# Registers every model on Base.metadata - importing database.connection alone
# doesn't import the model modules themselves.
from modules.chats.models import chat
from modules.media import models as media_blob
from modules.messaging import models as message
from modules.receipts import models as message_receipt_log
from modules.chats.models import participant
from modules.chats.models import private_chat_pair
from modules.auth import models as reserved_username
from modules.users import models as user
from modules.settings import models as user_settings
from modules.agents import models as agent


async def main(drop: bool) -> None:
    engine = create_async_engine(DATABASE_URL)

    async with engine.begin() as conn:
        if drop:
            await conn.run_sync(Base.metadata.drop_all)
            print("Dropped all tables.")
        else:
            # Must run before create_all: Message.embedding is `vector(768)`,
            # a type the `vector` extension provides (ADR 0042).
            await ensure_vector_extension(conn)
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
                # Blurred placeholder (ThumbHash, base64) - ADR 0014.
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_blur_hash TEXT",
                # Hard "delete forever" (ADR 0021) - stamped when the sender
                # purges an already-soft-deleted message.
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ",
                "ALTER TABLE media_blob ADD COLUMN IF NOT EXISTS blur_hash TEXT",
                # Per-user hard storage quota (ADR 0028) - running total of
                # sent-media sizes; existing users start at 0 (no backfill).
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS storage_bytes_used BIGINT NOT NULL DEFAULT 0",
                # ADR 0039: client-side E2EE dropped in favour of the server-side
                # cloud model (plaintext in `content`, indexable). Reverses the
                # ADR 0026/0034 schema - drop the encryption columns and the
                # public-key distribution table.
                "ALTER TABLE messages DROP COLUMN IF EXISTS is_encrypted",
                "ALTER TABLE messages DROP COLUMN IF EXISTS enc_header",
                "ALTER TABLE chats DROP COLUMN IF EXISTS last_message_enc",
                "DROP TABLE IF EXISTS user_public_keys",
                # Inline avatar thumbnail (~64px JPEG data: URI) - ADR 0016.
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_pic_preview TEXT",
                "ALTER TABLE chats ADD COLUMN IF NOT EXISTS profile_pic_preview TEXT",
                "ALTER TABLE users DROP COLUMN IF EXISTS profile_pic_blur_hash",
                "ALTER TABLE chats DROP COLUMN IF EXISTS profile_pic_blur_hash",
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
                # Unique lowercase username + change-cooldown timestamp (ADR 0017).
                # Add the columns nullable, backfill existing rows with a
                # deterministic unique handle, then enforce UNIQUE + NOT NULL.
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(32)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS username_changed_at TIMESTAMPTZ",
                # Rolling username-change quota log (ADR 0023).
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS username_change_log JSONB",
                "UPDATE users SET username = 'user_' || id WHERE username IS NULL",
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)",
                "ALTER TABLE users ALTER COLUMN username SET NOT NULL",
                # Optional free-form display name (ADR 0024). No uniqueness/index.
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR(80)",
                # Released-username grace hold (ADR 0017).
                # Scheduled messages (ADR 0031). Unpartitioned, low-volume.
                # Spelled out so an already-initialised dev DB picks it up
                # without a --drop; a deployed DB runs this once by hand.
                "CREATE TABLE IF NOT EXISTS scheduled_messages ("
                "  id BIGINT PRIMARY KEY,"
                "  chat_id BIGINT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,"
                "  sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                "  scheduled_for TIMESTAMPTZ NOT NULL,"
                "  type SMALLINT NOT NULL DEFAULT 1,"
                "  content TEXT,"
                "  media_key TEXT,"
                "  media_mime TEXT,"
                "  media_size BIGINT,"
                "  media_name TEXT,"
                "  media_duration_seconds BIGINT,"
                "  media_blur_hash TEXT,"
                "  reply_to_message_id BIGINT,"
                "  client_message_id TEXT NOT NULL,"
                "  status SMALLINT NOT NULL DEFAULT 0,"
                "  last_error TEXT,"
                "  fire_attempts SMALLINT NOT NULL DEFAULT 0,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                "  updated_at TIMESTAMPTZ"
                ")",
                "CREATE INDEX IF NOT EXISTS ix_scheduled_messages_sender_scheduled "
                "ON scheduled_messages (sender_id, scheduled_for)",
                "CREATE INDEX IF NOT EXISTS ix_scheduled_messages_status_scheduled "
                "ON scheduled_messages (status, scheduled_for)",
                "CREATE TABLE IF NOT EXISTS reserved_usernames ("
                "  username VARCHAR(32) PRIMARY KEY,"
                "  reserved_for_user_id BIGINT NOT NULL,"
                "  released_at TIMESTAMPTZ NOT NULL,"
                "  expires_at TIMESTAMPTZ NOT NULL"
                ")",
                # BYOK Gemini key (ADR 0046 decision 5). Fernet ciphertext,
                # NULL = use the shared settings.GEMINI_API_KEY.
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS encrypted_gemini_api_key BYTEA",
                # Trigger-shape drift safety net: a column's server_default is
                # fixed at DDL time, so create_all never re-applies it after
                # DEFAULT_AGENT_TRIGGERS gains new keys (on_unknown_sender -
                # ADR 0046 decision 2, on_schedule - decision 3). Re-pins the
                # default for future inserts AND backfills any existing row
                # missing either key (JSONB merge - never touches a row that
                # already has both keys, so per-agent customization survives).
                "ALTER TABLE agents ALTER COLUMN triggers SET DEFAULT "
                "'{\"on_time_window\": {\"enabled\": false, \"start\": \"09:00\", \"end\": \"22:00\"}, "
                "\"on_specific_chats\": {}, \"on_unknown_sender\": {\"enabled\": false}, "
                "\"on_schedule\": []}'::jsonb",
                "UPDATE agents SET triggers = "
                "'{\"on_unknown_sender\": {\"enabled\": false}, \"on_schedule\": []}'::jsonb || triggers "
                "WHERE NOT (triggers ? 'on_unknown_sender') OR NOT (triggers ? 'on_schedule')",
                # can_message_groups default flip (AGENT_DRAWER_UI_PLAN.md /
                # ADR 0048, new agents only - user-confirmed 2026-09-23, no
                # backfill of existing agents). Same server_default-is-fixed-
                # at-DDL-time issue as triggers above: only re-pins the
                # column default for future inserts, never UPDATEs existing
                # rows.
                "ALTER TABLE agents ALTER COLUMN restrictions SET DEFAULT "
                "'{\"can_send_messages\": true, \"can_message_groups\": false, "
                "\"can_message_private\": true, \"can_message_new_private_contacts\": true, "
                "\"can_leave_groups\": true, \"blocked_read_chat_ids\": [], "
                "\"max_messages_per_day\": null}'::jsonb",
                # ADR 0047 decision 3: skill/persona in force for execution-
                # mode turns. New column, existing rows backfilled to the
                # same default new rows get (no per-agent customization to
                # preserve yet, unlike the triggers backfill above).
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS active_skill VARCHAR(32) "
                "NOT NULL DEFAULT 'one_off_executor'",
                # ADR 0047 decision 2: is_enabled default flips True -> False -
                # every new agent starts dormant until the owner explicitly
                # turns it on. New rows only, no backfill of existing agents
                # (an already-enabled agent stays enabled).
                "ALTER TABLE agents ALTER COLUMN is_enabled SET DEFAULT false",
                # ADR 0047 decision 5: dynamic runtime pause state, written by
                # the agent itself via pause_and_escalate. New column, existing
                # rows backfilled to the empty default (no prior per-agent
                # value to preserve).
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS paused_chat_ids JSONB "
                "NOT NULL DEFAULT '[]'::jsonb",
                # ADR 0049: sub-state inside the config chat, written by the
                # agent's own transfer_to_builder/transfer_to_help/
                # finish_building_agent tools. New column, existing rows
                # backfilled to the same default new rows get (no prior
                # per-agent value to preserve, same as active_skill above).
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS builder_state VARCHAR(32) "
                "NOT NULL DEFAULT 'supervisor'",
            ):
                await conn.execute(text(ddl))
            # Semantic vector search (ADR 0042): embedding column safety net
            # for an already-initialised dev DB (extension already ensured
            # above, before create_all). The IVFFlat index is deliberately
            # NOT created here - see scripts/seed_vector_data.py.
            await apply_vector_ddl(conn)
            # Message search (ADR 0040): content_tsv column + the trigger that
            # maintains it + the composite btree_gin index. Runs after the
            # column DDL above and before ensure_partitions() so the trigger /
            # index are on the parent when new partitions inherit them. A
            # deployed DB also needs `scripts/backfill_search_tsv.py` for
            # pre-existing rows.
            await apply_search_ddl(conn)
            # Agent knowledge base FTS (ADR 0046 decision 4): content_tsv
            # trigger + gin(agent_id, content_tsv) index on the new
            # agent_knowledge_chunks table (created by create_all above).
            await apply_knowledge_ddl(conn)
            # Real dated partitions on top of the DEFAULT safety net (ADR 0005).
            await ensure_partitions(conn)
            print(
                "Created all tables (+ messages_default / message_receipt_log_default "
                "partitions, + dated partitions, + media/receipt columns)."
            )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(drop="--drop" in sys.argv))