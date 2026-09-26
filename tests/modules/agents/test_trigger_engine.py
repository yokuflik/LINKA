"""Trigger Rule Engine (evaluate_triggers) - ADR 0045 step 2 + ADR 0046
decision 1 (pre-filter cache) + ADR 0051 (auto-registration) + ADR 0052
(on_any_message) + ADR 0054 (paused-chat expiry, revised - see below).

These tests exercise the real Postgres (ephemeral per-session DB, ADR 0032)
and real Redis (flushed per-test via `redis_db`) - the whole point of this
engine is the interplay between the Redis pre-filter cache, the rate
limiter, and Postgres fallbacks, so mocking any of those away would test
nothing real. The only thing ever mocked is Gemini - and evaluate_triggers
never calls Gemini at all, it only enqueues a stream entry for the worker to
pick up later - so no LLM mocking is needed in this file.

Behavioral expectations encoded here (not just "whatever the code does"):

- A trigger only fires for *other* participants' agents, never for the
  sender's own agent (you can't accidentally wake your own agent by
  messaging someone else).
- System messages and the agent's own AGENT_REPLY_MESSAGE_TYPE messages
  never evaluate triggers at all (self-triggering-loop guard).
- A message in the owner's own dedicated agent chat is a direct, deliberate
  wake that bypasses on_specific_chats/on_time_window/keyword gating
  entirely, but still respects is_enabled and the hourly activation quota.
- That direct wake never implicitly resumes a paused chat, no matter what
  the message says (revised: the original ADR 0054 behavior resumed the
  most-recently-escalated pause on ANY owner message in that chat, which
  silently un-paused escalations unrelated to what the owner was actually
  saying - resuming a paused chat is now only ever explicit, via the
  resume_paused_chat config tool naming a phone/username, ADR 0055).
- Disabled agents (is_enabled=False) never fire, under any trigger.
- blocked_read_chat_ids and an active pause both suppress a match even if
  a trigger would otherwise fire.
- The hourly activation quota is enforced per-agent and, once exceeded,
  drops the trigger (message still delivered - this file only asserts on
  enqueue side effects) while posting exactly one owner-facing notice per
  quota window (SET NX cooldown), not one per dropped message.
- on_unknown_sender fires only on the very first message in a private chat,
  only once, and folds the chat into on_specific_chats afterwards so
  subsequent messages keep matching via the normal path.
- on_any_message fires on every message in every private chat while
  enabled, subject to on_time_window, but never in groups.
- Trigger matching must self-heal from a cold/missing Redis cache by
  falling back to Postgres and repopulating the cache.
"""
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import agent_settings, settings
from infra.redis.client import redis_client
from modules.agents.models import Agent, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.agents.trigger_engine import evaluate_triggers
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE, SYSTEM_MESSAGE_TYPE
from modules.messaging.crud import create_message
from modules.users.crud import create_user

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 900_000_000 + _ID


async def _make_user(session: AsyncSession) -> int:
    user_id = _next_id()
    await create_user(session, user_id=user_id, phone_number=f"+1555{user_id}")
    return user_id


async def _make_private_chat(session: AsyncSession, user_a: int, user_b: int) -> int:
    chat_id = _next_id()
    await create_chat(session, chat_id=chat_id, is_group=False)
    await add_participant_to_chat(session, chat_id=chat_id, user_id=user_a)
    await add_participant_to_chat(session, chat_id=chat_id, user_id=user_b)
    return chat_id


async def _make_group_chat(session: AsyncSession, *user_ids: int) -> int:
    chat_id = _next_id()
    await create_chat(session, chat_id=chat_id, is_group=True, title="Group")
    for uid in user_ids:
        await add_participant_to_chat(session, chat_id=chat_id, user_id=uid)
    return chat_id


async def _make_agent(
    session: AsyncSession,
    owner_user_id: int,
    owner_agent_chat_id: int,
    *,
    is_enabled: bool = True,
    triggers: dict | None = None,
    restrictions: dict | None = None,
) -> Agent:
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        is_enabled=is_enabled,
        triggers=triggers if triggers is not None else json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)),
        restrictions=restrictions if restrictions is not None else json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    # Re-sync the pre-filter cache the same way a real create/PATCH path
    # would, since evaluate_triggers reads it first.
    from modules.agents.cache import sync_agent_cache
    await sync_agent_cache(agent)
    return agent


async def _send(
    session: AsyncSession,
    chat_id: int,
    sender_id: int | None,
    content: str = "hello",
    type: int = 1,
):
    message_id = _next_id()
    message = await create_message(
        session, message_id=message_id, chat_id=chat_id, sender_id=sender_id, content=content, type=type
    )
    await session.commit()
    return message


