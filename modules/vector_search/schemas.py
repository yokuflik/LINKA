"""Semantic search API models (ADR 0030 / ADR 0042)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from api.schemas import IdStr


class SemanticSearchResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    chat_id: IdStr
    sender_id: Optional[IdStr]
    type: int
    content: Optional[str]
    # Cosine distance (0 = identical direction, 2 = opposite). Exposed so the
    # client can show a relevance cue; not a percentage.
    distance: float
    created_at: datetime


class SemanticSearchResponseOut(BaseModel):
    results: list[SemanticSearchResultOut]


__all__ = ["SemanticSearchResultOut", "SemanticSearchResponseOut"]
