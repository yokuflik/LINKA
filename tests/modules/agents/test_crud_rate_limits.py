"""Rate limits / quotas at the crud + cache layer (ADR 0045/0046/0047/0051/0054).

Scope: activation quota, unknown-sender daily quota, auto-registration FIFO
cap, and paused_chat_ids expiry - exercised directly against
`modules.agents.crud` / `infra.ratelimit.service` / `modules.agents.cache`,
distinct from the end-to-end `evaluate_triggers` coverage in
`test_trigger_engine.py`. Real Postgres (ephemeral per-session DB, ADR 0032)
and real Redis (flushed per-test via `redis_db`) - only Gemini itself is ever
worth mocking, and nothing here calls it.

Behavioral expectations encoded here (not just "whatever the code does"):

- `check_and_increment` (fixed-window) allows up to `max_per_window` calls in
  a window and rejects the (max+1)th, without raising.
- Two different identifiers (e.g. two different senders under the
  `agent_unknown_sender` action) never share a bucket - one being exhausted
  must not affect the other.
- `auto_register_unknown_sender_chat` is idempotent for an already-registered
  chat_id (no timestamp bump, no eviction side effect).
- Once the auto-added entry count exceeds `AGENT_MAX_AUTO_CHATS`, the
  *oldest* auto-added entry (by `_auto_added_at`) is evicted first (FIFO),
  and this only ever evicts other auto-added entries - a manually-added
  entry (no `_auto_added_at` key) is never evicted, even if it's the oldest
  chronologically, and never counts against the cap.
- `pause_agent_chat` is idempotent for a chat that's already actively
  paused (no duplicate entry, no expires_at bump) but still drops other
  already-lapsed entries while it's at it.
- `resume_agent_chat` removes only the targeted chat_id, leaving other
  active pauses untouched.
- `is_chat_actively_paused` / `_active_pauses` treat a lapsed `expires_at`
  as not-paused (lazy expiry on read), including the pre-ADR-0054 flat
  shape (no dict) which is tolerated as already-expired.
"""
import datetime
import json

import pytest

from config import agent_settings, settings
from infra.ratelimit.service import check_and_increment
from modules.agents.crud import (
    _active_pauses,
    auto_register_unknown_sender_chat,
    is_chat_actively_paused,
    pause_agent_chat,
    resume_agent_chat,
)
from modules.agents.models import Agent, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.users.crud import create_user

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 950_000_000 + _ID


async def _make_agent(session, owner_user_id: int | None = None, **overrides) -> Agent:
    if owner_user_id is None:
        owner_user_id = _next_id()
        await create_user(session, user_id=owner_user_id, phone_number=f"+1555{owner_user_id}")
    owner_agent_chat_id = _next_id()
    await create_chat(session, chat_id=owner_agent_chat_id, is_group=False)
    await add_participant_to_chat(session, chat_id=owner_agent_chat_id, user_id=owner_user_id)
    fields = {
        "triggers": json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)),
        "restrictions": json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
        **overrides,
    }
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        is_enabled=True,
        **fields,
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    return agent


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _pause_entry(chat_id: int, *, hours_until_expiry: float, hours_since_paused: float = 1.0) -> dict:
    now = _now()
    return {
        "chat_id": str(chat_id),
        "paused_at": (now - datetime.timedelta(hours=hours_since_paused)).isoformat(),
        "expires_at": (now + datetime.timedelta(hours=hours_until_expiry)).isoformat(),
    }


# --- infra.ratelimit.service.check_and_increment (fixed window) -------------

async def test_check_and_increment_allows_up_to_the_limit_then_blocks(redis_db):
    allowed = [await check_and_increment("agent-1", "agent_activation", 3, 3600) for _ in range(4)]
    assert allowed == [True, True, True, False]


async def test_check_and_increment_isolates_by_identifier(redis_db):
    for _ in range(3):
        assert await check_and_increment("agent-1", "agent_activation", 3, 3600) is True
    assert await check_and_increment("agent-1", "agent_activation", 3, 3600) is False

    # A different identifier under the same action must have its own bucket.
    assert await check_and_increment("agent-2", "agent_activation", 3, 3600) is True


async def test_check_and_increment_isolates_by_action(redis_db):
    for _ in range(3):
        assert await check_and_increment("agent-1", "agent_activation", 3, 3600) is True
    assert await check_and_increment("agent-1", "agent_activation", 3, 3600) is False

    # Same identifier, different action (e.g. unknown-sender vs activation)
    # must not be blocked by the activation bucket being full.
    assert await check_and_increment("agent-1", "agent_unknown_sender", 3, 3600) is True


