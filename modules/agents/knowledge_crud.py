"""DB access for agent knowledge-base semantic retrieval (ADR 0078). Raw SQL,
same rationale as `modules/vector_search/crud.py`: the `<=>` cosine-distance
operator and the vector cast aren't ORM-expressible without extra ceremony.

Casts use `CAST(:param AS vector)`, not `:param::vector` - see
`modules/vector_search/crud.py`'s docstring for why.
"""

from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def update_chunk_embeddings(session: AsyncSession, rows: Sequence[tuple[int, list[float]]]) -> None:
    """Write back one embedding per (chunk_id, vector) pair. Executemany-style,
    same shape as vector_search.crud.update_embeddings."""
    if not rows:
        return
    await session.execute(
        text("UPDATE agent_knowledge_chunks SET embedding = CAST(:embedding AS vector) WHERE id = :id"),
        [{"id": cid, "embedding": str(vec)} for cid, vec in rows],
    )


async def semantic_search_knowledge_chunks(
    session: AsyncSession,
    *,
    agent_id: int,
    query_embedding: list[float],
    limit: int,
    max_distance: float,
):
    """Cosine-nearest chunks for this agent only - agent_id is a hard filter,
    never optional (same posture as every other knowledge-base query in
    crud.py). `max_distance` drops rows below the relevance floor instead of
    letting LIMIT pad the page with unrelated matches (ADR 0042 addendum,
    mirrored here)."""
    stmt = text(
        "SELECT c.id AS chunk_id, c.document_id, d.filename, c.chunk_index, c.content, "
        "c.embedding <=> CAST(:query_embedding AS vector) AS distance "
        "FROM agent_knowledge_chunks c "
        "JOIN agent_knowledge_documents d ON d.id = c.document_id "
        "WHERE c.agent_id = :agent_id "
        "AND c.embedding IS NOT NULL "
        "AND c.embedding <=> CAST(:query_embedding AS vector) < :max_distance "
        "ORDER BY c.embedding <=> CAST(:query_embedding AS vector) "
        "LIMIT :limit"
    )
    result = await session.execute(
        stmt,
        {
            "agent_id": agent_id,
            "query_embedding": str(query_embedding),
            "max_distance": max_distance,
            "limit": limit,
        },
    )
    return result.mappings().all()
