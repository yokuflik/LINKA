"""Knowledge-base (RAG) upload + retrieval orchestration (ADR 0046 decision 4,
ADR 0078).

Client-side responsibility split (confirmed in the ADR):
- text/plain and text/markdown: client uploads the raw file to S3 (existing
  presigned-URL flow), then POSTs just the file key - the server fetches it
  and chunks it itself (modules.agents.chunking.chunk_text).
- application/pdf: the browser extracts + chunks the text itself (pdf.js);
  the original PDF bytes still go to S3 for reference/download, but the
  server never re-parses them - it only stores the chunk array it's given.
- Chat-originated text (ADR 0078, `save_knowledge_from_text`): no S3 involved
  at all - `commit_knowledge_text` runs the same chunk -> quota-check ->
  document -> chunk-rows sequence directly on text already in hand.

Every commit path (`commit_knowledge_document`/`commit_knowledge_text`) embeds
its chunks synchronously, one Gemini `embed_batch` call per document, right
after the chunk rows are written - see `_embed_chunks_best_effort`'s
docstring for why this is synchronous rather than queued.
"""

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ids.client import next_id
from modules.agents import crud as agent_crud
from modules.agents.chunking import chunk_text
from modules.agents.crud import KnowledgeQuotaExceededError
from modules.agents.knowledge_crud import update_chunk_embeddings
from modules.agents.models import Agent, AgentKnowledgeChunk, AgentKnowledgeDocument
from modules.media import media_service
from modules.vector_search import gemini_client
from modules.vector_search.errors import EmbeddingProviderError, EmbeddingProviderUnavailableError

logger = logging.getLogger(__name__)


class KnowledgeValidationError(Exception):
    """A bad upload request (unknown MIME, oversize, empty chunk list)."""


def create_knowledge_upload_ticket(mime_type: str, size_bytes: int):
    """Presigned PUT for a knowledge-document upload - reuses the same
    upload-ticket machinery as message media/avatars (modules.media.media_service),
    just under the 'agent_knowledge' kind (private bucket, own MIME allowlist)."""
    if mime_type not in settings.AGENT_KNOWLEDGE_ALLOWED_MIME:
        raise KnowledgeValidationError(f"content type {mime_type!r} is not allowed for knowledge documents")
    return media_service.create_upload_ticket("agent_knowledge", mime_type, size_bytes)


