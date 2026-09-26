from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.cache import sync_agent_cache
from modules.agents.models import Agent, AgentKnowledgeChunk, AgentKnowledgeDocument


class ScheduleQuotaExceededError(Exception):
    """Raised when a triggers patch would push on_schedule past
    AGENT_MAX_SCHEDULE_ENTRIES (ADR 0046 decision 3)."""


def _check_schedule_quota(triggers_patch: dict) -> None:
    entries = triggers_patch.get("on_schedule")
    if entries is not None and len(entries) > settings.AGENT_MAX_SCHEDULE_ENTRIES:
        raise ScheduleQuotaExceededError(
            f"on_schedule cannot exceed {settings.AGENT_MAX_SCHEDULE_ENTRIES} entries"
        )


def _merge_triggers(current: dict, patch: dict) -> dict:
    """Merges a triggers patch into `current`. on_time_window/on_unknown_sender/
    on_any_message are merged one level deeper (not replaced outright) so a
    partial caller patch like {"on_time_window": {"enabled": false}} keeps the
    existing start/end instead of dropping them - AgentOut requires both
    fields, and a top-level-only shallow merge previously let a partial patch
    corrupt the row (missing start/end -> 500 on GET /agents/me). on_specific_chats
    is merged per chat_id for the same reason (each entry has its own
    sub-shape); on_schedule is replaced wholesale by design (see
    update_own_triggers)."""
    merged = {**current, **patch}
    if "on_time_window" in patch:
        merged["on_time_window"] = {**current.get("on_time_window", {}), **patch["on_time_window"]}
    if "on_unknown_sender" in patch:
        merged["on_unknown_sender"] = {**current.get("on_unknown_sender", {}), **patch["on_unknown_sender"]}
    if "on_any_message" in patch:
        merged["on_any_message"] = {**current.get("on_any_message", {}), **patch["on_any_message"]}
    if "on_specific_chats" in patch:
        existing_chats = current.get("on_specific_chats", {})
        merged["on_specific_chats"] = {
            chat_id: {**existing_chats.get(chat_id, {}), **chat_patch}
            for chat_id, chat_patch in patch["on_specific_chats"].items()
        }
    return merged


async def get_agent_by_id(session: AsyncSession, agent_id: int) -> Optional[Agent]:
    """Used by the agent_worker consumer to re-check is_enabled at dequeue
    time (defense in depth for the enqueue-to-dequeue window, ADR 0045)."""
    return await session.get(Agent, agent_id)


