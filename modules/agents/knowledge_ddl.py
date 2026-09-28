"""Schema DDL for the agent knowledge base / RAG (ADR 0046 decision 4, ADR 0078).

Applied by both `scripts/init_db.py` and `tests/conftest.py`, same convention
as `modules/search/ddl.py`. All statements are idempotent.

Design:
- `agent_knowledge_chunks.content_tsv` is kept in lockstep with `content` by a
  BEFORE INSERT/UPDATE trigger, mirroring ADR 0040's messages trigger (a
  GENERATED column would need care around the FK-heavy insert path here too).
- One `btree_gin (agent_id, content_tsv)` partial index, same shape as ADR
  0040's `(chat_id, content_tsv)`, scoped to `agent_id` instead of `chat_id`.
- `embedding` (ADR 0078) mirrors `messages.embedding` (ADR 0042) in miniature -
  a SEPARATE `vector` column and IVFFlat index on this unrelated table, never
  sharing the messages index/queue. Like ADR 0042's, `ensure_knowledge_
  ivfflat_index` is deliberately NOT called by `apply_knowledge_ddl` - IVFFlat
  computed against an empty/near-empty table produces a useless index. It is
  built once, deferred, after real chunks with embeddings exist (see the
  `scripts/seed_vector_data.py`-style operational step for messages).
"""

from sqlalchemy import text

from config import settings

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
    # ADR 0078: the `vector` extension is already created by
    # modules/vector_search/ddl.py::ensure_vector_extension, which always runs
    # first (scripts/init_db.py orders it before this call) - no second
    # `CREATE EXTENSION` needed here, just the column.
    f"ALTER TABLE agent_knowledge_chunks ADD COLUMN IF NOT EXISTS embedding vector({settings.VECTOR_EMBEDDING_DIM})",
)


async def apply_knowledge_ddl(conn) -> None:
    """Run every statement in `KNOWLEDGE_DDL` on an open async connection.

    Call it AFTER `agent_knowledge_chunks` exists (create_all / the table DDL
    in scripts/init_db.py) AND after `ensure_vector_extension` has run.
    """
    for stmt in KNOWLEDGE_DDL:
        await conn.execute(text(stmt))


async def ensure_knowledge_ivfflat_index(conn) -> None:
    """Build the IVFFlat cosine-distance index over
    `agent_knowledge_chunks.embedding`. Call only after a representative slice
    of chunks already has an embedding - see this module's docstring. Not
    called by `apply_knowledge_ddl`/`scripts/init_db.py` automatically, same
    deferred-build posture as `modules.vector_search.ddl.ensure_ivfflat_index`."""
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_knowledge_chunks_embedding_ivfflat "
            "ON agent_knowledge_chunks USING ivfflat (embedding vector_cosine_ops) "
            f"WITH (lists = {settings.VECTOR_IVFFLAT_LISTS})"
        )
    )