async def _stream_entries() -> list[dict]:
    entries = await redis_client.xrange(settings.AGENT_INVOKE_STREAM_KEY)
    return [fields for _id, fields in entries]


# --- Basic on_specific_chats matching ---------------------------------------

async def test_fires_for_other_participants_enabled_agent_on_specific_chat(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
    )

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)

    entries = await _stream_entries()
    assert len(entries) == 1
    assert int(entries[0]["agent_id"]) == agent.id
    assert int(entries[0]["chat_id"]) == target_chat
    assert int(entries[0]["message_id"]) == message.id


async def test_never_fires_for_the_senders_own_agent(db_session, redis_db):
    """Sending a message must never wake your own agent - only other
    participants' agents are candidates."""
    sender = await _make_user(db_session)
    other = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, sender, other)
    target_chat = await _make_private_chat(db_session, sender, other)
    await _make_agent(
        db_session, sender, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
    )

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)

    assert await _stream_entries() == []


async def test_keyword_gate_requires_a_case_insensitive_substring_match(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": ["Refund"]}}},
    )

    no_match = await _send(db_session, target_chat, sender, content="what's the weather like")
    await evaluate_triggers(no_match)
    assert await _stream_entries() == []

    match = await _send(db_session, target_chat, sender, content="I need a REFUND please")
    await evaluate_triggers(match)
    assert len(await _stream_entries()) == 1


async def test_keyword_gate_never_matches_a_media_only_message(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": ["hi"]}}},
    )

    message = await _send(db_session, target_chat, sender, content=None)
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_chat_not_registered_in_on_specific_chats_never_fires(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    unrelated_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(db_session, owner, owner_agent_chat, triggers=DEFAULT_AGENT_TRIGGERS)

    message = await _send(db_session, unrelated_chat, sender)
    await evaluate_triggers(message)
    assert await _stream_entries() == []


# --- on_time_window ----------------------------------------------------------

async def test_time_window_blocks_a_match_outside_the_configured_hours(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    # A window that can never contain "now": 1 minute wide, set to a time
    # far from now would be flaky; instead pin start == end + 1 minute so
    # the window is a hair open only at one instant - simplest robust way is
    # to use a window that excludes "now" by checking both branches:
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    excluded_start = (now + datetime.timedelta(minutes=2)).strftime("%H:%M")
    excluded_end = (now + datetime.timedelta(minutes=3)).strftime("%H:%M")
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={
            **DEFAULT_AGENT_TRIGGERS,
            "on_specific_chats": {str(target_chat): {"keywords": []}},
            "on_time_window": {"enabled": True, "start": excluded_start, "end": excluded_end},
        },
    )

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_time_window_allows_a_match_inside_the_configured_hours(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={
            **DEFAULT_AGENT_TRIGGERS,
            "on_specific_chats": {str(target_chat): {"keywords": []}},
            "on_time_window": {"enabled": True, "start": "00:00", "end": "23:59"},
        },
    )

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)
    assert len(await _stream_entries()) == 1


async def test_malformed_time_window_fails_open(db_session, redis_db):
    """A malformed on_time_window must never silently gag the agent -
    the engine should fail open (treat it as if the window matched)."""
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={
            **DEFAULT_AGENT_TRIGGERS,
            "on_specific_chats": {str(target_chat): {"keywords": []}},
            "on_time_window": {"enabled": True, "start": "not-a-time", "end": "23:59"},
        },
    )

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)
    assert len(await _stream_entries()) == 1


# --- is_enabled kill switch ---------------------------------------------------

async def test_disabled_agent_never_fires(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat, is_enabled=False,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
    )

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)
    assert await _stream_entries() == []


# --- System / agent-reply messages never evaluate ---------------------------

async def test_system_messages_never_evaluate_triggers(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
    )

    message = await _send(db_session, target_chat, sender_id=None, type=SYSTEM_MESSAGE_TYPE, content="X joined")
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_agent_reply_messages_never_self_trigger(db_session, redis_db):
    """Regression: an agent's own reply (sender_id=owner_user_id,
    type=AGENT_REPLY_MESSAGE_TYPE) must never re-wake any agent - this is
    the fix for the found self-triggering-loop bug."""
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
    )

    reply = await _send(db_session, target_chat, sender_id=owner, type=AGENT_REPLY_MESSAGE_TYPE, content="reply")
    await evaluate_triggers(reply)
    assert await _stream_entries() == []


