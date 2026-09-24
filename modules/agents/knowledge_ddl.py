"""Schema DDL for the agent knowledge base / RAG (ADR 0046 decision 4).

Applied by both `scripts/init_db.py` and `tests/conftest.py`, same convention
as `modules/search/ddl.py`. All statements are idempotent.

Design:
- `agent_knowledge_chunks.content_tsv` is kept in lockstep with `content` by a
  BEFORE INSERT/UPDATE trigger, mirroring ADR 0040's messages trigger (a
  GENERATED column would need care around the FK-heavy insert path here too).
- One `btree_gin (agent_id, content_tsv)` partial index, same shape as ADR
  0040's `(chat_id, content_tsv)`, scoped to `agent_id` instead of `chat_id`.
"""

from sqlalchemy import text

_TSV_FUNCTION = """
CREATE OR REPLACE FUNCTION agent_knowledge_chunks_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.content_tsv := to_tsvector('simple', coalesce(NEW.content, ''));
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

KNOWLEDGE_DDL: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS btree_gin",
    "ALTER TABLE agent_knowledge_chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector",
    _TSV_FUNCTION,
    "DROP TRIGGER IF EXISTS trg_agent_knowledge_chunks_tsv ON agent_knowledge_chunks",
    "CREATE TRIGGER trg_agent_knowledge_chunks_tsv "
    "BEFORE INSERT OR UPDATE OF content ON agent_knowledge_chunks "
    "FOR EACH ROW EXECUTE FUNCTION agent_knowledge_chunks_tsv_trigger()",
    "CREATE INDEX IF NOT EXISTS ix_agent_knowledge_chunks_agentid_tsv ON agent_knowledge_chunks "
    "USING gin (agent_id, content_tsv)",
)


async def apply_knowledge_ddl(conn) -> None:
    """Run every statement in `KNOWLEDGE_DDL` on an open async connection.

    Call it AFTER `agent_knowledge_chunks` exists (create_all / the table DDL
    in scripts/init_db.py).
    """
    for stmt in KNOWLEDGE_DDL:
        await conn.execute(text(stmt))
