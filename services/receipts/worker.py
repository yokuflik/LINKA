"""
Background consumer that drains the receipt Redis Stream into
message_receipt_log. Started once per app process from main.py's lifespan;
every process shares one consumer group, so adding replicas splits the load.
"""
import asyncio
import logging

from config import RECEIPT_WORKER_BLOCK_MS
from database.connection import session_scope
from services.receipts import receipt_log

logger = logging.getLogger(__name__)


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    try:
        await receipt_log.ensure_group()
    except Exception:  # Redis down at boot - the loop below keeps retrying
        logger.exception("receipt worker: ensure_group failed, will retry")

    while stop_event is None or not stop_event.is_set():
        try:
            async with session_scope() as session:
                written = await receipt_log.drain_once(session, block_ms=RECEIPT_WORKER_BLOCK_MS)
            # A full-looking batch means there may be more waiting - loop
            # straight back without the block delay. An empty read already
            # blocked for RECEIPT_WORKER_BLOCK_MS inside drain_once.
            if written == 0:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("receipt worker: drain iteration failed")
            await asyncio.sleep(1.0)
