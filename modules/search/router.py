"""REST + SSE endpoints for message search (ADR 0040).

- GET /chats/{chat_id}/messages/search        - in-chat, cursor paginated
- GET /chats/{chat_id}/messages/around/{id}   - jump-to-context window
- GET /search/messages                        - global, cursor paginated (default)
- GET /search/messages/stream                 - global, SSE (huge result sets)
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_current_user_id
from infra.db.connection import get_db
from infra.ratelimit import service as rate_limit_service
from infra.ratelimit.service import RateLimited
from infra.redis.client import redis_client
from modules.messaging.schemas import MessageOut
from modules.search import service as search_service
from modules.search.errors import SearchStreamBusyError
from modules.search.limits import DEFAULT_SEARCH_LIMITS, SearchLimits
from modules.search.schemas import SearchResponseOut

chat_search_router = APIRouter(prefix="/chats/{chat_id}/messages", tags=["search"])
search_router = APIRouter(prefix="/search", tags=["search"])


def get_search_limits() -> SearchLimits:
    """FastAPI dependency (ADR 0033). Tests override via
    app.dependency_overrides[get_search_limits]."""
    return DEFAULT_SEARCH_LIMITS


def _clamp_page(limit: int, limits: SearchLimits) -> int:
    if limit <= 0:
        return limits.default_page_size
    return min(max(1, limit), limits.max_page_size)


async def _enforce_query_limits(user_id: int, request: Request, limits: SearchLimits) -> None:
    """Two-tier per-user sliding window + a shared per-IP ceiling. Protects the
    DB from an as-you-type client that fires on every keystroke."""
    await rate_limit_service.enforce_sliding_window(
        user_id, "search_query", limits.query_rate_max, limits.query_rate_window_s
    )
    await rate_limit_service.enforce_sliding_window(
        user_id, "search_query_burst", limits.query_burst_max, limits.query_burst_window_s
    )
    ip = rate_limit_service.client_ip(request)
    if not await rate_limit_service.check_and_increment(
        ip, "search_ip", limits.ip_rate_max, limits.ip_rate_window_s
    ):
        raise RateLimited("search_ip", retry_after=limits.ip_rate_window_s)


@chat_search_router.get("/search", response_model=SearchResponseOut)
async def search_in_chat(
    chat_id: int,
    q: str,
    request: Request,
    cursor: Optional[str] = None,
    limit: int = 0,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: SearchLimits = Depends(get_search_limits),
):
    await _enforce_query_limits(user_id, request, limits)
    return await search_service.search_in_chat(
        session,
        user_id=user_id,
        chat_id=chat_id,
        raw_query=q,
        cursor=cursor,
        limit=_clamp_page(limit, limits),
        limits=limits,
    )


@chat_search_router.get("/around/{message_id}", response_model=list[MessageOut])
async def messages_around(
    chat_id: int,
    message_id: int,
    request: Request,
    radius: int = 0,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: SearchLimits = Depends(get_search_limits),
):
    """Load a message in its surrounding context (the history endpoint only
    pages backwards). Used to open a search hit in its chat."""
    await _enforce_query_limits(user_id, request, limits)
    radius = limits.around_default_radius if radius <= 0 else min(radius, limits.around_max_radius)
    return await search_service.messages_around(
        session, user_id=user_id, chat_id=chat_id, message_id=message_id, radius=radius
    )


@search_router.get("/messages", response_model=SearchResponseOut)
async def search_messages(
    q: str,
    request: Request,
    cursor: Optional[str] = None,
    limit: int = 0,
    chat_id: Optional[int] = None,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: SearchLimits = Depends(get_search_limits),
):
    await _enforce_query_limits(user_id, request, limits)
    page = _clamp_page(limit, limits)
    if chat_id is not None:
        return await search_service.search_in_chat(
            session, user_id=user_id, chat_id=chat_id, raw_query=q, cursor=cursor, limit=page, limits=limits
        )
    return await search_service.search_global(
        session, user_id=user_id, raw_query=q, cursor=cursor, limit=page, limits=limits
    )


@search_router.get("/messages/stream")
async def stream_search_messages(
    q: str,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    limits: SearchLimits = Depends(get_search_limits),
):
    """Global search as Server-Sent Events. For a result set too large to buffer
    in one JSON body / in the single app process: rows stream off a server-side
    cursor, capped by row count and wall-clock. One in-flight stream per user."""
    ip = rate_limit_service.client_ip(request)
    await rate_limit_service.enforce_sliding_window(
        user_id, "search_stream", limits.stream_rate_max, limits.stream_rate_window_s
    )
    if not await rate_limit_service.check_and_increment(
        ip, "search_ip", limits.ip_rate_max, limits.ip_rate_window_s
    ):
        raise RateLimited("search_ip", retry_after=limits.ip_rate_window_s)
    # Validate the query now so a bad one is a clean 422, not a 200 that opens a
    # stream and immediately errors.
    search_service.build_tsquery(q, limits)

    lock_key = search_service.stream_lock_key(user_id)
    if not await redis_client.set(lock_key, "1", nx=True, ex=limits.stream_lock_ttl_seconds):
        raise SearchStreamBusyError("a search stream is already running for this user")

    return StreamingResponse(
        search_service.stream_search(user_id=user_id, raw_query=q, lock_key=lock_key, limits=limits),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