async def test_unknown_sender_quota_key_isolates_per_sender(redis_db, monkeypatch):
    """Mirrors how trigger_engine keys the unknown-sender bucket:
    f"{agent.id}:{sender_user_id}" under the "agent_unknown_sender" action -
    exhausting it for one sender must not affect a different sender."""
    monkeypatch.setattr(agent_settings, "AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY", 2)
    agent_id = 12345
    sender_a = 111
    sender_b = 222

    results_a = [
        await check_and_increment(f"{agent_id}:{sender_a}", "agent_unknown_sender", 2, 86400)
        for _ in range(3)
    ]
    assert results_a == [True, True, False]

    # sender_b's own bucket is untouched by sender_a burning theirs out.
    assert await check_and_increment(f"{agent_id}:{sender_b}", "agent_unknown_sender", 2, 86400) is True


# --- auto_register_unknown_sender_chat: FIFO cap -----------------------------

async def test_auto_register_is_idempotent_for_an_already_registered_chat(db_session):
    agent = await _make_agent(db_session)
    chat_id = _next_id()

    await auto_register_unknown_sender_chat(db_session, agent, chat_id)
    first_entry = dict(agent.triggers["on_specific_chats"][str(chat_id)])

    await auto_register_unknown_sender_chat(db_session, agent, chat_id)
    second_entry = agent.triggers["on_specific_chats"][str(chat_id)]

    assert second_entry == first_entry
    assert len(agent.triggers["on_specific_chats"]) == 1


async def test_auto_registration_evicts_oldest_auto_added_entry_once_cap_exceeded(db_session, monkeypatch):
    monkeypatch.setattr(agent_settings, "AGENT_MAX_AUTO_CHATS", 2)
    agent = await _make_agent(db_session)
    chat_1, chat_2, chat_3 = _next_id(), _next_id(), _next_id()

    await auto_register_unknown_sender_chat(db_session, agent, chat_1)
    await auto_register_unknown_sender_chat(db_session, agent, chat_2)
    await auto_register_unknown_sender_chat(db_session, agent, chat_3)

    specific_chats = agent.triggers["on_specific_chats"]
    assert str(chat_1) not in specific_chats
    assert str(chat_2) in specific_chats
    assert str(chat_3) in specific_chats
    assert len(specific_chats) == 2


async def test_auto_registration_never_evicts_a_manually_added_entry(db_session, monkeypatch):
    monkeypatch.setattr(agent_settings, "AGENT_MAX_AUTO_CHATS", 1)
    manual_chat = _next_id()
    agent = await _make_agent(
        db_session,
        triggers={
            **DEFAULT_AGENT_TRIGGERS,
            # Manually added: no _auto_added_at key, so it never counts
            # against AGENT_MAX_AUTO_CHATS and is never eviction-eligible.
            "on_specific_chats": {str(manual_chat): {"keywords": []}},
        },
    )
    chat_a, chat_b = _next_id(), _next_id()

    await auto_register_unknown_sender_chat(db_session, agent, chat_a)
    await auto_register_unknown_sender_chat(db_session, agent, chat_b)

    specific_chats = agent.triggers["on_specific_chats"]
    # Manual entry survives regardless of the auto cap.
    assert str(manual_chat) in specific_chats
    # Cap of 1 auto-added entry: only the newest auto entry (chat_b) remains.
    assert str(chat_a) not in specific_chats
    assert str(chat_b) in specific_chats


async def test_auto_registration_with_cap_of_zero_evicts_every_auto_entry_immediately(db_session, monkeypatch):
    """Edge case: a cap of 0 means an auto-added entry is registered and then
    immediately evicted again on the very same call (overflow includes the
    entry just inserted) - the chat never persists in on_specific_chats."""
    monkeypatch.setattr(agent_settings, "AGENT_MAX_AUTO_CHATS", 0)
    agent = await _make_agent(db_session)
    chat_id = _next_id()

    await auto_register_unknown_sender_chat(db_session, agent, chat_id)

    assert agent.triggers["on_specific_chats"] == {}


# --- paused_chat_ids: pause / resume / expiry --------------------------------

