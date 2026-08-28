"""
The outgoing-message send queue's producer side: an XADD onto
``message_send_stream`` plus the consumer-group bootstrap.

Unlike services/receipts, an enqueue failure here is NOT swallowed. A dropped
receipt event only loses detailed history (the coarse watermark is still
written); a dropped send event loses the message itself while the sender
believes it sent. The caller must surface a synchronous error instead.
"""
import logging
from typing import Optional

from redis.exceptions import ResponseError

from config import (
    FANOUT_STREAM_SHARDS,
    MESSAGE_FANOUT_STREAM_GROUP,
    MESSAGE_FANOUT_STREAM_KEY,
    MESSAGE_FANOUT_STREAM_MAXLEN,
    MESSAGE_SEND_STREAM_GROUP,
    MESSAGE_SEND_STREAM_KEY,
    MESSAGE_SEND_STREAM_MAXLEN,
    SEND_STREAM_SHARDS,
)
from services.redis_client import redis_client

logger = logging.getLogger(__name__)


def _clean(value: Optional[object]) -> str:
    """Redis stream fields must be strings; None becomes '' and is read back as None."""
    return "" if value is None else str(value)


def shard_for_chat(chat_id: int, shards: int) -> int:
    """Which shard a chat's messages live on. All of a chat's traffic maps to
    one shard so per-chat order is preserved on a single consumer."""
    return chat_id % shards


def stream_key(base: str, shard: int) -> str:
    """Shard 0 keeps the bare key so an in-flight upgrade from the unsharded
    layout doesn't strand entries already sitting on ``base``."""
    return base if shard == 0 else f"{base}:{shard}"


def send_stream_keys() -> list[str]:
    return [stream_key(MESSAGE_SEND_STREAM_KEY, s) for s in range(SEND_STREAM_SHARDS)]


def fanout_stream_keys() -> list[str]:
    return [stream_key(MESSAGE_FANOUT_STREAM_KEY, s) for s in range(FANOUT_STREAM_SHARDS)]


async def enqueue_outgoing_message(
    *,
    chat_id: int,
    sender_id: int,
    client_message_id: str,
    content: Optional[str] = None,
    type: int = 1,
    reply_to_message_id: Optional[int] = None,
    media_key: Optional[str] = None,
    media_name: Optional[str] = None,
    media_duration_seconds: Optional[int] = None,
) -> str:
    """
    Append one outgoing message onto the send stream. Returns the stream
    entry id. Raises on failure - the caller returns a sync error to the
    sender (the message is otherwise silently lost).

    Media is passed as its raw parts rather than the dict the WS payload
    carries; the S3 HEAD validation happens in the worker.
    """
    key = stream_key(MESSAGE_SEND_STREAM_KEY, shard_for_chat(chat_id, SEND_STREAM_SHARDS))
    return await redis_client.xadd(
        key,
        {
            "chat_id": _clean(chat_id),
            "sender_id": _clean(sender_id),
            "client_message_id": _clean(client_message_id),
            "content": _clean(content),
            "type": _clean(type),
            "reply_to_message_id": _clean(reply_to_message_id),
            "media_key": _clean(media_key),
            "media_name": _clean(media_name),
            "media_duration_seconds": _clean(media_duration_seconds),
        },
        maxlen=MESSAGE_SEND_STREAM_MAXLEN,
        approximate=True,
    )


async def enqueue_fanout(
    *,
    message_id: int,
    chat_id: int,
    sender_id: Optional[int],
    client_message_id: Optional[str] = None,
) -> str:
    """
    Append a reference to an already-persisted message onto the fan-out
    stream. The fan-out worker loads the row and does the actual publish +
    push. Returns the stream entry id.

    Idempotent downstream: re-running fan-out just re-publishes (clients
    dedupe by message_id), so unlike enqueue_outgoing_message a failure here
    is recoverable - but we still raise so the send worker leaves its own
    entry unacked and the whole message is retried.
    """
    key = stream_key(MESSAGE_FANOUT_STREAM_KEY, shard_for_chat(chat_id, FANOUT_STREAM_SHARDS))
    return await redis_client.xadd(
        key,
        {
            "message_id": _clean(message_id),
            "chat_id": _clean(chat_id),
            "sender_id": _clean(sender_id),
            "client_message_id": _clean(client_message_id),
        },
        maxlen=MESSAGE_FANOUT_STREAM_MAXLEN,
        approximate=True,
    )


async def _ensure_group(stream_key: str, group: str) -> None:
    try:
        await redis_client.xgroup_create(stream_key, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def ensure_group() -> None:
    """Create the send-stream consumer group on every shard if absent. Idempotent."""
    for key in send_stream_keys():
        await _ensure_group(key, MESSAGE_SEND_STREAM_GROUP)


async def ensure_fanout_group() -> None:
    """Create the fan-out-stream consumer group on every shard if absent. Idempotent."""
    for key in fanout_stream_keys():
        await _ensure_group(key, MESSAGE_FANOUT_STREAM_GROUP)