async def get_agent_by_owner(session: AsyncSession, owner_user_id: int) -> Optional[Agent]:
    """Used by GET/PATCH /agents/me - one agent per user (ADR 0045)."""
    stmt = select(Agent).where(Agent.owner_user_id == owner_user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def update_agent_config(session: AsyncSession, agent: Agent, patch: dict) -> Agent:
    """Applies a config PATCH (system_prompt / is_enabled / restrictions /
    triggers / active_skill) from the config UI (step 5) or the
    set_agent_persona config tool (ADR 0047 decision 6). restrictions/
    triggers are shallow-merged (only the keys present in the patch change);
    system_prompt/is_enabled/active_skill are replaced outright when present.
    Distinct from update_agent_triggers above, which is scoped to the
    update_own_triggers *tool* and never touches restrictions."""
    if "system_prompt" in patch:
        agent.system_prompt = patch["system_prompt"]
    if "is_enabled" in patch:
        agent.is_enabled = patch["is_enabled"]
    if "active_skill" in patch:
        agent.active_skill = patch["active_skill"]
    if "builder_state" in patch:
        agent.builder_state = patch["builder_state"]
    if "restrictions" in patch:
        agent.restrictions = {**agent.restrictions, **patch["restrictions"]}
    if "triggers" in patch:
        _check_schedule_quota(patch["triggers"])
        agent.triggers = _merge_triggers(agent.triggers, patch["triggers"])
    await session.flush()
    return agent


async def update_agent_triggers(session: AsyncSession, agent_id: int, patch: dict) -> Agent:
    """Merges `patch` into Agent.triggers (shallow, top-level keys only) and
    flushes. Used exclusively by the update_own_triggers tool - callers must
    ensure `agent_id` is the calling agent's own id; this never touches
    restrictions or any other agent's row (ADR 0045). Enforces
    AGENT_MAX_SCHEDULE_ENTRIES (ADR 0046 decision 3) so a self-editing agent
    cannot schedule its way past the cap."""
    _check_schedule_quota(patch)
    agent = await session.get(Agent, agent_id)
    agent.triggers = _merge_triggers(agent.triggers, patch)
    await session.flush()
    return agent


# --- Knowledge base (ADR 0046 decision 4) -----------------------------------

class KnowledgeQuotaExceededError(Exception):
    """Raised when an upload would push an agent past
    AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT or _MAX_CHUNKS_PER_AGENT."""


async def count_knowledge_documents(session: AsyncSession, agent_id: int) -> int:
    stmt = select(func.count()).select_from(AgentKnowledgeDocument).where(
        AgentKnowledgeDocument.agent_id == agent_id
    )
    return (await session.execute(stmt)).scalar_one()


async def count_knowledge_chunks(session: AsyncSession, agent_id: int) -> int:
    stmt = select(func.count()).select_from(AgentKnowledgeChunk).where(
        AgentKnowledgeChunk.agent_id == agent_id
    )
    return (await session.execute(stmt)).scalar_one()


async def check_knowledge_quota(session: AsyncSession, agent_id: int, new_chunks: int) -> None:
    """Enforced at upload (modules/agents/knowledge_service.py), before any
    chunk row is written."""
    doc_count = await count_knowledge_documents(session, agent_id)
    if doc_count >= settings.AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT:
        raise KnowledgeQuotaExceededError(
            f"cannot exceed {settings.AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT} documents"
        )
    chunk_count = await count_knowledge_chunks(session, agent_id)
    if chunk_count + new_chunks > settings.AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT:
        raise KnowledgeQuotaExceededError(
            f"cannot exceed {settings.AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT} chunks"
        )


async def list_knowledge_documents(session: AsyncSession, agent_id: int) -> Sequence[AgentKnowledgeDocument]:
    stmt = (
        select(AgentKnowledgeDocument)
        .where(AgentKnowledgeDocument.agent_id == agent_id)
        .order_by(AgentKnowledgeDocument.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_knowledge_document(
    session: AsyncSession, agent_id: int, document_id: int
) -> Optional[AgentKnowledgeDocument]:
    """Scoped to agent_id so one agent can never fetch/delete another's
    document by guessing an id."""
    stmt = select(AgentKnowledgeDocument).where(
        AgentKnowledgeDocument.id == document_id,
        AgentKnowledgeDocument.agent_id == agent_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_knowledge_document(session: AsyncSession, document: AgentKnowledgeDocument) -> None:
    """Cascades to its chunks via the FK's ON DELETE CASCADE."""
    await session.delete(document)
    await session.flush()


async def search_knowledge_chunks(
    session: AsyncSession, agent_id: int, tsquery: str, limit: int
) -> Sequence[AgentKnowledgeChunk]:
    """`websearch_to_tsquery`-style match against this agent's own chunks,
    top-N by ts_rank. Never crosses into another agent's documents - the
    agent_id filter is not optional (hard-scoped, same as update_own_triggers).
    Superseded by get_knowledge_index/fetch_chunk (ADR 0047 decision 5) for
    the document-RAG use case - kept for any other caller, not removed."""
    stmt = (
        select(AgentKnowledgeChunk)
        .where(
            AgentKnowledgeChunk.agent_id == agent_id,
            AgentKnowledgeChunk.content_tsv.op("@@")(func.websearch_to_tsquery("simple", tsquery)),
        )
        .order_by(
            func.ts_rank(
                AgentKnowledgeChunk.content_tsv, func.websearch_to_tsquery("simple", tsquery)
            ).desc()
        )
        .limit(limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def list_knowledge_index(session: AsyncSession, agent_id: int, limit: int) -> Sequence[AgentKnowledgeChunk]:
    """ADR 0047 decision 5: every chunk belonging to this agent, joined with
    its document's filename, for get_knowledge_index. Capped by the existing
    AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT guardrail (passed in as `limit`) -
    a known gap for a large corpus, acceptable for v1 per the ADR."""
    stmt = (
        select(AgentKnowledgeChunk, AgentKnowledgeDocument.filename)
        .join(AgentKnowledgeDocument, AgentKnowledgeChunk.document_id == AgentKnowledgeDocument.id)
        .where(AgentKnowledgeChunk.agent_id == agent_id)
        .order_by(AgentKnowledgeChunk.document_id, AgentKnowledgeChunk.chunk_index)
        .limit(limit)
    )
    return (await session.execute(stmt)).all()


async def get_knowledge_chunk(
    session: AsyncSession, agent_id: int, chunk_id: int
) -> Optional[AgentKnowledgeChunk]:
    """Scoped to agent_id - never returns another agent's chunk (ADR 0047
    decision 5's fetch_chunk tool)."""
    stmt = select(AgentKnowledgeChunk).where(
        AgentKnowledgeChunk.id == chunk_id,
        AgentKnowledgeChunk.agent_id == agent_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_enabled_agents_for_owners(session: AsyncSession, owner_user_ids: Sequence[int]) -> Sequence[Agent]:
    """Enabled agents owned by any of the given users - used by the Trigger
    Rule Engine to find which chat participants (besides the sender) have an
    active agent to possibly wake. Empty input short-circuits to no query."""
    if not owner_user_ids:
        return []
    stmt = select(Agent).where(
        Agent.owner_user_id.in_(owner_user_ids),
        Agent.is_enabled.is_(True),
    )
    result = await session.execute(stmt)
    return result.scalars().all()


def _active_pauses(agent: Agent) -> list[dict]:
    """ADR 0054: filters agent.paused_chat_ids down to entries that haven't
    lapsed yet, tolerating the pre-0054 flat-string shape (treated as already
    expired - no backfill needed, they just get dropped on first read)."""
    now = datetime.now(timezone.utc)
    active = []
    for entry in agent.paused_chat_ids:
        if not isinstance(entry, dict):
            continue
        expires_at = entry.get("expires_at")
        if not expires_at:
            continue
        try:
            if datetime.fromisoformat(expires_at) > now:
                active.append(entry)
        except ValueError:
            continue
    return active


async def pause_agent_chat(session: AsyncSession, agent: Agent, chat_id: int) -> Agent:
    """ADR 0047 decision 5 / ADR 0054: pause_and_escalate's write path - adds
    chat_id to paused_chat_ids (stamped with paused_at/expires_at) if not
    already actively paused. Idempotent (a second escalation on an
    already-paused, non-expired chat is a no-op, not an error) - also drops
    any other already-lapsed entries while it's here."""
    active = _active_pauses(agent)
    if any(int(entry["chat_id"]) == chat_id for entry in active):
        agent.paused_chat_ids = active
        return agent

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=settings.AGENT_ESCALATION_PAUSE_HOURS)
    agent.paused_chat_ids = [
        *active,
        {
            "chat_id": str(chat_id),
            "paused_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    ]
    await session.flush()
    return agent


async def resume_agent_chat(session: AsyncSession, agent: Agent, chat_id: int) -> Agent:
    """Human-only un-pause (POST /agents/me/resume-chat/{chat_id}) - the
    agent itself has no tool that can call this (ADR 0047 decision 5:
    un-pausing is deliberately not something an agent can do to itself)."""
    agent.paused_chat_ids = [
        entry for entry in _active_pauses(agent) if int(entry["chat_id"]) != chat_id
    ]
    await session.flush()
    return agent


def is_chat_actively_paused(agent: Agent, chat_id: int) -> bool:
    """ADR 0055: cheap check used by the resume_paused_chat tool to report a
    plain "not currently paused" outcome instead of silently no-op'ing."""
    return any(int(entry["chat_id"]) == chat_id for entry in _active_pauses(agent))


async def resume_most_recent_pause(session: AsyncSession, agent: Agent) -> Optional[int]:
    """ADR 0054: an owner message in their own agent chat is presumed to be
    about whichever escalation is freshest, so it resumes only the
    most-recently-escalated (max paused_at) active pause - not every paused
    chat at once. Returns the resumed chat_id, or None if nothing was
    actively paused."""
    active = _active_pauses(agent)
    if not active:
        return None
    most_recent = max(active, key=lambda entry: entry["paused_at"])
    agent.paused_chat_ids = [e for e in active if e is not most_recent]
    await session.flush()
    return int(most_recent["chat_id"])


async def auto_register_unknown_sender_chat(session: AsyncSession, agent: Agent, chat_id: int) -> Agent:
    """ADR 0051: called once on_unknown_sender has actually fired and the
    turn is enqueued - merges chat_id into on_specific_chats (empty keywords,
    tagged with _auto_added_at) so future messages in that chat keep matching
    via the normal _matches_trigger_config path. Idempotent: re-registering
    an already-present chat just refreshes nothing (keeps the original
    timestamp) rather than bumping it to the back of the FIFO queue.
    Manually-added entries (no _auto_added_at) are never touched or counted
    against AGENT_MAX_AUTO_CHATS - only auto-added entries evict each other."""
    chat_key = str(chat_id)
    specific_chats = dict(agent.triggers.get("on_specific_chats", {}))
    if chat_key in specific_chats:
        return agent

    specific_chats[chat_key] = {
        "keywords": [],
        "_auto_added_at": datetime.now(timezone.utc).isoformat(),
    }

    auto_entries = [
        (key, entry) for key, entry in specific_chats.items() if "_auto_added_at" in entry
    ]
    overflow = len(auto_entries) - settings.AGENT_MAX_AUTO_CHATS
    if overflow > 0:
        auto_entries.sort(key=lambda kv: kv[1]["_auto_added_at"])
        for key, _ in auto_entries[:overflow]:
            del specific_chats[key]

    agent.triggers = {**agent.triggers, "on_specific_chats": specific_chats}
    await session.flush()
    await sync_agent_cache(agent)
    return agent


async def get_agent_by_owner_chat(session: AsyncSession, chat_id: int) -> Optional[Agent]:
    """Used by the Trigger Rule Engine's owner-chat direct-wake special case
    (AGENT_DRAWER_UI_PLAN.md Wave 2, Step 5a): `owner_agent_chat_id` is
    unique per agent (one owner-agent chat per agent), so at most one row."""
    stmt = select(Agent).where(Agent.owner_agent_chat_id == chat_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
