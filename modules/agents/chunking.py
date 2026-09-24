"""Fixed-size/overlap text chunker (ADR 0046 decision 4).

Deliberately no NLP/sentence-boundary awareness - cheap on the 1GB app host.
The client-side PDF chunker (poc/composables/useKnowledgeUpload.js) mirrors
this exact algorithm so retrieval quality is consistent regardless of which
path (server text/markdown vs client-side PDF) a document took.
"""

from config import settings


def chunk_text(text: str, max_chars: int | None = None, overlap_chars: int | None = None) -> list[str]:
    """Splits `text` into overlapping fixed-size chunks. Empty/whitespace-only
    input returns no chunks. `overlap_chars` must be smaller than `max_chars`
    or the window never advances."""
    max_chars = max_chars if max_chars is not None else settings.AGENT_KNOWLEDGE_CHUNK_MAX_CHARS
    overlap_chars = (
        overlap_chars if overlap_chars is not None else settings.AGENT_KNOWLEDGE_CHUNK_OVERLAP_CHARS
    )
    if overlap_chars >= max_chars:
        overlap_chars = 0

    stripped = text.strip()
    if not stripped:
        return []

    chunks = []
    start = 0
    length = len(stripped)
    step = max_chars - overlap_chars
    while start < length:
        end = min(start + max_chars, length)
        chunk = stripped[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == length:
            break
        start += step
    return chunks
