"""Schema DDL for semantic vector search (ADR 0042).

Split from the IVFFlat index on purpose. `apply_vector_ddl` (extension +
column) is safe to run on an empty table and is called by `scripts/init_db.py`
/ `tests/conftest.py` like every other DDL block. `ensure_ivfflat_index` is
**not** - IVFFlat computes its cluster centroids from whatever data exists at
CREATE INDEX time, so building it against an empty/near-empty table produces
a useless index. It is called explicitly, once, after the seed/backfill
script has populated `embedding` for its rows (see scripts/seed_vector_data.py).
"""

from sqlalchemy import text

from config import settings

async def ensure_vector_extension(conn) -> None:
    """`CREATE EXTENSION IF NOT EXISTS vector` only. MUST run before
    `Base.metadata.create_all()` - the `vector` type has to exist before
    `CREATE TABLE messages (... embedding vector(768) ...)` is emitted, unlike
    the search feature's TSVECTOR (a built-in type, no extension needed)."""
    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


async def apply_vector_ddl(conn) -> None:
    """Extension (idempotent no-op if already created pre-`create_all`) + the
    `embedding` column via `ADD COLUMN IF NOT EXISTS` - the deployed-DB safety
    net for a database initialised before this column existed (same pattern as
    every other `ALTER TABLE ... IF NOT EXISTS` in scripts/init_db.py)."""
    await ensure_vector_extension(conn)
    await conn.execute(
        text(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS embedding "
            f"vector({settings.VECTOR_EMBEDDING_DIM})"
        )
    )


async def ensure_ivfflat_index(conn) -> None:
    """Build the IVFFlat cosine-distance index. Call only after `embedding` is
    populated for a representative slice of `messages` - see the ADR 0042
    rationale. Idempotent; re-running after significant data growth (a
    `DROP INDEX` + re-run) is the documented way to re-tune `lists`."""
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_messages_embedding_ivfflat ON messages "
            "USING ivfflat (embedding vector_cosine_ops) "
            f"WITH (lists = {settings.VECTOR_IVFFLAT_LISTS})"
        )
    )
