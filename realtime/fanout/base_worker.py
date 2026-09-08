"""
Shared Redis Stream consumer for the fan-out workers.

services/fanout/worker.py (persist) and services/fanout/fanout_worker.py
(fan-out) both drain a chat_id-sharded Redis Stream with the same shape:
one consumer group per shard, one consumer task per shard, XREADGROUP for
new entries, XAUTOCLAIM to reclaim entries left unacked by a crashed
worker, XACK only the entries that processed cleanly, and leave transient
failures unacked for another worker to pick up.

BaseStreamConsumer captures that boilerplate. A concrete worker subclasses
it, fills in the stream/group config and the per-entry business logic
(process_entry), and optionally overrides the hooks below.
"""
import asyncio
import logging
from typing import Optional

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import session_scope
from services.redis_client import redis_client

logger = logging.getLogger(__name__)


class BaseStreamConsumer:
    # --- per-worker configuration (set by subclass) -----------------------
    name: str = "stream-worker"          # used in log lines
    consumer_name: str = "stream-worker" # Redis consumer name
    group: str = ""                      # consumer group (same on every shard)
    shard_count: int = 1
    default_batch: int = 1
    block_ms: int = 0
    claim_idle_ms: int = 0

    # --- hooks a subclass may override -----------------------------------
    async def ensure_group(self) -> None:
        """Create the consumer group(s) if missing. Cheap and idempotent."""
        raise NotImplementedError

    def stream_keys(self) -> list:
        """All shard stream keys (used when draining every shard)."""
        raise NotImplementedError

    def stream_key_for_shard(self, shard: int) -> str:
        """Stream key for one shard."""
        raise NotImplementedError

    async def process_entry(self, session: AsyncSession, fields: dict) -> None:
        """
        Handle one stream entry. Raise on a transient failure (the entry is
        then left unacked for reclaim); handle permanent cases internally.
        """
        raise NotImplementedError

    async def after_entry(self, session: AsyncSession) -> None:
        """
        Run after a successful process_entry, before the entry is acked.
        Default: nothing. worker.py commits here so one later transient
        failure can't roll back messages already persisted in the batch.
        """
        return None

    async def on_transient_failure(self, session: AsyncSession) -> None:
        """Run after a transient process_entry failure. Default: rollback."""
        await session.rollback()

    # --- generic stream-consuming machinery ------------------------------
    async def _claim_stale(self, stream_key: str, count: int) -> list:
        try:
            result = await redis_client.xautoclaim(
                stream_key,
                self.group,
                self.consumer_name,
                min_idle_time=self.claim_idle_ms,
                count=count,
            )
        except ResponseError as exc:
            logger.warning("XAUTOCLAIM unavailable, skipping stale-entry reclaim: %s", exc)
            return []
        claimed = result[1] if len(result) >= 2 else []
        return [(entry_id, fields) for entry_id, fields in claimed if fields]

    async def _drain_shard(
        self,
        session: AsyncSession,
        stream_key: str,
        *,
        block_ms: int,
        count: int,
        claim_stale: bool,
    ) -> int:
        response = await redis_client.xreadgroup(
            self.group,
            self.consumer_name,
            {stream_key: ">"},
            count=count,
            block=block_ms or None,
        )
        entries: list = []
        if response:
            entries = list(response[0][1])

        if claim_stale and len(entries) < count:
            entries.extend(await self._claim_stale(stream_key, count - len(entries)))

        if not entries:
            return 0

        ack_ids: list = []
        for entry_id, fields in entries:
            try:
                await self.process_entry(session, fields)
                await self.after_entry(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "%s: entry %s failed transiently, will be reclaimed", self.name, entry_id
                )
                await self.on_transient_failure(session)
                continue
            ack_ids.append(entry_id)

        if ack_ids:
            await redis_client.xack(stream_key, self.group, *ack_ids)
        return len(ack_ids)

    async def drain_once(
        self,
        session: AsyncSession,
        *,
        block_ms: int = 0,
        count: Optional[int] = None,
        claim_stale: bool = True,
        shard: Optional[int] = None,
    ) -> int:
        """
        Read one batch and process it. With ``shard`` given, only that
        shard's stream is drained (the per-shard worker loop passes it);
        with ``shard`` None, every shard is drained in turn (tests, a
        single-shard config). Returns how many entries were acked.
        """
        count = count or self.default_batch

        # Cheap and idempotent - keeps drain_once usable on its own (tests,
        # a fresh Redis) without a separate ensure_group() call first.
        await self.ensure_group()

        if shard is not None:
            key = self.stream_key_for_shard(shard)
            return await self._drain_shard(
                session, key, block_ms=block_ms, count=count, claim_stale=claim_stale
            )

        total = 0
        for key in self.stream_keys():
            total += await self._drain_shard(
                session, key, block_ms=block_ms, count=count, claim_stale=claim_stale
            )
        return total

    async def _run_shard(self, shard: int, stop_event: asyncio.Event | None) -> None:
        while stop_event is None or not stop_event.is_set():
            try:
                async with session_scope() as session:
                    acked = await self.drain_once(session, block_ms=self.block_ms, shard=shard)
                if acked == 0:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s (shard %s): drain iteration failed", self.name, shard)
                await asyncio.sleep(1.0)

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        try:
            await self.ensure_group()
        except Exception:  # Redis down at boot - the loop below keeps retrying
            logger.exception("%s: ensure_group failed, will retry", self.name)

        tasks = [
            asyncio.create_task(self._run_shard(s, stop_event))
            for s in range(self.shard_count)
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            raise
