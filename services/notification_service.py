import logging
from typing import Any, Optional

from services.redis_client import redis_client

logger = logging.getLogger(__name__)

_DEVICE_TOKENS_KEY_PREFIX = "device_tokens:"


def _device_tokens_key(user_id: int) -> str:
    return f"{_DEVICE_TOKENS_KEY_PREFIX}{user_id}"


async def register_device_token(user_id: int, fcm_token: str) -> None:
    """A user can have multiple devices, so tokens accumulate in a set."""
    await redis_client.sadd(_device_tokens_key(user_id), fcm_token)


async def unregister_device_token(user_id: int, fcm_token: str) -> None:
    await redis_client.srem(_device_tokens_key(user_id), fcm_token)


async def send_push(user_id: int, title: str, body: str, data: Optional[dict[str, Any]] = None) -> None:
    """
    Called by message_service only for a recipient with no live connection
    anywhere (checked via presence_service) - a connected recipient gets the
    real-time fanout instead, not a redundant push.

    NOTE: no FCM credentials are wired up yet - delivery is stubbed to a log
    line. Swap `_deliver` for a real `firebase_admin.messaging.send()` call
    later; nothing else in this module or its callers needs to change.
    """
    tokens = await redis_client.smembers(_device_tokens_key(user_id))
    if not tokens:
        logger.info(f"No registered device for user {user_id}, dropping push")
        return

    for token in tokens:
        await _deliver(token, title, body, data or {})


async def _deliver(fcm_token: str, title: str, body: str, data: dict[str, Any]) -> None:
    logger.info(f"[STUB] Would push to {fcm_token}: {title} - {body} ({data})")
