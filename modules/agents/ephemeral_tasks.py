"""Ephemeral reply-collection tasks (ADR 0061).

A spawn_ephemeral_task call messages one or more chats, waits for their
replies, then summarizes the results back to the owner and deletes its own
Agent.triggers["on_ephemeral_task"][task_id] entry - self-cleaning, no
separate worker/cron. Modeled directly on pause_and_escalate's lazy-expiry
pattern (ADR 0054, modules/agents/crud.py::_active_pauses): an entry carries
its own expires_at, checked on read wherever it matters, rather than an
active sweep - except for the one case a message-triggered check can never
catch (nobody replies at all before expires_at), which the existing
_schedule_poll_loop (invoke_worker.py) already polls every
AGENT_SCHEDULE_POLL_INTERVAL_SECONDS and is reused here rather than adding a
second loop.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.cache import sync_agent_cache
from modules.agents.models import Agent

logger = logging.getLogger(__name__)


class EphemeralTaskQuotaExceededError(Exception):
    """Raised when spawning a task would push on_ephemeral_task past
    AGENT_MAX_EPHEMERAL_TASKS."""


def build_relay_prompt(instruction: str) -> str:
    """Scoped system prompt (ADR 0061) for the relay turn that messages an
    expected chat and waits for its reply - deliberately narrow so the task
    can't be steered into doing anything beyond the one exchange it was
    spawned for, regardless of what the counterpart says back."""
    return (
        "You are relaying a one-off request on behalf of your owner, as a "
        "single short exchange. Task: " + instruction + "\n\n"
        "Send this to the person in this chat using send_message, in their "
        "own language if they write back in one. Do not discuss anything "
        "else, and do not take any other action."
    )


def build_summary_prompt(instruction: str, collected: dict[str, str]) -> str:
    """Scoped system prompt for the final turn that reports the collected
    replies back to the owner, in their own agent chat."""
    if collected:
        lines = "\n".join(f"- {reply}" for reply in collected.values())
    else:
        lines = "(nobody replied in time)"
    return (
        "You spawned a one-off task on behalf of your owner: " + instruction + "\n\n"
        "Here are the replies collected so far:\n" + lines + "\n\n"
        "Summarize this for your owner in a short, natural message using "
        "send_message, addressed to their own agent chat. Do not take any "
        "other action."
    )


def _check_ephemeral_task_quota(triggers_patch: dict) -> None:
    entries = triggers_patch.get("on_ephemeral_task")
    if entries is not None and len(entries) > settings.AGENT_MAX_EPHEMERAL_TASKS:
        raise EphemeralTaskQuotaExceededError(
            f"on_ephemeral_task cannot exceed {settings.AGENT_MAX_EPHEMERAL_TASKS} concurrent tasks"
        )


async def spawn_ephemeral_task(
    session: AsyncSession,
    agent: Agent,
    *,
    instruction: str,
    chat_ids: list[str],
    timeout_minutes: Optional[int] = None,
) -> dict:
    """Creates a new on_ephemeral_task entry AND schedules the initial
    outreach to each expected chat_id via the existing schedule_one_off_task
    mechanism (ADR 0046 decision 3 / ADR 0061's execute_at="now" extension),
    each firing with scoped_system_prompt=build_relay_prompt(instruction) so
    the actual send_message call happens in a normal execution-mode turn
    against that chat - the spawning call itself runs in config-mode (the
    owner's own chat with Supervisor/Builder), which has no send_message tool
    and could never message a third party directly."""
    from modules.agents.crud import ScheduleQuotaExceededError, update_agent_triggers
    from modules.agents.schedule import sync_schedule_zset

    task_id = uuid.uuid4().hex
    minutes = min(
        int(timeout_minutes) if timeout_minutes else settings.AGENT_EPHEMERAL_TASK_DEFAULT_MINUTES,
        settings.AGENT_EPHEMERAL_TASK_MAX_MINUTES,
    )
    now = datetime.now(timezone.utc)
    chat_id_strs = [str(cid) for cid in chat_ids]
    entry = {
        "instruction": instruction,
        "owner_chat_id": str(agent.owner_agent_chat_id),
        "expected_chat_ids": chat_id_strs,
        "collected": {},
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
    }

    tasks = dict(agent.triggers.get("on_ephemeral_task", {}))
    tasks[task_id] = entry
    _check_ephemeral_task_quota({"on_ephemeral_task": tasks})

    relay_prompt = build_relay_prompt(instruction)
    schedule_entries = list(agent.triggers.get("on_schedule", []))
    for chat_id in chat_id_strs:
        schedule_entries.append({
            "id": uuid.uuid4().hex,
            "kind": "once",
            "at": now.isoformat(),
            "instruction": instruction,
            "scoped_system_prompt": relay_prompt,
            "chat_id": chat_id,
            "enabled": True,
        })

    try:
        updated = await update_agent_triggers(
            session, agent.id, {"on_ephemeral_task": tasks, "on_schedule": schedule_entries},
        )
    except ScheduleQuotaExceededError as exc:
        raise EphemeralTaskQuotaExceededError(
            f"cannot spawn: would exceed the {settings.AGENT_MAX_SCHEDULE_ENTRIES}-entry "
            "schedule limit shared with schedule_one_off_task"
        ) from exc
    await sync_agent_cache(updated)
    await sync_schedule_zset(updated)
    return {"task_id": task_id, **entry}


def _active_ephemeral_tasks(agent: Agent) -> dict[str, dict]:
    """Lazy-expiry filter (ADR 0054-style): drops any on_ephemeral_task entry
    whose expires_at has lapsed. Does NOT fire the summary turn or persist
    the removal itself - callers that need the summary-then-delete behavior
    use record_reply_and_maybe_complete / sweep_expired_tasks below, which
    call this only to decide whether an entry is still worth matching
    against."""
    now = datetime.now(timezone.utc)
    active = {}
    for task_id, entry in agent.triggers.get("on_ephemeral_task", {}).items():
        expires_at = entry.get("expires_at")
        if not expires_at:
            continue
        try:
            if datetime.fromisoformat(expires_at) > now:
                active[task_id] = entry
        except ValueError:
            continue
    return active


def find_task_for_chat(agent: Agent, chat_id: int) -> Optional[tuple[str, dict]]:
    """Returns the (task_id, entry) of the first active on_ephemeral_task
    entry expecting a reply from `chat_id`, or None. A chat_id could in
    theory appear in more than one concurrent task (AGENT_MAX_EPHEMERAL_TASKS
    is small, and nothing stops the owner from asking the same person two
    unrelated things back to back) - callers only need the first match since
    a real reply is relayed by whichever relay turn is currently listening,
    same as on_specific_chats' single-entry-per-chat matching."""
    chat_key = str(chat_id)
    for task_id, entry in _active_ephemeral_tasks(agent).items():
        if chat_key in entry.get("expected_chat_ids", []):
            return task_id, entry
    return None


async def record_reply(
    session: AsyncSession, agent: Agent, task_id: str, chat_id: int, reply_text: str
) -> Agent:
    """Appends one reply to a task's `collected` map. Caller (the relay turn's
    tool dispatch, or trigger_engine) is responsible for then checking
    is_task_complete and calling complete_task if so - kept separate so the
    actual summarize-turn enqueue happens outside of this write."""
    tasks = dict(agent.triggers.get("on_ephemeral_task", {}))
    entry = tasks.get(task_id)
    if entry is None:
        return agent
    collected = dict(entry.get("collected", {}))
    collected[str(chat_id)] = reply_text
    tasks[task_id] = {**entry, "collected": collected}
    agent.triggers = {**agent.triggers, "on_ephemeral_task": tasks}
    await session.flush()
    await sync_agent_cache(agent)
    return agent


def is_task_complete(entry: dict) -> bool:
    expected = set(entry.get("expected_chat_ids", []))
    collected = set(entry.get("collected", {}).keys())
    return expected.issubset(collected)


async def complete_task(session: AsyncSession, agent: Agent, task_id: str) -> Agent:
    """Removes a task's entry - called once its summarize turn has been
    enqueued (either because every expected chat replied, or its expires_at
    lapsed and sweep_expired_tasks is closing it out with whatever was
    collected). This is the self-cleanup step: no separate deletion tool, no
    sweep beyond the timeout case."""
    tasks = dict(agent.triggers.get("on_ephemeral_task", {}))
    tasks.pop(task_id, None)
    agent.triggers = {**agent.triggers, "on_ephemeral_task": tasks}
    await session.flush()
    await sync_agent_cache(agent)
    return agent


async def fire_summary_and_complete(session: AsyncSession, agent: Agent, task_id: str, entry: dict) -> None:
    """Shared by both completion paths (Trigger Rule Engine, on every reply
    landing; the schedule poll loop's timeout sweep, for tasks nobody ever
    replied to): schedules an immediate scoped turn (execute_at="now", same
    mechanism as schedule_one_off_task) that summarizes entry["collected"]
    into the owner's own agent chat, then deletes the task entry. Runs
    through the schedule/ZSET path rather than calling _run_turn inline so
    both callers stay fire-and-forget, matching how every other trigger
    match/poll-loop tick in this codebase only ever enqueues, never runs a
    turn synchronously."""
    from modules.agents.crud import update_agent_triggers
    from modules.agents.schedule import sync_schedule_zset

    summary_prompt = build_summary_prompt(entry["instruction"], entry.get("collected", {}))
    schedule_entries = [
        *agent.triggers.get("on_schedule", []),
        {
            "id": f"ephemeral-summary-{task_id}",
            "kind": "once",
            "at": datetime.now(timezone.utc).isoformat(),
            "instruction": entry["instruction"],
            "scoped_system_prompt": summary_prompt,
            "chat_id": entry["owner_chat_id"],
            "enabled": True,
        },
    ]
    agent = await complete_task(session, agent, task_id)
    try:
        updated = await update_agent_triggers(session, agent.id, {"on_schedule": schedule_entries})
    except Exception:
        logger.exception("agent %s: failed to schedule ephemeral summary for task %s", agent.id, task_id)
        return
    await sync_agent_cache(updated)
    await sync_schedule_zset(updated)


def sweep_expired_task_ids(agent: Agent) -> list[str]:
    """Task ids whose expires_at has lapsed - used by the schedule poll loop
    to close out tasks nobody ever replied to (the one case with no inbound
    message to piggyback a completion check on)."""
    now = datetime.now(timezone.utc)
    expired = []
    for task_id, entry in agent.triggers.get("on_ephemeral_task", {}).items():
        expires_at = entry.get("expires_at")
        if not expires_at:
            continue
        try:
            if datetime.fromisoformat(expires_at) <= now:
                expired.append(task_id)
        except ValueError:
            continue
    return expired
