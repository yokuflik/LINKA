"""Knowledge-base (RAG) upload + retrieval orchestration (ADR 0046 decision 4).

Client-side responsibility split (confirmed in the ADR):
- text/plain and text/markdown: client uploads the raw file to S3 (existing
  presigned-URL flow), then POSTs just the file key - the server fetches it
  and chunks it itself (modules.agents.chunking.chunk_text).
- application/pdf: the browser extracts + chunks the text itself (pdf.js);
  the original PDF bytes still go to S3 for reference/download, but the
  server never re-parses them - it only stores the chunk array it's given.
"""

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ids.client import next_id
from modules.agents import crud as agent_crud
from modules.agents.chunking import chunk_text
from modules.agents.crud import KnowledgeQuotaExceededError
from modules.agents.models import Agent, AgentKnowledgeChunk, AgentKnowledgeDocument
from modules.media import media_service

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
) -> AgentKnowledgeDocument:
    """Finalizes an uploaded knowledge document: text/markdown is fetched and
    chunked server-side (`chunks` must be None/empty); PDF chunks arrive
    pre-computed from the client (`chunks` must be non-empty - the server
    never runs a PDF parser). Raises KnowledgeQuotaExceededError before
    writing anything if the per-agent document/chunk caps would be exceeded."""
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

    for index, content in enumerate(chunk_list):
        session.add(
            AgentKnowledgeChunk(
                id=await next_id(),
                agent_id=agent.id,
                document_id=document.id,
                chunk_index=index,
                content=content,
            )
        )
    await session.flush()
    return document


async def delete_knowledge_document(session: AsyncSession, agent: Agent, document_id: int) -> bool:
    """Returns False if no such document exists for this agent (404 at the
    router). Deletes the S3 object best-effort - a failed delete there must
    not block removing the searchable rows."""
    document = await agent_crud.get_knowledge_document(session, agent.id, document_id)
    if document is None:
        return False

    try:
        await media_service.delete_object(document.s3_key, bucket=settings.UPLOAD_BUCKET_BY_KIND["agent_knowledge"])
    except Exception:
        logger.warning("failed to delete S3 object for knowledge document %s", document.id, exc_info=True)

    await agent_crud.delete_knowledge_document(session, document)
    return True
