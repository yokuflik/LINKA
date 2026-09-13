"""REST endpoint for semantic vector search (ADR 0042).

GET /search/semantic?q=&limit=&chat_id=
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_current_user_id
from infra.db.connection import get_db
from infra.ratelimit import service as rate_limit_service
from modules.vector_search import service as vector_search_service
from modules.vector_search.limits import DEFAULT_VECTOR_SEARCH_LIMITS, VectorSearchLimits
from modules.vector_search.schemas import SemanticSearchResponseOut

vector_search_router = APIRouter(prefix="/search", tags=["vector-search"])


def get_vector_search_limits() -> VectorSearchLimits:
    """FastAPI dependency (ADR 0033). Tests override via app.dependency_overrides."""
    return DEFAULT_VECTOR_SEARCH_LIMITS


@vector_search_router.get("/semantic", response_model=SemanticSearchResponseOut)
async def search_semantic(
    q: str,
    request: Request,
    limit: int = 0,
    chat_id: Optional[int] = None,
    expanded: bool = False,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: VectorSearchLimits = Depends(get_vector_search_limits),
):
    await rate_limit_service.enforce_sliding_window(
        user_id, "vector_search", limits.rate_max, limits.rate_window_s
    )
    page = limits.default_limit if limit <= 0 else min(limit, limits.max_limit)
    return await vector_search_service.semantic_search(
        session, user_id=user_id, raw_query=q, chat_id=chat_id, limit=page, expanded=expanded, limits=limits
    )