async def _fetch_text_from_s3(storage_key: str) -> str:
    """Server-side fetch for the text/markdown path only - never called for
    PDFs (those are parsed client-side, ADR 0046 decision 4)."""
    url = media_service.download_url(storage_key, bucket=settings.UPLOAD_BUCKET_BY_KIND["agent_knowledge"])
    async with httpx.AsyncClient(timeout=settings.GEMINI_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def commit_knowledge_document(
    session: AsyncSession,
    agent: Agent,
    *,
    filename: str,
    storage_key: str,
    mime_type: str,
    chunks: list[str] | None,
) -> tuple[AgentKnowledgeDocument, list[str]]:
    """Finalizes an uploaded knowledge document: text/markdown is fetched and
    chunked server-side (`chunks` must be None/empty); PDF chunks arrive
    pre-computed from the client (`chunks` must be non-empty - the server
    never runs a PDF parser). Raises KnowledgeQuotaExceededError before
    writing anything if the per-agent document/chunk caps would be exceeded.

    Returns (document, chunk_list) - the caller (router.py, ADR 0085) uses
    the actual chunk text to seed the owner-notice turn with real content,
    not just filename/mime metadata."""
    if mime_type not in settings.AGENT_KNOWLEDGE_ALLOWED_MIME:
        raise KnowledgeValidationError(f"content type {mime_type!r} is not allowed for knowledge documents")

    if mime_type in settings.AGENT_KNOWLEDGE_SERVER_CHUNKED_MIME:
        if chunks:
            raise KnowledgeValidationError("chunks must not be supplied for a server-chunked mime type")
        raw_text = await _fetch_text_from_s3(storage_key)
        chunk_list = chunk_text(raw_text)
    else:
        if not chunks:
            raise KnowledgeValidationError("chunks are required for this mime type (client-side parsing)")
        chunk_list = [c.strip() for c in chunks if c and c.strip()]

    if not chunk_list:
        raise KnowledgeValidationError("no extractable text found in this document")

    await agent_crud.check_knowledge_quota(session, agent.id, len(chunk_list))

    document = AgentKnowledgeDocument(
        id=await next_id(),
        agent_id=agent.id,
        filename=filename[: 255],
        s3_key=storage_key,
        mime_type=mime_type,
        status="ready",
    )
    session.add(document)
    await session.flush()

    chunk_rows = []
    for index, content in enumerate(chunk_list):
        chunk = AgentKnowledgeChunk(
            id=await next_id(),
            agent_id=agent.id,
            document_id=document.id,
            chunk_index=index,
            content=content,
        )
        session.add(chunk)
        chunk_rows.append(chunk)
    await session.flush()
    await _embed_chunks_best_effort(session, chunk_rows)
    return document, chunk_list


async def _embed_chunks_best_effort(session: AsyncSession, chunks: list[AgentKnowledgeChunk]) -> None:
    """Synchronous per-document embedding (ADR 0078), not the flush-on-demand
    Redis queue ADR 0042 uses for messages: knowledge documents are created
    rarely (an occasional owner/agent action) rather than on every send, so
    there's no hot-path latency to defer around, and having a chunk
    searchable immediately after the tool call returns is worth more than the
    batching win the message queue exists for.

    Mirrors ADR 0042's "drop the batch on Gemini failure" posture - an embed
    failure logs a warning and leaves `embedding` NULL rather than blocking or
    rolling back the whole document commit. A chunk with no embedding simply
    falls back to get_knowledge_index/fetch_chunk (still FTS-searchable via
    content_tsv), it is not lost."""
    if not chunks:
        return
    try:
        vectors = await gemini_client.embed_batch([c.content for c in chunks])
    except (EmbeddingProviderError, EmbeddingProviderUnavailableError):
        logger.warning("knowledge-chunk embedding failed - %d chunks left unembedded", len(chunks), exc_info=True)
        return
    await update_chunk_embeddings(session, [(c.id, vec) for c, vec in zip(chunks, vectors)])
    await session.commit()


async def commit_knowledge_text(
    session: AsyncSession,
    agent: Agent,
    *,
    source_label: str,
    raw_text: str,
) -> AgentKnowledgeDocument:
    """Text-only ingestion path (ADR 0078) for the `save_knowledge_from_text`
    config tool - no S3 upload, no upload ticket, nothing to fetch. Runs the
    same chunk -> quota-check -> document -> chunk-rows sequence as
    `commit_knowledge_document`, skipping the S3 fetch entirely since the text
    is already in hand from the tool call argument."""
    chunk_list = chunk_text(raw_text)
    if not chunk_list:
        raise KnowledgeValidationError("no extractable text in the provided content")

    await agent_crud.check_knowledge_quota(session, agent.id, len(chunk_list))

    document = AgentKnowledgeDocument(
        id=await next_id(),
        agent_id=agent.id,
        filename=source_label[:255],
        s3_key=None,
        mime_type="text/plain",
        status="ready",
    )
    session.add(document)
    await session.flush()

    chunk_rows = []
    for index, content in enumerate(chunk_list):
        chunk = AgentKnowledgeChunk(
            id=await next_id(),
            agent_id=agent.id,
            document_id=document.id,
            chunk_index=index,
            content=content,
        )
        session.add(chunk)
        chunk_rows.append(chunk)
    await session.flush()
    await _embed_chunks_best_effort(session, chunk_rows)
    return document


async def delete_knowledge_document(session: AsyncSession, agent: Agent, document_id: int) -> bool:
    """Returns False if no such document exists for this agent (404 at the
    router). Deletes the S3 object best-effort - a failed delete there must
    not block removing the searchable rows. `s3_key` is None (ADR 0078) for a
    document created via save_knowledge_from_text - nothing to delete there,
    so the S3 call is skipped entirely rather than attempted with a null key."""
    document = await agent_crud.get_knowledge_document(session, agent.id, document_id)
    if document is None:
        return False

    if document.s3_key:
        try:
            await media_service.delete_object(document.s3_key, bucket=settings.UPLOAD_BUCKET_BY_KIND["agent_knowledge"])
        except Exception:
            logger.warning("failed to delete S3 object for knowledge document %s", document.id, exc_info=True)

    await agent_crud.delete_knowledge_document(session, document)
    return True


async def search_knowledge_semantic(
    session: AsyncSession, agent: Agent, *, query: str, limit: int,
) -> list[dict]:
    """Ranked chunk retrieval (ADR 0078) - embeds the query, cosine-searches
    this agent's own `agent_knowledge_chunks`, returns the top matches'
    content directly (no index-then-fetch two-hop, unlike
    get_knowledge_index/fetch_chunk). Uses the same live query-embedding LRU
    (query_cache, ADR 0044) as message semantic search - independent cache
    key space (keyed on model+dim+text, agent-agnostic), safe to share."""
    from modules.agents.knowledge_crud import semantic_search_knowledge_chunks
    from modules.vector_search import query_cache

    q = (query or "").strip()
    if not q:
        raise KnowledgeValidationError("query must not be empty")

    query_embedding = await query_cache.embed_query_cached(q)
    rows = await semantic_search_knowledge_chunks(
        session,
        agent_id=agent.id,
        query_embedding=query_embedding,
        limit=limit,
        max_distance=settings.VECTOR_SEARCH_MAX_DISTANCE,
    )
    return [
        {
            "document_id": r["document_id"],
            "filename": r["filename"],
            "chunk_id": r["chunk_id"],
            "content": r["content"],
            "distance": r["distance"],
        }
        for r in rows
    ]
