"""Consumer for agent_invoke_stream (ADR 0045, step 3).

Drains the stream the Trigger Rule Engine (trigger_engine.py) writes to,
re-checks the kill switch and the daily active-time budget (defense in
depth for the enqueue-to-dequeue window), then runs one agent turn. Runs in
its own `agent_worker` Docker Compose service/process, never inside the main
app - a stuck or crashing Gemini turn must not affect the WS gateway or the
send/fan-out/receipt workers.

The Gemini call + tool dispatch (step 4) run inside `_run_turn` (invoke_turn.py
and the invoke_turn_*.py phase modules, split out by ADR 0100): build a
conversation (system prompt + a read_history-shaped transcript of the
triggering chat), call Gemini, and follow up to AGENT_TURN_MAX_TOOL_ROUNDTRIPS
functionCall round-trips, each dispatched through
modules.agents.tools.execute_tool_call. The whole turn is wrapped in
asyncio.wait_for(AGENT_TURN_TIMEOUT_SECONDS) by process_entry below so a
stuck Gemini call or tool execution can't hold a worker slot indefinitely.

`process_entry` also holds a per-(agent_id, chat_id) turn mutex around
`_run_turn` (ADR 0063) - a debounced fire landing while a previous turn for
the same pair is still running re-arms the debounce timer instead of racing
a second concurrent turn. `_invoke_debounce_poll_loop` (alongside
`_schedule_poll_loop`, same due-ZSET-poll pattern) is what actually turns a
coalesced burst of trigger matches into the single `enqueue_invocation` call
that lands on this stream in the first place.

That same re-arm path also marks the in-flight turn `superseded` (ADR 00732):
`_run_turn` checks this flag at the top of every round-trip and again right
before dispatching send_message/reply_message, ending the turn without
delivering its reply if a newer message has already taken its place. This
does not cancel the underlying Gemini call - it only stops a now-stale
answer from reaching the chat.
"""
import asyncio
import contextlib
import logging
import time

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.db.connection import session_scope
from infra.redis.client import redis_client
from modules.agents.crud import get_agent_by_id
from modules.agents.invoke_debounce import (
    acquire_turn_lock,
    arm_debounce,
    mark_superseded,
    release_turn_lock,
)
from modules.agents.invoke_notify import _notify_daily_budget_exhausted
from modules.agents.invoke_poll_loops import (  # noqa: F401 - re-exported
    _fire_schedule_entry,
    _invoke_debounce_poll_loop,
    _schedule_poll_loop,
    _sweep_expired_ephemeral_tasks,
)
from modules.agents.invoke_turn import _run_turn  # noqa: F401 - re-exported, patched here by tests
from modules.agents.invoke_turn_helpers import _post_config_reply
from modules.agents.time_budget import has_budget_remaining, record_active_seconds
from realtime.fanout.base_worker import BaseStreamConsumer, touch_app_liveness

logger = logging.getLogger(__name__)


class _TurnBusy(Exception):
    """A non-message entry hit a held turn lock; leave it unacked for reclaim."""


