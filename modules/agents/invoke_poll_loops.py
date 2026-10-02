"""Schedule/debounce poll loops that run beside the stream consumer in the
agent_worker process (split out of invoke_worker.py by ADR 0100)."""
import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.db.connection import session_scope
from modules.agents.crud import get_agent_by_id, get_agents_with_ephemeral_tasks
from modules.agents.ephemeral_tasks import fire_summary_and_complete, sweep_expired_task_ids
from modules.agents.goal_tasks import CONVERSE_MODE, close_goal_task
from modules.agents.invoke_debounce import due_pairs, pop_latest_message_id
from modules.agents.invoke_queue import enqueue_invocation, enqueue_schedule_fire
from modules.agents.schedule import due_members, remove_due_member, reschedule_recurring

logger = logging.getLogger(__name__)


async def _fire_schedule_entry(session: AsyncSession, agent_id: int, schedule_id: str) -> None:
    """One due ZSET member: re-load the live Agent row (defense in depth,
    same pattern as process_entry's dequeue-time re-check), enqueue the turn
    onto agent_invoke_stream, then either reschedule (recurring) or flip
    enabled=false and drop the ZSET member (once) - ADR 0046 decision 3."""
    agent = await get_agent_by_id(session, agent_id)
    if agent is None or not agent.is_enabled:
        await remove_due_member(agent_id, schedule_id)
        return

    entries = agent.triggers.get("on_schedule", []) or []
    entry = next((e for e in entries if e.get("id") == schedule_id), None)
    if entry is None or not entry.get("enabled", True):
        await remove_due_member(agent_id, schedule_id)
        return

    await enqueue_schedule_fire(agent_id=agent_id, schedule_id=schedule_id)

    if entry.get("kind") == "recurring" and entry.get("time"):
        await reschedule_recurring(agent_id, schedule_id, entry["time"])
    else:
        # "once": mark fired (visible in the UI, not silently gone) and drop
        # the ZSET member - one small DB write, same as any other trigger
        # config change. Builds a fresh entry dict rather than mutating
        # `entry` in place before reassigning agent.triggers - `entries` is
        # the same list object already referenced from agent.triggers, so an
        # in-place mutation followed by `agent.triggers = {...}` makes the
        # "old" and "new" JSONB values compare equal (both already reflect
        # the mutation), and SQLAlchemy's plain `==` change detection on the
        # JSONB column then skips the UPDATE entirely - the enabled=false
        # flip silently never reaches Postgres.
        updated_entries = [
            {**e, "enabled": False} if e.get("id") == schedule_id else e for e in entries
        ]
        agent.triggers = {**agent.triggers, "on_schedule": updated_entries}
        await session.flush()
        await remove_due_member(agent_id, schedule_id)


async def _sweep_expired_ephemeral_tasks() -> None:
    """ADR 0061: closes out on_ephemeral_task entries whose expires_at
    lapsed with nobody having replied - the one completion path with no
    inbound message to piggyback on (every other completion happens inline
    in the Trigger Rule Engine as soon as a reply lands). Runs on the same
    cadence as the schedule poll loop rather than a separate timer - one
    more cheap check alongside it, not a new worker."""
    async with session_scope() as session:
        agents = await get_agents_with_ephemeral_tasks(session)
        for agent in agents:
            for task_id in sweep_expired_task_ids(agent):
                entry = agent.triggers.get("on_ephemeral_task", {}).get(task_id)
                if entry is None:
                    continue
                if entry.get("mode") == CONVERSE_MODE:
                    # ADR 0099: goal tasks close via the shared owner-notice path.
                    await close_goal_task(
                        session, agent, task_id, status="timed_out",
                        summary="No resolution before the deadline.",
                    )
                    continue
                await fire_summary_and_complete(session, agent, task_id, entry)
        await session.commit()


async def _invoke_debounce_poll_loop(stop_event: asyncio.Event) -> None:
    """Tight poll (ADR 0063) alongside the schedule poll loop below - pops
    every (agent_id, chat_id) pair whose debounce window has elapsed and
    enqueues one agent_invoke_stream entry per pair, using the message_id
    stashed by the most recent arm_debounce call for that pair (the latest
    message in whatever burst got coalesced). A pair with no stashed
    message_id (arm_debounce failed, or the key already expired) is skipped
    - nothing to seed a turn from."""
    while not stop_event.is_set():
        try:
            for agent_id, chat_id in await due_pairs():
                message_id = await pop_latest_message_id(agent_id, chat_id)
                if message_id is None:
                    continue
                await enqueue_invocation(agent_id=agent_id, chat_id=chat_id, message_id=message_id)
        except Exception:
            logger.exception("agent_worker: invoke debounce poll iteration failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass


async def _schedule_poll_loop(stop_event: asyncio.Event) -> None:
    """Lightweight loop inside this same agent_worker process (ADR 0046
    decision 3) - checks agent_schedule_due every
    AGENT_SCHEDULE_POLL_INTERVAL_SECONDS and fires whatever's due. Runs
    alongside the stream consumer, not as a replacement for it. Also sweeps
    expired ephemeral tasks (ADR 0061) on the same tick."""
    while not stop_event.is_set():
        try:
            members = await due_members()
            for member in members:
                agent_id_str, schedule_id = member.split(":", 1)
                async with session_scope() as session:
                    await _fire_schedule_entry(session, int(agent_id_str), schedule_id)
        except Exception:
            logger.exception("agent_worker: schedule poll iteration failed")

        try:
            await _sweep_expired_ephemeral_tasks()
        except Exception:
            logger.exception("agent_worker: ephemeral task sweep failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.AGENT_SCHEDULE_POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
