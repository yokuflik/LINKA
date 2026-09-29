"""FTS queries for message search (ADR 0040).

All queries carry the same three predicates as the partial GIN index
(`deleted_at IS NULL AND purged_at IS NULL AND sender_id IS NOT NULL`) so the
index is always usable. `messages_around` is the exception - it keeps
soft-deleted rows so the jump-to-context view can render a tombstone in place.
"""

from datetime import datetime, timedelta
from typing import AsyncIterator, Optional, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ids.snowflake import id_to_datetime
from modules.chats.models.participant import Participant
from modules.messaging.models import Message

# Same id -> created_at slack as crud_message: the predicate is always a safe
# superset, so it prunes partitions without ever dropping a matching row.
_SKEW = timedelta(hours=settings.MESSAGE_PARTITION_QUERY_SKEW_HOURS)


def _tsquery(fn_name: str, value: str):
    # fn_name is "to_tsquery" or "websearch_to_tsquery" - chosen by the service
    # from the shape of the raw query, never from user input directly.
    return getattr(func, fn_name)("simple", value)


def _match_conditions(tsq):
    return (
        Message.content_tsv.op("@@")(tsq),
        Message.deleted_at.is_(None),
        Message.purged_at.is_(None),
        Message.sender_id.is_not(None),
    )


def _date_range_conditions(start_at: Optional[datetime], end_at: Optional[datetime]):
    """ADR 0068 (extended for time-of-day, ADR 0070): inclusive [start_at,
    end_at] on `created_at`, both ends optional and independent, full
    timestamp precision (not just calendar dates)."""
    conditions = []
    if start_at is not None:
        conditions.append(Message.created_at >= start_at)
    if end_at is not None:
        conditions.append(Message.created_at <= end_at)
    return conditions


async def search_chat_messages(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    tsq_fn: str,
    tsq_value: str,
    before_id: Optional[int],
    limit: int,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
) -> Sequence[Message]:
    """In-chat search. `chat_id = :chat_id AND content_tsv @@ :q` seeks straight
    into the composite `gin (chat_id, content_tsv)` index (btree_gin).
    `user_id` membership is enforced via a participants JOIN (same hard
    guarantee as search_global_messages/semantic_search_messages) so a
    wrong/guessed chat_id structurally returns zero rows, on top of - not
    instead of - the caller's own is_participant pre-check."""
    tsq = _tsquery(tsq_fn, tsq_value)
    stmt = (
        select(Message)
        .join(Participant, and_(Participant.chat_id == Message.chat_id, Participant.user_id == user_id))
        .where(Message.chat_id == chat_id, *_match_conditions(tsq), *_date_range_conditions(start_at, end_at))
    )
    if before_id is not None:
        stmt = stmt.where(
            Message.id < before_id,
            Message.created_at <= id_to_datetime(before_id) + _SKEW,
        )
    stmt = stmt.order_by(Message.id.desc()).limit(limit + 1)
    return (await session.execute(stmt)).scalars().all()


def _global_stmt(
    user_id: int,
    tsq,
    chat_ids: Optional[Sequence[int]],
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
):
    stmt = (
        select(Message)
        .join(
            Participant,
            and_(Participant.chat_id == Message.chat_id, Participant.user_id == user_id),
        )
        .where(*_match_conditions(tsq), *_date_range_conditions(start_at, end_at))
    )
    if chat_ids is not None:
        # Planner hint only - the JOIN already enforces membership. Passed by the
        # service when the caller is in few enough chats to inline.
        stmt = stmt.where(Message.chat_id.in_(chat_ids))
    return stmt


async def search_global_messages(
    session: AsyncSession,
    *,
    user_id: int,
    tsq_fn: str,
    tsq_value: str,
    before_id: Optional[int],
    limit: int,
    chat_ids: Optional[Sequence[int]] = None,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
) -> Sequence[Message]:
    """Global search. The `participants` JOIN enforces *current* membership in
    the query itself - a removed member's chats drop out immediately."""
    tsq = _tsquery(tsq_fn, tsq_value)
    stmt = _global_stmt(user_id, tsq, chat_ids, start_at, end_at)
    if before_id is not None:
        stmt = stmt.where(
            Message.id < before_id,
            Message.created_at <= id_to_datetime(before_id) + _SKEW,
        )
    stmt = stmt.order_by(Message.id.desc()).limit(limit + 1)
    return (await session.execute(stmt)).scalars().all()


async def stream_global_messages(
    session: AsyncSession,
    *,
    user_id: int,
    tsq_fn: str,
    tsq_value: str,
    batch: int,
    chat_ids: Optional[Sequence[int]] = None,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
) -> AsyncIterator[Message]:
    """Same as `search_global_messages` but over a server-side cursor: rows are
    fetched `batch` at a time so the app process never holds the whole result
    set. The caller (service) enforces the row / wall-clock caps."""
    tsq = _tsquery(tsq_fn, tsq_value)
    stmt = _global_stmt(user_id, tsq, chat_ids, start_at, end_at).order_by(Message.id.desc())
    result = await session.stream_scalars(stmt.execution_options(yield_per=batch))
    async for msg in result:
        yield msg


async def messages_around(
    session: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    radius: int,
) -> Sequence[Message]:
    """`radius` messages before + the target + `radius` after, oldest first.
    Keeps soft-deleted rows (rendered as a tombstone), like `get_message_history`.
    The existing history endpoint only pages backwards, hence this helper."""
    at = id_to_datetime(message_id)
    older = (
        select(Message)
        .where(
            Message.chat_id == chat_id,
            Message.id <= message_id,
            Message.created_at <= at + _SKEW,
        )
        .order_by(Message.id.desc())
        .limit(radius + 1)
    )
    newer = (
        select(Message)
        .where(
            Message.chat_id == chat_id,
            Message.id > message_id,
            Message.created_at >= at - _SKEW,
        )
        .order_by(Message.id.asc())
        .limit(radius)
    )
    rows = [*(await session.execute(older)).scalars().all(), *(await session.execute(newer)).scalars().all()]
    rows.sort(key=lambda m: m.id)
    return rows
