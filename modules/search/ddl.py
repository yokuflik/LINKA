"""Schema DDL for message search (ADR 0040).

Applied verbatim by both `scripts/init_db.py` (fresh / deployed dev DB) and
`tests/conftest.py` (the ephemeral per-run test DB, ADR 0032) so every
environment agrees. All statements are idempotent (`IF NOT EXISTS` /
`OR REPLACE` / `DROP ... IF EXISTS`).

Design (see ADR 0040):
- `messages.content_tsv` is a real column on the model too, so `create_all`
  makes it on a fresh DB; the ALTER here is the deployed-DB safety net.
- A BEFORE INSERT/UPDATE row trigger on the partitioned parent keeps the vector
  in lockstep with `content` + `media_name` (a `GENERATED` column would rewrite
  every partition). The trigger cascades to all existing and future partitions.
- One composite `gin (chat_id, content_tsv)` partial index via `btree_gin`
  serves both in-chat (`chat_id = ...`) and global (`content_tsv @@ ...`)
  search; the partial predicate keeps deleted / purged / system rows out.
"""

from sqlalchemy import text

_TSV_FUNCTION = """
CREATE OR REPLACE FUNCTION messages_content_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.content_tsv := to_tsvector(
        'simple',
        coalesce(NEW.content, '') || ' ' || coalesce(NEW.media_name, '')
    );
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

SEARCH_DDL: tuple[str, ...] = (
    # btree_gin lets `chat_id` (a bigint) sit in the same GIN index as the
    # tsvector - a standard contrib extension, present on RDS / vanilla PG.
    "CREATE EXTENSION IF NOT EXISTS btree_gin",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS content_tsv tsvector",
    _TSV_FUNCTION,
    "DROP TRIGGER IF EXISTS trg_messages_content_tsv ON messages",
    "CREATE TRIGGER trg_messages_content_tsv "
    "BEFORE INSERT OR UPDATE OF content, media_name ON messages "
    "FOR EACH ROW EXECUTE FUNCTION messages_content_tsv_trigger()",
    "CREATE INDEX IF NOT EXISTS ix_messages_chatid_tsv ON messages "
    "USING gin (chat_id, content_tsv) "
    "WHERE deleted_at IS NULL AND purged_at IS NULL AND sender_id IS NOT NULL",
)


async def apply_search_ddl(conn) -> None:
    """Run every statement in `SEARCH_DDL` on an open async connection.

    Call it AFTER the tables / partitions exist (the trigger and index attach
    to `messages` and cascade to its partitions).
    """
    for stmt in SEARCH_DDL:
        await conn.execute(text(stmt))