class AgentInvokeConsumer(BaseStreamConsumer):
    name = "agent-invoke-worker"
    group = settings.AGENT_INVOKE_STREAM_GROUP
    shard_count = 1  # single stream, not chat_id-sharded - see config/agent_settings.py
    default_batch = settings.AGENT_INVOKE_STREAM_BATCH
    block_ms = settings.AGENT_INVOKE_STREAM_BLOCK_MS
    claim_idle_ms = settings.AGENT_INVOKE_STREAM_CLAIM_IDLE_MS

    def __init__(self, consumer_name: str, semaphore: asyncio.Semaphore):
        self.consumer_name = consumer_name
        self._semaphore = semaphore

    async def ensure_group(self) -> None:
        try:
            await redis_client.xgroup_create(
                settings.AGENT_INVOKE_STREAM_KEY, self.group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def stream_keys(self) -> list:
        return [settings.AGENT_INVOKE_STREAM_KEY]

    def stream_key_for_shard(self, shard: int) -> str:
        return settings.AGENT_INVOKE_STREAM_KEY

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """ADR 0094: overrides BaseStreamConsumer.run_forever entirely - the
        default _run_shard loop `await`s drain_once (one xreadgroup batch,
        fully processed) before looping back to read again, so even a
        gather()-based concurrent _drain_shard override still can't pick up
        an entry that lands in Redis *after* the current batch was read but
        *before* the current batch finishes (found via direct testing: two
        messages ~7s apart landed in separate xreadgroup batches and the
        second never started until the first's full ~90s-capped turn ended -
        the read loop itself was the thing blocking, not the semaphore).

        Fix: decouple "read entries from the stream" from "wait for them to
        finish processing" entirely. One tight pump loop keeps calling
        xreadgroup/XAUTOCLAIM and fire-and-forget dispatches a task per
        entry; a separate bounded set of in-flight tasks (capped at
        AGENT_WORKER_CONCURRENCY via self._semaphore, acquired inside each
        task before it does any real work) is all that limits how many
        turns run at once - the pump itself never awaits a turn's own
        completion before reading the next batch."""
        try:
            await self.ensure_group()
        except Exception:
            logger.exception("%s: ensure_group failed, will retry", self.name)

        stop_event = stop_event or asyncio.Event()
        in_flight: set[asyncio.Task] = set()
        stream_key = settings.AGENT_INVOKE_STREAM_KEY

        async def _process_one(entry_id, fields: dict) -> None:
            async with self._semaphore:
                async with session_scope() as entry_session:
                    try:
                        await self.process_entry(entry_session, fields)
                    except asyncio.CancelledError:
                        raise
                    except _TurnBusy as busy:
                        logger.info(
                            "%s: entry %s deferred, turn busy (%s), will be reclaimed",
                            self.name, entry_id, busy,
                        )
                        await entry_session.rollback()
                        return
                    except Exception:
                        logger.exception(
                            "%s: entry %s failed transiently, will be reclaimed",
                            self.name, entry_id,
                        )
                        await entry_session.rollback()
                        return
            await self._ack_with_retry(stream_key, [entry_id])

        try:
            while not stop_event.is_set():
                try:
                    response = await redis_client.xreadgroup(
                        self.group,
                        self.consumer_name,
                        {stream_key: ">"},
                        count=self.default_batch,
                        block=self.block_ms or None,
                    )
                    entries: list = list(response[0][1]) if response else []

                    if len(entries) < self.default_batch:
                        entries.extend(
                            await self._claim_stale(stream_key, self.default_batch - len(entries))
                        )

                    for entry_id, fields in entries:
                        task = asyncio.create_task(_process_one(entry_id, fields))
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)

                    await touch_app_liveness()
                    if not entries:
                        await asyncio.sleep(0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("%s: drain iteration failed", self.name)
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            for task in in_flight:
                task.cancel()
            for task in list(in_flight):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    async def process_entry(self, session: AsyncSession, fields: dict) -> None:
        # ADR 0094: concurrency is now bounded by _drain_shard's own
        # semaphore acquisition (one per dispatched entry, around this whole
        # call) rather than here - process_entry itself no longer touches
        # self._semaphore.
        agent_id = int(fields["agent_id"])
        kind = fields.get("kind", "message")

        agent = await get_agent_by_id(session, agent_id)
        if agent is None or not agent.is_enabled:
            logger.info("agent_worker: skipping disabled/missing agent %s", agent_id)
            return

        if not await has_budget_remaining(agent_id):
            logger.info("agent_worker: agent %s over daily time budget, skipping", agent_id)
            await _notify_daily_budget_exhausted(session, agent)
            return

        if kind == "schedule":
            # ADR 0046 decision 3: re-load the live entry (defense in
            # depth for the enqueue-to-dequeue window, same pattern as
            # the is_enabled re-check above) - an edited/removed/
            # disabled entry since the poller enqueued it is a no-op.
            schedule_id = fields["schedule_id"]
            entry = next(
                (e for e in agent.triggers.get("on_schedule", []) if e.get("id") == schedule_id),
                None,
            )
            if entry is None or not entry.get("enabled", True):
                logger.info("agent_worker: schedule entry %s gone/disabled, skipping", schedule_id)
                return
            chat_id = int(entry["chat_id"]) if entry.get("chat_id") else None
            make_coro = lambda: _run_turn(  # noqa: E731
                agent_id,
                chat_id,
                schedule_instruction=entry["instruction"],
                scoped_system_prompt=entry.get("scoped_system_prompt"),
            )
            log_target = f"schedule {schedule_id}"
        elif kind == "knowledge":
            # ADR 0085: always targets the owner-agent chat - config-mode
            # by construction (is_config_mode), no chat history to seed
            # from, just the caller-built instruction string.
            chat_id = int(fields["chat_id"])
            make_coro = lambda: _run_turn(  # noqa: E731
                agent_id, chat_id, knowledge_instruction=fields["instruction"]
            )
            log_target = f"knowledge notice, chat {chat_id}"
        else:
            chat_id = int(fields["chat_id"])
            message_id = int(fields["message_id"])
            make_coro = lambda: _run_turn(agent_id, chat_id, message_id)  # noqa: E731
            log_target = f"chat {chat_id}"

        # Per-(agent_id, chat_id) turn mutex (ADR 0063): a debounced fire
        # landing while a previous turn for the same pair is still
        # running (up to AGENT_TURN_TIMEOUT_SECONDS) must not start a
        # second concurrent turn - re-arm the debounce timer instead of
        # dropping the message, so it retries right after the current
        # turn finishes. chat_id=None (schedule-fired, no chat target)
        # never contends with anything.
        if not await acquire_turn_lock(agent_id, chat_id):
            if kind != "message":
                # The debounce path below only replays a plain chat message;
                # a schedule/knowledge instruction would be lost. Leave the
                # entry unacked so it is reclaimed and retried later.
                raise _TurnBusy(f"agent {agent_id} {log_target}")
            logger.info(
                "agent_worker: turn already running for agent %s %s, marking superseded "
                "and re-arming debounce",
                agent_id, log_target,
            )
            if chat_id is not None:
                # ADR 00732: the in-flight turn for this pair checks this
                # flag and ends without delivering its (now-stale) reply,
                # instead of letting it reach the chat before the new
                # message gets its own turn.
                await mark_superseded(agent_id, chat_id)
                # ADR 0103: the debounce poll loop already POPPED the stashed
                # message_id to enqueue this entry - re-arm with it (without
                # overwriting a newer one), or the fire finds no id and is
                # silently skipped, dropping the message for good.
                await arm_debounce(agent_id, chat_id, message_id, keep_newer=True)
            return

        started = time.monotonic()
        try:
            await asyncio.wait_for(make_coro(), timeout=settings.AGENT_TURN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "agent_worker: turn for agent %s %s timed out after %ss",
                agent_id, log_target, settings.AGENT_TURN_TIMEOUT_SECONDS,
            )
            # Generic, non-technical notice to the owner - always posted to
            # their dedicated agent chat regardless of which chat/schedule
            # entry triggered the turn (frontend error UX rule: no raw
            # timeout/technical detail surfaced to the user).
            await _post_config_reply(
                session,
                agent,
                agent.owner_agent_chat_id,
                "This took a bit too long to process. Please try again in a moment.",
            )
            await session.commit()
        except Exception:
            # ADR 0089: any other unhandled failure inside _run_turn used
            # to propagate to _drain_shard's catch-all, which only logs
            # and leaves the entry unacked for reclaim - completely
            # silent from the owner's side (agent_thinking flips to
            # "error" in _run_turn's own finally, but that pub/sub event
            # has no replay and the frontend currently renders "error"
            # identically to "done"). Post the same fixed, non-technical
            # notice the timeout path above already uses, then re-raise
            # so the entry is still left unacked for reclaim/retry
            # exactly as before - this only adds an owner-facing trace,
            # it does not change delivery/retry semantics.
            logger.exception(
                "agent_worker: turn for agent %s %s failed", agent_id, log_target
            )
            await _post_config_reply(
                session,
                agent,
                agent.owner_agent_chat_id,
                "Sorry, something went wrong on my end. Please try again in a moment.",
            )
            await session.commit()
            raise
        finally:
            await record_active_seconds(agent_id, time.monotonic() - started)
            await release_turn_lock(agent_id, chat_id)



async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    from config import SERVER_ID

    stop_event = stop_event or asyncio.Event()
    semaphore = asyncio.Semaphore(settings.AGENT_WORKER_CONCURRENCY)
    consumer = AgentInvokeConsumer(f"agent-worker-{SERVER_ID}", semaphore)

    await asyncio.gather(
        consumer.run_forever(stop_event),
        _schedule_poll_loop(stop_event),
        _invoke_debounce_poll_loop(stop_event),
    )