# --- Owner-chat direct wake ---------------------------------------------------

async def test_owner_message_in_own_agent_chat_bypasses_specific_chats_and_time_window(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        # No on_specific_chats entry for owner_agent_chat, and a time window
        # that would reject everything - direct wake must ignore both.
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_time_window": {"enabled": True, "start": "00:00", "end": "00:01"}},
    )

    message = await _send(db_session, owner_agent_chat, owner, content="hey agent")
    await evaluate_triggers(message)
    assert len(await _stream_entries()) == 1


async def test_owner_chat_direct_wake_still_respects_is_enabled(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(db_session, owner, owner_agent_chat, is_enabled=False)

    message = await _send(db_session, owner_agent_chat, owner, content="hey agent")
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_non_owner_message_in_owner_agent_chat_does_not_direct_wake(db_session, redis_db):
    """Only the owner's own message in their own agent chat direct-wakes;
    a message from anyone else there (shouldn't normally happen since it's
    a 1:1, but guards the sender_id check) must not."""
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(db_session, owner, owner_agent_chat)

    message = await _send(db_session, owner_agent_chat, sender, content="not the owner")
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_owner_message_in_own_agent_chat_never_auto_resumes_a_paused_chat(db_session, redis_db):
    """Revised behavior: a plain message from the owner in their own agent
    chat must NOT implicitly resume any paused chat, regardless of content -
    only the explicit resume_paused_chat config tool (ADR 0055, named
    phone/username) is allowed to un-pause a chat. The old ADR 0054
    "resume the freshest pause on any owner message" behavior silently
    un-paused escalations even when the owner's message had nothing to do
    with them, and has been removed."""
    import datetime
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    paused_chat = await _make_private_chat(db_session, owner, sender)
    now = datetime.datetime.now(datetime.timezone.utc)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    pause_entry = {
        "chat_id": str(paused_chat),
        "paused_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(hours=1)).isoformat(),
    }
    agent.paused_chat_ids = [pause_entry]
    await db_session.commit()

    message = await _send(db_session, owner_agent_chat, owner, content="hey agent, handle it")
    await evaluate_triggers(message)

    refreshed = await db_session.get(Agent, agent.id, populate_existing=True)
    assert refreshed.paused_chat_ids == [pause_entry]


# --- blocked_read_chat_ids and pauses ----------------------------------------

async def test_blocked_read_chat_id_suppresses_an_otherwise_matching_trigger(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
        restrictions={**DEFAULT_AGENT_RESTRICTIONS, "blocked_read_chat_ids": [str(target_chat)]},
    )

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_actively_paused_chat_suppresses_a_match(db_session, redis_db):
    import datetime
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    now = datetime.datetime.now(datetime.timezone.utc)
    agent = await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
    )
    agent.paused_chat_ids = [{
        "chat_id": str(target_chat),
        "paused_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(hours=1)).isoformat(),
    }]
    await db_session.commit()
    from modules.agents.cache import sync_agent_cache
    await sync_agent_cache(agent)

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_expired_pause_no_longer_suppresses_a_match(db_session, redis_db):
    import datetime
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    now = datetime.datetime.now(datetime.timezone.utc)
    agent = await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
    )
    agent.paused_chat_ids = [{
        "chat_id": str(target_chat),
        "paused_at": (now - datetime.timedelta(hours=2)).isoformat(),
        "expires_at": (now - datetime.timedelta(hours=1)).isoformat(),
    }]
    await db_session.commit()
    from modules.agents.cache import sync_agent_cache
    await sync_agent_cache(agent)

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)
    assert len(await _stream_entries()) == 1


# --- on_unknown_sender ---------------------------------------------------------

async def test_unknown_sender_fires_only_on_the_first_message_in_a_private_chat(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_unknown_sender": {"enabled": True}},
    )

    first = await _send(db_session, target_chat, sender, content="hi, who is this?")
    await evaluate_triggers(first)
    assert len(await _stream_entries()) == 1

    second = await _send(db_session, target_chat, sender, content="following up")
    await evaluate_triggers(second)
    # The second message should now match via the auto-registered
    # on_specific_chats entry (ADR 0051), not via on_unknown_sender again -
    # but either way exactly one *additional* enqueue is expected.
    assert len(await _stream_entries()) == 2


