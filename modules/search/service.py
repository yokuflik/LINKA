"""Search orchestration (ADR 0040): query-string -> tsquery, cursor codec,
snippet, permission checks, and the SSE stream generator."""

import base64
import binascii
import json
import logging
import re
import time
from typing import AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.connection import session_scope
from infra.redis.client import redis_client
from modules.chats.crud.crud_chat import get_chat_by_id
from modules.chats.crud.crud_participant import get_all_chat_ids_for_user, is_participant
from modules.messaging.crud import compute_message_status
from modules.messaging.errors import NotAParticipantError
from modules.messaging.models import Message
from modules.messaging.receipt_privacy import mask_status, read_receipts_hidden_for_message
from modules.media import media_service
from modules.search import crud
from modules.search.errors import SearchQueryTooShortError
from modules.search.limits import DEFAULT_SEARCH_LIMITS, SearchLimits
from modules.search.schemas import SearchResponseOut, SearchResultOut

logger = logging.getLogger(__name__)

# A query made only of word characters + spaces gets prefix ("as you type")
# matching on its last term via `to_tsquery`. Anything with punctuation
# (a "quoted phrase", a -exclusion, or/and) goes through `websearch_to_tsquery`
# verbatim, which parses those operators correctly but does not prefix-match.
_SIMPLE_QUERY_RE = re.compile(r"^[\w\s]+$", re.UNICODE)


def build_tsquery(raw: str, limits: SearchLimits = DEFAULT_SEARCH_LIMITS) -> tuple[str, str, list[str]]:
    """(fn_name, value, snippet_terms). Raises SearchQueryTooShortError (-> 422)
    for an empty / too-short / operator-only query, before any DB work."""
    q = " ".join((raw or "").split())
    if len(q) < limits.min_query_len:
        raise SearchQueryTooShortError(f"query must be at least {limits.min_query_len} characters")
    q = q[: limits.max_query_len]

    if _SIMPLE_QUERY_RE.match(q):
        words = q.split()
        # Last word is a prefix match; the rest are exact lexemes, all ANDed.
        # Every token is `\w+`, so it is always a valid `to_tsquery` term.
        parts = [w.lower() for w in words[:-1]] + [words[-1].lower() + ":*"]
        return "to_tsquery", " & ".join(parts), [w for w in words]

    terms = [t.strip('"') for t in q.split() if t.lower() != "or" and not t.startswith("-")]
    return "websearch_to_tsquery", q, [t for t in terms if t]


# --- Opaque cursor (base64 of the last message id) --------------------------

def encode_cursor(message_id: int) -> str:
    return base64.urlsafe_b64encode(str(message_id).encode()).decode().rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[int]:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return int(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, binascii.Error):
        # A malformed cursor is treated as "start from the top", not a 500.
        return None


# --- Snippet (app-side, cheaper than SQL ts_headline on a 1-CPU host) -------

