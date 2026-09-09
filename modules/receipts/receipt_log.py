"""
The detailed per-user receipt log's write path.

Nothing here is on the hot path of sending or acknowledging a message: the
watermark columns on Participant/Chat (see crud_participant) still carry the
sent/delivered/read/played tick with O(1) reads and one small update per
acknowledgement. This module is purely the *history* - "when did U read X",
"who in this group played X" - and it is written asynchronously:

  mark_as_read()  ->  enqueue_receipt_event()  ->  Redis Stream (XADD)
                                                        |
                              services/receipts/worker.py drains it
                                                        |
                      collapse per (chat, user, kind)  ->  one multi-row INSERT

So a 1000-member group opening a chat produces ~1000 tiny XADDs that the
worker folds into a single INSERT, instead of 1000 transactions against a
partitioned, billion-row-scale table.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.receipts.apply import apply_receipt
from modules.receipts.models import MessageReceiptLog
from realtime import realtime_service
from infra.redis.client import redis_client
# Local generator on purpose: _rows_from_entries() is sync and these ids are
# message_receipt_log PKs, whose row is placed by occurred_at (carried in the
# stream entry), not by the id timestamp - see ADR 0011.
from infra.ids.snowflake import next_id

logger = logging.getLogger(__name__)

# One consumer name per process. The consumer *group* is shared across every
# app instance, so the stream's load is split between them automatically.
_CONSUMER_NAME = f"receipt-worker-{settings.SERVER_ID}"


async def enqueue_receipt_event(
    chat_id: int,
    user_id: int,
    kind: int,
    up_to_message_id: int,
    occurred_at: Optional[datetime] = None,
) -> None:
    """
    Record that `user_id`'s `kind` watermark in `chat_id` advanced to
    `up_to_message_id` at `occurred_at`. Fire-and-forget: an XADD onto the
    stream, drained into Postgres later by the worker. A Redis hiccup here
    must never fail the acknowledgement it accompanies - callers log and move
    on (the coarse Participant.last_*_at column is still written synchronously
    by the crud layer, so nothing is fully lost).
    """
    occurred_at = occurred_at or datetime.now(timezone.utc)
    await redis_client.xadd(
        settings.RECEIPT_STREAM_KEY,
        {
            "chat_id": str(chat_id),
            "user_id": str(user_id),
            "kind": str(kind),
            "up_to_message_id": str(up_to_message_id),
            "occurred_at": occurred_at.isoformat(),
        },
        maxlen=settings.RECEIPT_STREAM_MAXLEN,
        approximate=True,
    )


async def ensure_group() -> None:
    """Create the consumer group (and the stream) if absent. Idempotent."""
    try:
        await redis_client.xgroup_create(
            settings.RECEIPT_STREAM_KEY, settings.RECEIPT_STREAM_GROUP, id="0", mkstream=True
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _collapse(entries: list) -> tuple[dict[tuple[int, int, int], tuple[int, datetime]], list]:
    """
    Fold a batch of raw stream entries into at most one (up_to, occurred_at)
    per (chat_id, user_id, kind), keeping the furthest watermark. Returns
    (collapsed, entry_ids_to_ack).

    Collapsing here is what bounds write amplification: a client that sent
    ten mark_read frames while scrolling becomes one apply + one row.
    """
    collapsed: dict[tuple[int, int, int], tuple[int, datetime]] = {}
    ack_ids: list = []

    for entry_id, fields in entries:
        ack_ids.append(entry_id)
        key = (int(fields["chat_id"]), int(fields["user_id"]), int(fields["kind"]))
        up_to = int(fields["up_to_message_id"])
        occurred_at = datetime.fromisoformat(fields["occurred_at"])
        current = collapsed.get(key)
        if current is None or up_to > current[0]:
            collapsed[key] = (up_to, occurred_at)

    return collapsed, ack_ids


async def _claim_stale(count: int) -> list:
    """
    Reclaim entries a crashed worker read but never acked. Best-effort:
    returns the claimed (id, fields) pairs, or [] if there are none / the
    Redis server is too old for XAUTOCLAIM.
    """
    try:
        result = await redis_client.xautoclaim(
            settings.RECEIPT_STREAM_KEY,
            settings.RECEIPT_STREAM_GROUP,
            _CONSUMER_NAME,
            min_idle_time=settings.RECEIPT_STREAM_CLAIM_IDLE_MS,
            count=count,
        )
    except ResponseError as exc:
        logger.warning("XAUTOCLAIM unavailable, skipping stale-entry reclaim: %s", exc)
        return []
    # redis-py returns (next_cursor, claimed, deleted) on newer versions,
    # (next_cursor, claimed) on older ones.
    claimed = result[1] if len(result) >= 2 else []
    return [(entry_id, fields) for entry_id, fields in claimed if fields]


async def drain_once(
    session: AsyncSession,
    *,
    block_ms: int = 0,
    count: Optional[int] = None,
    claim_stale: bool = True,
) -> int:
    """
    Read one batch, apply each collapsed watermark (ADR 0037), write the
    detailed-log rows for the ones that advanced, publish their live receipt
    events, ack the batch. Returns the number of rows inserted.

    Exposed directly (not just run from the worker loop) so tests can drive
    the drain deterministically right after enqueuing.
    """
    count = count or settings.RECEIPT_WORKER_BATCH

    response = await redis_client.xreadgroup(
        settings.RECEIPT_STREAM_GROUP,
        _CONSUMER_NAME,
        {settings.RECEIPT_STREAM_KEY: ">"},
        count=count,
        block=block_ms or None,
    )
    entries: list = []
    if response:
        # [(stream_key, [(id, {field: value}), ...])]
        entries = list(response[0][1])

    if claim_stale and len(entries) < count:
        entries.extend(await _claim_stale(count - len(entries)))

    if not entries:
        return 0

    collapsed, ack_ids = _collapse(entries)

    rows: list[MessageReceiptLog] = []
    events: list[tuple[int, dict]] = []  # (chat_id, event payload) to publish after commit
    for (chat_id, user_id, kind), (up_to, occurred_at) in collapsed.items():
        # A transient failure (DB blip) propagates: the batch is not acked and
        # XAUTOCLAIM redelivers it. apply_receipt is idempotent so re-running
        # the keys that already succeeded is harmless.
        outcome = await apply_receipt(session, chat_id, user_id, kind, up_to, occurred_at)
        if outcome is None:
            continue
        rows.append(
            MessageReceiptLog(
                id=next_id(),
                chat_id=chat_id,
                user_id=user_id,
                kind=kind,
                up_to_message_id=up_to,
                occurred_at=occurred_at,
            )
        )
        if outcome.event is not None:
            events.append((chat_id, outcome.event))

    if rows:
        session.add_all(rows)
        await session.commit()

    for chat_id, event in events:
        try:
            await realtime_service.publish_event(chat_id, event)
        except Exception:
            logger.exception("receipt publish_event failed (chat=%s event=%s)", chat_id, event.get("event"))

    if ack_ids:
        await redis_client.xack(settings.RECEIPT_STREAM_KEY, settings.RECEIPT_STREAM_GROUP, *ack_ids)
    return len(rows)
