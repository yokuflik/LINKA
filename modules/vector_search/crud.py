"""DB access for semantic vector search (ADR 0042). Raw SQL (not the ORM) for
both the embedding write-back and the similarity query, same rationale as
modules/search/crud.py's tsquery calls: the `<=>` cosine-distance operator and
the vector cast aren't ORM-expressible without extra ceremony, and the
similarity query needs the exact JOIN shape below to be index/participant-safe.

Casts use `CAST(:param AS vector)`, not `:param::vector` - SQLAlchemy's
`text()` named-paramstyle parser mishandles a `::` cast glued directly onto a
bind parameter name (silently drops the parameter instead of raising), so the
unambiguous `CAST` form is required here.
"""

from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def update_embeddings(session: AsyncSession, rows: Sequence[tuple[int, list[float]]]) -> None:
    """Write back one embedding per (message_id, vector) pair. Executemany-style:
    one statement, N parameter sets - message ids are the composite PK's first
    column only, so this updates across whatever partition each id lives in
    (no created_at predicate needed: :id is an exact PK-prefix match)."""
    if not rows:
        return
    await session.execute(
        # asyncpg binds a Python str as PG `text`; the explicit cast to
        # `vector` is required for the assignment (pgvector defines no
        # implicit text->vector assignment cast for a typed bind parameter).
        text("UPDATE messages SET embedding = CAST(:embedding AS vector) WHERE id = :id"),
        [{"id": mid, "embedding": str(vec)} for mid, vec in rows],
    )
    await session.commit()


async def semantic_search_messages(
    session: AsyncSession,
    *,
    user_id: int,
    query_embedding: list[float],
    limit: int,
    max_distance: float,
    chat_id: Optional[int] = None,
):
    """Cosine-nearest messages, membership enforced by the participants JOIN
    (ADR 0040's pattern) - a removed member's chats never surface, checked at
    query time rather than off a point-in-time chat-id list. `chat_id`, when
    given, additionally scopes to one chat (still re-checks membership via the
    JOIN rather than trusting the caller). `max_distance` drops rows below the
    relevance floor instead of letting LIMIT pad the page with unrelated
    matches (ADR 0042 addendum)."""
    where_extra = "AND m.chat_id = :chat_id " if chat_id is not None else ""
    stmt = text(
        "SELECT m.id, m.chat_id, m.sender_id, m.type, m.content, m.created_at, "
        "m.embedding <=> CAST(:query_embedding AS vector) AS distance "
        "FROM messages m "
        "INNER JOIN participants p ON p.chat_id = m.chat_id AND p.user_id = :user_id "
        "WHERE m.embedding IS NOT NULL "
        "AND m.deleted_at IS NULL AND m.purged_at IS NULL AND m.sender_id IS NOT NULL "
        "AND m.embedding <=> CAST(:query_embedding AS vector) < :max_distance "
        f"{where_extra}"
        "ORDER BY m.embedding <=> CAST(:query_embedding AS vector) "
        "LIMIT :limit"
    )
    params = {
        "query_embedding": str(query_embedding),
        "user_id": user_id,
        "limit": limit,
        "max_distance": max_distance,
    }
    if chat_id is not None:
        params["chat_id"] = chat_id
    result = await session.execute(stmt, params)
    return result.mappings().all()