async def test_unknown_sender_never_fires_in_group_chats(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    group_chat = await _make_group_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_unknown_sender": {"enabled": True}},
    )

    message = await _send(db_session, group_chat, sender, content="hi group")
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_unknown_sender_auto_registers_chat_into_on_specific_chats(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_unknown_sender": {"enabled": True}},
    )

    message = await _send(db_session, target_chat, sender, content="hi")
    await evaluate_triggers(message)

    # auto_register_unknown_sender_chat only flush()es internally - the
    # caller (_evaluate_triggers) must commit or this write is invisible to
    # any other connection and gets rolled back when session_scope() closes
    # the session. populate_existing forces a fresh read from Postgres
    # through this test's own connection to prove the write really landed.
    refreshed = await db_session.get(Agent, agent.id, populate_existing=True)
    assert str(target_chat) in refreshed.triggers["on_specific_chats"]


# --- on_any_message ------------------------------------------------------------

async def test_on_any_message_fires_on_every_private_message_when_enabled(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_any_message": {"enabled": True}},
    )

    m1 = await _send(db_session, target_chat, sender, content="first")
    await evaluate_triggers(m1)
    m2 = await _send(db_session, target_chat, sender, content="second, unrelated")
    await evaluate_triggers(m2)

    assert len(await _stream_entries()) == 2


async def test_on_any_message_never_fires_in_group_chats(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    group_chat = await _make_group_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_any_message": {"enabled": True}},
    )

    message = await _send(db_session, group_chat, sender, content="hi group")
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_on_any_message_respects_time_window(db_session, redis_db):
    import datetime
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    now = datetime.datetime.now(datetime.timezone.utc)
    excluded_start = (now + datetime.timedelta(minutes=2)).strftime("%H:%M")
    excluded_end = (now + datetime.timedelta(minutes=3)).strftime("%H:%M")
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={
            **DEFAULT_AGENT_TRIGGERS,
            "on_any_message": {"enabled": True},
            "on_time_window": {"enabled": True, "start": excluded_start, "end": excluded_end},
        },
    )

    message = await _send(db_session, target_chat, sender, content="hi")
    await evaluate_triggers(message)
    assert await _stream_entries() == []


async def test_disabled_on_any_message_falls_back_to_no_match(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(db_session, owner, owner_agent_chat, triggers=DEFAULT_AGENT_TRIGGERS)

    message = await _send(db_session, target_chat, sender, content="hi")
    await evaluate_triggers(message)
    assert await _stream_entries() == []


# --- Activation quota ----------------------------------------------------------

async def test_activation_quota_drops_the_trigger_once_exceeded(db_session, redis_db, monkeypatch):
    monkeypatch.setattr(agent_settings, "AGENT_ACTIVATION_QUOTA_PER_HOUR", 1)

    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_any_message": {"enabled": True}},
    )

    m1 = await _send(db_session, target_chat, sender, content="one")
    await evaluate_triggers(m1)
    assert len(await _stream_entries()) == 1

    m2 = await _send(db_session, target_chat, sender, content="two")
    await evaluate_triggers(m2)
    # Still just one enqueued entry - the second trigger was dropped.
    assert len(await _stream_entries()) == 1


async def test_activation_quota_exceeded_posts_exactly_one_owner_notice_per_window(db_session, redis_db, monkeypatch):
    monkeypatch.setattr(agent_settings, "AGENT_ACTIVATION_QUOTA_PER_HOUR", 1)

    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_any_message": {"enabled": True}},
    )

    for content in ("one", "two", "three"):
        message = await _send(db_session, target_chat, sender, content=content)
        await evaluate_triggers(message)

    from modules.messaging.crud import get_chat_messages
    notices = await get_chat_messages(db_session, owner_agent_chat, limit=50)
    system_notices = [
        m for m in notices
        if m.type == SYSTEM_MESSAGE_TYPE and m.content and "hourly activation limit" in m.content
    ]
    assert len(system_notices) == 1


# --- Cache self-healing ---------------------------------------------------------

async def test_cold_cache_falls_back_to_postgres_and_still_matches(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    await _make_agent(
        db_session, owner, owner_agent_chat,
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {str(target_chat): {"keywords": []}}},
    )

    # Simulate a cold/evicted cache - the fallback path must still work.
    await redis_client.flushdb()

    message = await _send(db_session, target_chat, sender)
    await evaluate_triggers(message)
    assert len(await _stream_entries()) == 1


async def test_no_participant_owns_an_agent_short_circuits_cleanly(db_session, redis_db):
    a = await _make_user(db_session)
    b = await _make_user(db_session)
    chat = await _make_private_chat(db_session, a, b)

    message = await _send(db_session, chat, a)
    await evaluate_triggers(message)
    assert await _stream_entries() == []