def make_snippet(content: Optional[str], terms: list[str], radius: int) -> Optional[str]:
    if not content:
        return None
    low = content.lower()
    pos = -1
    for term in terms:
        i = low.find(term.lower())
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        head = content[: radius * 2]
        return head + ("…" if len(content) > radius * 2 else "")
    start = max(0, pos - radius)
    end = min(len(content), pos + radius + max((len(t) for t in terms), default=0))
    snippet = content[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(content):
        snippet = snippet + "…"
    return snippet


def _to_result(m: Message, terms: list[str], radius: int) -> SearchResultOut:
    return SearchResultOut(
        id=m.id,
        chat_id=m.chat_id,
        sender_id=m.sender_id,
        type=m.type,
        content=m.content,
        snippet=make_snippet(m.content, terms, radius),
        reply_to_message_id=m.reply_to_message_id,
        media_mime=m.media_mime,
        media_size=m.media_size,
        media_name=m.media_name,
        media_duration_seconds=m.media_duration_seconds,
        media_blur_hash=m.media_blur_hash,
        is_edited=m.is_edited,
        edited_at=m.edited_at,
        deleted_at=m.deleted_at,
        created_at=m.created_at,
    )


def _paginate(rows, limit: int, terms: list[str], radius: int) -> SearchResponseOut:
    has_more = len(rows) > limit
    page = list(rows[:limit])
    next_cursor = encode_cursor(page[-1].id) if (has_more and page) else None
    return SearchResponseOut(
        results=[_to_result(m, terms, radius) for m in page],
        next_cursor=next_cursor,
        has_more=has_more,
    )


# --- Public entry points ---------------------------------------------------

async def search_in_chat(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    raw_query: str,
    cursor: Optional[str],
    limit: int,
    limits: SearchLimits = DEFAULT_SEARCH_LIMITS,
) -> SearchResponseOut:
    # Parse first (no DB), so a too-short query is a clean 422 even before the
    # membership hit; membership is still enforced before the search query runs.
    fn, value, terms = build_tsquery(raw_query, limits)
    if not await is_participant(session, chat_id, user_id):
        raise NotAParticipantError(f"User {user_id} is not a participant of chat {chat_id}")
    rows = await crud.search_chat_messages(
        session,
        chat_id=chat_id,
        tsq_fn=fn,
        tsq_value=value,
        before_id=decode_cursor(cursor),
        limit=limit,
    )
    return _paginate(rows, limit, terms, limits.snippet_radius)


async def search_global(
    session: AsyncSession,
    *,
    user_id: int,
    raw_query: str,
    cursor: Optional[str],
    limit: int,
    limits: SearchLimits = DEFAULT_SEARCH_LIMITS,
) -> SearchResponseOut:
    fn, value, terms = build_tsquery(raw_query, limits)
    ids = list(await get_all_chat_ids_for_user(session, user_id, limit=limits.any_inline_max + 1))
    chat_ids = ids if len(ids) <= limits.any_inline_max else None
    if chat_ids is not None and not chat_ids:
        return SearchResponseOut(results=[], next_cursor=None, has_more=False)
    rows = await crud.search_global_messages(
        session,
        user_id=user_id,
        tsq_fn=fn,
        tsq_value=value,
        before_id=decode_cursor(cursor),
        limit=limit,
        chat_ids=chat_ids,
    )
    return _paginate(rows, limit, terms, limits.snippet_radius)


async def messages_around(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    radius: int,
):
    if not await is_participant(session, chat_id, user_id):
        raise NotAParticipantError(f"User {user_id} is not a participant of chat {chat_id}")
    chat = await get_chat_by_id(session, chat_id)
    hide_read = await read_receipts_hidden_for_message(session, chat_id, sender_id=user_id, chat=chat)
    messages = await crud.messages_around(session, chat_id=chat_id, message_id=message_id, radius=radius)
    for m in messages:
        m.status = compute_message_status(m.id, chat, m.type)
        if hide_read and m.sender_id == user_id:
            m.status = mask_status(m.status)
        m.media_url = media_service.message_media_download_url(m.media_key)
    return messages


# --- SSE stream ----------------------------------------------------------------

def stream_lock_key(user_id: int) -> str:
    return f"search:stream:active:{user_id}"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def stream_search(
    *,
    user_id: int,
    raw_query: str,
    lock_key: str,
    limits: SearchLimits = DEFAULT_SEARCH_LIMITS,
) -> AsyncIterator[str]:
    """Global search as an SSE byte stream over a server-side cursor. Ends with
    `event: done {count, truncated}`; `truncated` is set when the row cap or the
    wall-clock cap is hit. The caller acquires `lock_key`; this generator always
    releases it. Membership is enforced by the query's JOIN, checked once here."""
    fn, value, terms = build_tsquery(raw_query, limits)
    started = time.monotonic()
    count = 0
    truncated = False
    yield ": ok\n\n"
    try:
        async with session_scope() as session:
            # SET LOCAL does not accept a bind parameter; the value is a
            # config-derived int, coerced here, never user input.
            await session.execute(text(f"SET LOCAL statement_timeout = {int(limits.statement_timeout_ms)}"))
            last_keepalive = time.monotonic()
            async for msg in crud.stream_global_messages(
                session, user_id=user_id, tsq_fn=fn, tsq_value=value, batch=limits.stream_batch
            ):
                if count >= limits.stream_max_results or (time.monotonic() - started) >= limits.stream_max_seconds:
                    truncated = True
                    break
                yield _sse("match", _to_result(msg, terms, limits.snippet_radius).model_dump(mode="json"))
                count += 1
                now = time.monotonic()
                if now - last_keepalive >= limits.stream_keepalive_seconds:
                    yield ": keepalive\n\n"
                    last_keepalive = now
            yield _sse("done", {"count": count, "truncated": truncated})
    except Exception:
        logger.exception("search stream failed for user %s", user_id)
        yield _sse("error", {"detail": "search stream failed"})
    finally:
        try:
            await redis_client.delete(lock_key)
        except Exception:
            logger.warning("failed to release search stream lock %s", lock_key)
