"""
Read queries over message_receipt_log, for the per-message "info" view
(when did each person read this / who in this group has played it).

None of this is on any hot path - it runs only when a user explicitly opens
the details of one message. The per-bubble tick and the chat list still use
the O(1) watermark rollup on Chat and never touch this table.
"""
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.message_receipt_log import MessageReceiptLog


async def crossers_for_message(
    session: AsyncSession,
    chat_id: int,
    kind: int,
    message_id: int,
) -> list[tuple[int, datetime]]:
    """
    For every user whose `kind` watermark in `chat_id` has reached or passed
    `message_id`, the (user_id, occurred_at) of the *earliest* row that did
    so - i.e. the moment their watermark crossed this message.

    Ordering by up_to_message_id asc picks that first crossing; DISTINCT ON
    collapses each user to it. Bounded by the chat's participant count, via
    ix_receipt_log_chat_kind_upto - independent of history length.

    The caller is responsible for intersecting the result with the chat's
    *current* participants (a row survives a participant leaving).
    """
    stmt = (
        select(MessageReceiptLog.user_id, MessageReceiptLog.occurred_at)
        .where(
            MessageReceiptLog.chat_id == chat_id,
            MessageReceiptLog.kind == kind,
            MessageReceiptLog.up_to_message_id >= message_id,
        )
        .order_by(
            MessageReceiptLog.user_id,
            MessageReceiptLog.up_to_message_id.asc(),
            MessageReceiptLog.id.asc(),
        )
        .distinct(MessageReceiptLog.user_id)
    )
    result = await session.execute(stmt)
    return [(row.user_id, row.occurred_at) for row in result]


async def crosser_count_for_message(
    session: AsyncSession,
    chat_id: int,
    kind: int,
    message_id: int,
    user_ids: Optional[Sequence[int]] = None,
) -> int:
    """
    Same predicate as crossers_for_message but only the count of distinct
    users - for large groups where a per-member list is not returned.
    Optionally restricted to `user_ids` (the chat's current participants).
    """
    stmt = (
        select(func.count(func.distinct(MessageReceiptLog.user_id)))
        .where(
            MessageReceiptLog.chat_id == chat_id,
            MessageReceiptLog.kind == kind,
            MessageReceiptLog.up_to_message_id >= message_id,
        )
    )
    if user_ids is not None:
        stmt = stmt.where(MessageReceiptLog.user_id.in_(list(user_ids)))
    return (await session.execute(stmt)).scalar_one()


async def my_receipt_history(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
) -> Sequence[MessageReceiptLog]:
    """One user's own receipt rows in a chat, newest first - a "my activity"
    view. Uses ix_receipt_log_chat_user_kind_id."""
    limit = min(limit, 100)
    stmt = select(MessageReceiptLog).where(
        MessageReceiptLog.chat_id == chat_id,
        MessageReceiptLog.user_id == user_id,
    )
    if before_id is not None:
        stmt = stmt.where(MessageReceiptLog.id < before_id)
    stmt = stmt.order_by(MessageReceiptLog.id.desc()).limit(limit)
    return (await session.execute(stmt)).scalars().all()
