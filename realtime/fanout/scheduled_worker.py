"""
Poll-based background worker that fires scheduled messages (ADR 0026).

Unlike the send / fan-out workers this is NOT a Redis Stream consumer: volume
is tiny (bounded by SCHEDULED_MAX_PENDING_PER_USER per user) and a sorted-set
range query is the natural fit.

Loop, every SCHEDULED_POLL_INTERVAL_SECONDS:
  1. ``ZRANGEBYSCORE scheduled_messages:due -inf <now> LIMIT 0 BATCH`` - the
     ids whose fire time has passed.
  2. per id: ``ZREM`` (claim) -> load the row ``FOR UPDATE SKIP LOCKED``, bail
     unless still pending -> re-check the sender is still a participant ->
     ``send_queue.enqueue_outgoing_message`` with the stored ``client_message_id``
     (so a crash/retry can't double-send) -> mark the row sent + emit
     ``scheduled_message_sent`` on the sender's user channel.
  3. transient failure (DB/Redis) -> bump ``fire_attempts``; below
     SCHEDULED_MAX_FIRE_ATTEMPTS re-``ZADD`` with a short backoff, otherwise
     mark ``failed`` + emit ``scheduled_message_failed``.

Postgres is the source of truth: on startup and every
SCHEDULED_RECONCILE_INTERVAL_SECONDS a reconcile scan re-``ZADD``s every
pending row, self-healing a Redis flush, a missed add, or a fire time that
elapsed during downtime (an overdue row is simply "due" and fires next poll).

``drain_once`` / ``reconcile_once`` are exposed for tests.
"""
import asyncio
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    SCHEDULED_DUE_SET_KEY,
    SCHEDULED_FIRE_BACKOFF_SECONDS,
    SCHEDULED_MAX_FIRE_ATTEMPTS,
    SCHEDULED_POLL_INTERVAL_SECONDS,
    SCHEDULED_RECONCILE_INTERVAL_SECONDS,
    SCHEDULED_WORKER_BATCH,
)
from infra.db.connection import session_scope
from infra.redis.client import redis_client
from modules.chats.crud.crud_participant import is_participant
from modules.messaging import crud_scheduled
from modules.messaging.models import ScheduledMessageStatus
from realtime import realtime_service
from realtime.fanout import send_queue

logger = logging.getLogger(__name__)


def _now_epoch() -> float:
    return time.time()


async def _emit(sender_id: int, event: dict) -> None:
    try:
        await realtime_service.publish_user_event(sender_id, event)
    except Exception as exc:  # noqa: BLE001 - a lost notification isn't fatal
        logger.warning("scheduled: user-event publish failed for %s: %s", sender_id, exc)


async def _fire_one(session: AsyncSession, scheduled_message_id: int) -> None:
    """
    Fire a single claimed id. Runs in its own transaction (the caller commits
    via session_scope). Raises on a transient failure so the caller can
    re-queue with a backoff; handles the permanent cases (row gone / not
    pending / sender left) internally.
    """
    row = await crud_scheduled.get_scheduled_for_update(session, scheduled_message_id)
    if row is None or row.status != ScheduledMessageStatus.PENDING:
        return  # cancelled / already sent / chat deleted - nothing to do

    if not await is_participant(session, row.chat_id, row.sender_id):
        await crud_scheduled.set_status(
            session,
            row.id,
            ScheduledMessageStatus.FAILED,
            last_error="no longer a participant",
            commit=False,
        )
        await _emit(
            row.sender_id,
            {
                "event": "scheduled_message_failed",
                "id": str(row.id),
                "chat_id": str(row.chat_id),
                "reason": "no longer a participant",
            },
        )
        return

    # Same entry point _handle_send_message uses - identical idempotency
    # (reused client_message_id), media HEAD-validation, fan-out, receipts,
    # push. A worker crash after this XADD but before set_status re-fires the
    # same client_message_id -> process_outgoing raises MessageAlreadySentError
    # downstream, so no second message is written.
    await send_queue.enqueue_outgoing_message(
        chat_id=row.chat_id,
        sender_id=row.sender_id,
        client_message_id=row.client_message_id,
        content=row.content,
        type=row.type,
        reply_to_message_id=row.reply_to_message_id,
        media_key=row.media_key,
        media_name=row.media_name,
        media_duration_seconds=row.media_duration_seconds,
        media_blur_hash=row.media_blur_hash,
    )

    await crud_scheduled.set_status(
        session, row.id, ScheduledMessageStatus.SENT, commit=False
    )
    await _emit(
        row.sender_id,
        {
            "event": "scheduled_message_sent",
            "id": str(row.id),
            "chat_id": str(row.chat_id),
        },
    )


