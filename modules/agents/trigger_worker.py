"""Consumer for agent_trigger_stream (ADR 0106).

Runs inside the `agent_worker` process. Loads the persisted message and runs the
unchanged Trigger Rule Engine. At-most-once on purpose: evaluate_triggers was
fire-and-forget (errors logged, never raised) and a redelivery would
double-increment the activation quota, so failures are logged and the entry is
acked.
"""
import asyncio
import logging

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.redis.client import redis_client
from modules.agents.trigger_engine import _INFRA_ERRORS, _evaluate_triggers
from modules.messaging.crud import get_message_by_id
from realtime.fanout.base_worker import BaseStreamConsumer

logger = logging.getLogger(__name__)


class AgentTriggerConsumer(BaseStreamConsumer):
    name = "agent-trigger-worker"
    group = settings.AGENT_TRIGGER_STREAM_GROUP
    shard_count = 1
    default_batch = settings.AGENT_TRIGGER_STREAM_BATCH
    block_ms = settings.AGENT_TRIGGER_STREAM_BLOCK_MS
    claim_idle_ms = settings.AGENT_TRIGGER_STREAM_CLAIM_IDLE_MS

    def __init__(self, consumer_name: str):
        self.consumer_name = consumer_name

    async def ensure_group(self) -> None:
        try:
            await redis_client.xgroup_create(
                settings.AGENT_TRIGGER_STREAM_KEY, self.group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def stream_keys(self) -> list:
        return [settings.AGENT_TRIGGER_STREAM_KEY]

    def stream_key_for_shard(self, shard: int) -> str:
        return settings.AGENT_TRIGGER_STREAM_KEY

    async def _requeue(self, fields: dict, owner_ids: set | None) -> None:
        """Re-XADD with attempts+1 (restricted to the owners that failed), or drop
        once AGENT_TRIGGER_MAX_ATTEMPTS is reached. The old entry is acked by the
        caller either way."""
        attempts = int(fields.get("attempts", 0)) + 1
        if attempts >= settings.AGENT_TRIGGER_MAX_ATTEMPTS:
            logger.warning(
                "agent trigger for message %s dropped after %s attempts", fields.get("message_id"), attempts
            )
            return
        await asyncio.sleep(attempts)  # small backoff so an outage isn't hammered
        entry = {"message_id": fields["message_id"], "chat_id": fields["chat_id"], "attempts": str(attempts)}
        if owner_ids:
            entry["owner_ids"] = ",".join(str(o) for o in sorted(owner_ids))
        await redis_client.xadd(
            settings.AGENT_TRIGGER_STREAM_KEY,
            entry,
            maxlen=settings.AGENT_TRIGGER_STREAM_MAXLEN,
            approximate=True,
        )

    async def process_entry(self, session: AsyncSession, fields: dict) -> None:
        message_id = int(fields["message_id"])
        chat_id = int(fields["chat_id"])
        only = {int(o) for o in fields["owner_ids"].split(",")} if fields.get("owner_ids") else None
        try:
            message = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
            if message is None:
                return
            # Detach so a per-owner session.rollback() inside the engine can't
            # expire the message's attributes mid-evaluation.
            session.expunge(message)
            failed = await _evaluate_triggers(session, message, only_owner_ids=only)
        except _INFRA_ERRORS:
            logger.warning("agent trigger infra failure for message %s", message_id, exc_info=True)
            await session.rollback()
            await self._requeue(fields, only)
            return
        except Exception:
            logger.exception("agent trigger evaluation failed for message %s", message_id)
            await session.rollback()
            return
        if failed:
            await self._requeue(fields, set(failed))
