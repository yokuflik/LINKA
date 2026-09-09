"""
CRUD for `scheduled_messages` (ADR 0031).

Low-volume, unpartitioned table. Postgres is the source of truth for the poll
worker; the Redis `scheduled_messages:due` ZSET is only a fast index rebuilt by
the reconcile scan (see realtime/fanout/scheduled_worker.py).
"""
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from modules.messaging.models import ScheduledMessage, ScheduledMessageStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_scheduled(
    session: AsyncSession,
    *,
    scheduled_message_id: int,
    chat_id: int,
    sender_id: int,
    scheduled_for: datetime,
    client_message_id: str,
    type: int = 1,
    content: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    media_key: Optional[str] = None,
    media_mime: Optional[str] = None,
    media_size: Optional[int] = None,
    media_name: Optional[str] = None,
    media_duration_seconds: Optional[int] = None,
    media_blur_hash: Optional[str] = None,
) -> ScheduledMessage:
    """Insert a pending scheduled message and commit. O(log N)."""
    row = ScheduledMessage(
        id=scheduled_message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        scheduled_for=scheduled_for,
        client_message_id=client_message_id,
        type=type,
        content=content,
        reply_to_message_id=reply_to_message_id,
        media_key=media_key,
        media_mime=media_mime,
        media_size=media_size,
        media_name=media_name,
        media_duration_seconds=media_duration_seconds,
        media_blur_hash=media_blur_hash,
        status=ScheduledMessageStatus.PENDING,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_scheduled_by_id(
    session: AsyncSession, scheduled_message_id: int
) -> Optional[ScheduledMessage]:
    """Fetch one row by id (any status). O(log N)."""
    return await session.get(ScheduledMessage, scheduled_message_id)


async def get_scheduled_for_update(
    session: AsyncSession, scheduled_message_id: int
) -> Optional[ScheduledMessage]:
    """
    Fetch one row with `FOR UPDATE SKIP LOCKED` - the worker's claim path, so
    two processes racing the same tick don't both fire it.
    """
    stmt = (
        select(ScheduledMessage)
        .where(ScheduledMessage.id == scheduled_message_id)
        .with_for_update(skip_locked=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_pending_for_user(
    session: AsyncSession,
    sender_id: int,
    chat_id: Optional[int] = None,
) -> Sequence[ScheduledMessage]:
    """Pending rows for a user, oldest fire time first. Optional chat filter."""
    stmt = select(ScheduledMessage).where(
        ScheduledMessage.sender_id == sender_id,
        ScheduledMessage.status == ScheduledMessageStatus.PENDING,
    )
    if chat_id is not None:
        stmt = stmt.where(ScheduledMessage.chat_id == chat_id)
    stmt = stmt.order_by(ScheduledMessage.scheduled_for.asc())
    return (await session.execute(stmt)).scalars().all()


async def count_pending_for_user(session: AsyncSession, sender_id: int) -> int:
    """Number of pending scheduled messages a user currently has."""
    stmt = select(func.count()).select_from(ScheduledMessage).where(
        ScheduledMessage.sender_id == sender_id,
        ScheduledMessage.status == ScheduledMessageStatus.PENDING,
    )
    return int((await session.execute(stmt)).scalar_one())


async def list_all_pending(session: AsyncSession) -> Sequence[ScheduledMessage]:
    """
    Every pending row (id + scheduled_for), for the worker's reconcile scan
    that rebuilds the Redis due-set.
    """
    stmt = select(ScheduledMessage).where(
        ScheduledMessage.status == ScheduledMessageStatus.PENDING
    )
    return (await session.execute(stmt)).scalars().all()


async def update_scheduled(
    session: AsyncSession,
    scheduled_message_id: int,
    sender_id: int,
    *,
    scheduled_for: Optional[datetime] = None,
    content: Optional[str] = None,
    set_content: bool = False,
) -> Optional[ScheduledMessage]:
    """
    Reschedule / edit the caption of a pending row owned by `sender_id`.
    `set_content=True` applies `content` (which may be None to clear it).
    Returns the updated row, or None if it was not pending / not owned.
    """
    values: dict = {"updated_at": _utcnow()}
    if scheduled_for is not None:
        values["scheduled_for"] = scheduled_for
    if set_content:
        values["content"] = content

    stmt = (
        update(ScheduledMessage)
        .where(
            ScheduledMessage.id == scheduled_message_id,
            ScheduledMessage.sender_id == sender_id,
            ScheduledMessage.status == ScheduledMessageStatus.PENDING,
        )
        .values(**values)
        .returning(ScheduledMessage)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()
    return row


async def cancel_scheduled(
    session: AsyncSession, scheduled_message_id: int, sender_id: int
) -> Optional[ScheduledMessage]:
    """
    Mark a pending row owned by `sender_id` as cancelled. Returns the row, or
    None if it was not pending / not owned.
    """
    stmt = (
        update(ScheduledMessage)
        .where(
            ScheduledMessage.id == scheduled_message_id,
            ScheduledMessage.sender_id == sender_id,
            ScheduledMessage.status == ScheduledMessageStatus.PENDING,
        )
        .values(status=ScheduledMessageStatus.CANCELLED, updated_at=_utcnow())
        .returning(ScheduledMessage)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()
    return row


async def set_status(
    session: AsyncSession,
    scheduled_message_id: int,
    status: ScheduledMessageStatus,
    *,
    last_error: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Worker path: stamp a terminal (or back-to-pending) status."""
    values: dict = {"status": int(status), "updated_at": _utcnow()}
    if last_error is not None:
        values["last_error"] = last_error
    await session.execute(
        update(ScheduledMessage)
        .where(ScheduledMessage.id == scheduled_message_id)
        .values(**values)
    )
    if commit:
        await session.commit()


async def bump_fire_attempts(
    session: AsyncSession, scheduled_message_id: int, *, commit: bool = True
) -> int:
    """
    Increment the transient-failure counter and return the new value, so the
    worker can decide between another retry and marking the row failed.
    """
    stmt = (
        update(ScheduledMessage)
        .where(ScheduledMessage.id == scheduled_message_id)
        .values(
            fire_attempts=ScheduledMessage.fire_attempts + 1,
            updated_at=_utcnow(),
        )
        .returning(ScheduledMessage.fire_attempts)
    )
    new_value = (await session.execute(stmt)).scalar_one()
    if commit:
        await session.commit()
    return int(new_value)