async def _handle_transient_failure(scheduled_message_id: int) -> None:
    """A DB/Redis blip fired an id but couldn't complete it. Bump the attempt
    counter; re-queue with a backoff or give up."""
    async with session_scope() as session:
        attempts = await crud_scheduled.bump_fire_attempts(session, scheduled_message_id)

    if attempts >= SCHEDULED_MAX_FIRE_ATTEMPTS:
        async with session_scope() as session:
            row = await crud_scheduled.get_scheduled_by_id(session, scheduled_message_id)
            if row is None or row.status != ScheduledMessageStatus.PENDING:
                return
            await crud_scheduled.set_status(
                session,
                scheduled_message_id,
                ScheduledMessageStatus.FAILED,
                last_error="fire failed after retries",
                commit=False,
            )
            await _emit(
                row.sender_id,
                {
                    "event": "scheduled_message_failed",
                    "id": str(row.id),
                    "chat_id": str(row.chat_id),
                    "reason": "delivery failed",
                },
            )
            await session.commit()
        return

    # Re-queue with a backoff so the next poll retries it.
    try:
        await redis_client.zadd(
            SCHEDULED_DUE_SET_KEY,
            {str(scheduled_message_id): _now_epoch() + SCHEDULED_FIRE_BACKOFF_SECONDS},
        )
    except Exception as exc:  # noqa: BLE001 - reconcile will re-add it anyway
        logger.warning("scheduled: backoff re-ZADD failed for %s: %s", scheduled_message_id, exc)


async def drain_once(*, batch: int = SCHEDULED_WORKER_BATCH) -> int:
    """
    Fire every due scheduled message. Returns how many ids were claimed.
    Safe to call from tests.
    """
    now = _now_epoch()
    due = await redis_client.zrangebyscore(
        SCHEDULED_DUE_SET_KEY, min="-inf", max=now, start=0, num=batch
    )
    fired = 0
    for raw_id in due:
        scheduled_message_id = int(raw_id)
        # Claim first so two processes racing this tick don't both fire it.
        await redis_client.zrem(SCHEDULED_DUE_SET_KEY, raw_id)
        fired += 1
        try:
            async with session_scope() as session:
                await _fire_one(session, scheduled_message_id)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - transient: re-queue with backoff
            logger.warning(
                "scheduled: fire failed for %s (%s); will retry",
                scheduled_message_id, exc,
            )
            await _handle_transient_failure(scheduled_message_id)
    return fired


async def reconcile_once() -> int:
    """
    Rebuild the Redis due-set from Postgres (the source of truth). Returns the
    number of pending rows re-added. Run on startup and periodically.
    """
    async with session_scope() as session:
        rows = await crud_scheduled.list_all_pending(session)
    if not rows:
        return 0
    mapping = {str(r.id): _epoch_of(r.scheduled_for) for r in rows}
    try:
        await redis_client.zadd(SCHEDULED_DUE_SET_KEY, mapping)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduled: reconcile ZADD failed: %s", exc)
        return 0
    return len(mapping)


def _epoch_of(dt) -> float:
    from datetime import timezone

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Poll loop + periodic reconcile. One task per app process."""
    try:
        await reconcile_once()
    except Exception:
        logger.exception("scheduled: initial reconcile failed")

    last_reconcile = _now_epoch()
    while stop_event is None or not stop_event.is_set():
        try:
            await drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled: drain_once failed")

        if _now_epoch() - last_reconcile >= SCHEDULED_RECONCILE_INTERVAL_SECONDS:
            try:
                await reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduled: periodic reconcile failed")
            last_reconcile = _now_epoch()

        try:
            await asyncio.sleep(SCHEDULED_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