async def test_pause_agent_chat_adds_an_entry_with_configured_expiry(db_session, monkeypatch):
    monkeypatch.setattr(agent_settings, "AGENT_ESCALATION_PAUSE_HOURS", 24)
    agent = await _make_agent(db_session)
    chat_id = _next_id()

    before = _now()
    await pause_agent_chat(db_session, agent, chat_id)
    after = _now()

    assert len(agent.paused_chat_ids) == 1
    entry = agent.paused_chat_ids[0]
    assert int(entry["chat_id"]) == chat_id
    expires_at = datetime.datetime.fromisoformat(entry["expires_at"])
    assert before + datetime.timedelta(hours=24) <= expires_at <= after + datetime.timedelta(hours=24)


async def test_pause_agent_chat_is_idempotent_for_an_already_active_pause(db_session):
    agent = await _make_agent(db_session)
    chat_id = _next_id()
    agent.paused_chat_ids = [_pause_entry(chat_id, hours_until_expiry=5)]
    await db_session.commit()
    original_entry = dict(agent.paused_chat_ids[0])

    await pause_agent_chat(db_session, agent, chat_id)

    assert len(agent.paused_chat_ids) == 1
    assert agent.paused_chat_ids[0] == original_entry


async def test_pause_agent_chat_drops_other_already_lapsed_entries(db_session):
    agent = await _make_agent(db_session)
    lapsed_chat = _next_id()
    new_chat = _next_id()
    agent.paused_chat_ids = [_pause_entry(lapsed_chat, hours_until_expiry=-1)]
    await db_session.commit()

    await pause_agent_chat(db_session, agent, new_chat)

    chat_ids = {int(e["chat_id"]) for e in agent.paused_chat_ids}
    assert chat_ids == {new_chat}


async def test_resume_agent_chat_removes_only_the_targeted_chat(db_session):
    agent = await _make_agent(db_session)
    chat_a, chat_b = _next_id(), _next_id()
    agent.paused_chat_ids = [
        _pause_entry(chat_a, hours_until_expiry=5),
        _pause_entry(chat_b, hours_until_expiry=5),
    ]
    await db_session.commit()

    await resume_agent_chat(db_session, agent, chat_a)

    chat_ids = {int(e["chat_id"]) for e in agent.paused_chat_ids}
    assert chat_ids == {chat_b}


async def test_resume_agent_chat_on_a_chat_not_paused_is_a_no_op(db_session):
    agent = await _make_agent(db_session)
    chat_a = _next_id()
    agent.paused_chat_ids = [_pause_entry(chat_a, hours_until_expiry=5)]
    await db_session.commit()

    await resume_agent_chat(db_session, agent, _next_id())

    chat_ids = {int(e["chat_id"]) for e in agent.paused_chat_ids}
    assert chat_ids == {chat_a}


async def test_is_chat_actively_paused_true_for_an_unexpired_entry(db_session):
    agent = await _make_agent(db_session)
    chat_id = _next_id()
    agent.paused_chat_ids = [_pause_entry(chat_id, hours_until_expiry=5)]

    assert is_chat_actively_paused(agent, chat_id) is True


async def test_is_chat_actively_paused_false_once_expired(db_session):
    agent = await _make_agent(db_session)
    chat_id = _next_id()
    agent.paused_chat_ids = [_pause_entry(chat_id, hours_until_expiry=-0.01)]

    assert is_chat_actively_paused(agent, chat_id) is False


async def test_is_chat_actively_paused_false_for_pre_0054_flat_shape(db_session):
    """Pre-ADR-0054 rows may still carry a bare chat_id (int/str) instead of
    a {chat_id, paused_at, expires_at} dict - _active_pauses/is_chat_actively_
    paused must tolerate this by treating it as already expired, not crash."""
    agent = await _make_agent(db_session)
    chat_id = _next_id()
    agent.paused_chat_ids = [str(chat_id)]

    assert is_chat_actively_paused(agent, chat_id) is False
    assert _active_pauses(agent) == []


async def test_active_pauses_excludes_expired_and_includes_active(db_session):
    agent = await _make_agent(db_session)
    active_chat = _next_id()
    expired_chat = _next_id()
    agent.paused_chat_ids = [
        _pause_entry(active_chat, hours_until_expiry=5),
        _pause_entry(expired_chat, hours_until_expiry=-1),
    ]

    active = _active_pauses(agent)
    assert {int(e["chat_id"]) for e in active} == {active_chat}
