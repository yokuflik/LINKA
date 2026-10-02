"""ADR 0095: disposable-only trigger write access from ONE_OFF_ACTION
(_tool_update_own_triggers's require_expiry gate) and the new
delete_own_trigger tool (modules/agents/crud.py::delete_agent_trigger).

Real Postgres (ephemeral per-session DB, ADR 0032) - these handlers write
through Agent.triggers and the Redis pre-filter cache, so mocking the DB
away would test nothing real. Redis is flushed per-test via `redis_db`.
"""
import pytest

from modules.agents.tools.common import ToolDeniedError
from modules.agents.tools.execution import (
    _tool_delete_own_trigger,
    _tool_update_own_triggers,
    _tool_update_own_triggers_disposable,
)
from tests.modules.agents._factories import make_agent, make_chat, make_user

pytestmark = pytest.mark.asyncio


async def _setup(db_session):
    owner = await make_user(db_session)
    sender = await make_user(db_session)
    owner_agent_chat = await make_chat(db_session, owner, sender)
    target_chat = await make_chat(db_session, owner, sender)
    agent = await make_agent(db_session, owner, owner_agent_chat)
    return agent, target_chat


# --- require_expiry gate (ONE_OFF_ACTION's disposable-only rule) ------------

async def test_disposable_dispatch_rejects_a_permanent_on_specific_chats_entry(db_session, redis_db):
    agent, target_chat = await _setup(db_session)

    with pytest.raises(ToolDeniedError):
        await _tool_update_own_triggers_disposable(
            db_session, agent, {"triggers": {"on_specific_chats": {str(target_chat): {"keywords": []}}}}
        )


async def test_disposable_dispatch_accepts_an_entry_with_expires_at(db_session, redis_db):
    agent, target_chat = await _setup(db_session)

    result = await _tool_update_own_triggers_disposable(
        db_session,
        agent,
        {
            "triggers": {
                "on_specific_chats": {
                    str(target_chat): {"keywords": [], "expires_at": "2099-01-01T00:00:00+00:00"}
                }
            }
        },
    )
    assert result["triggers"]["on_specific_chats"][str(target_chat)]["expires_at"] == "2099-01-01T00:00:00+00:00"


async def test_disposable_dispatch_accepts_an_entry_with_max_fires(db_session, redis_db):
    agent, target_chat = await _setup(db_session)

    result = await _tool_update_own_triggers_disposable(
        db_session,
        agent,
        {"triggers": {"on_specific_chats": {str(target_chat): {"keywords": [], "max_fires": 3}}}},
    )
    assert result["triggers"]["on_specific_chats"][str(target_chat)]["max_fires"] == 3


async def test_disposable_dispatch_rejects_a_permanent_on_any_message_flag(db_session, redis_db):
    agent, _target_chat = await _setup(db_session)

    with pytest.raises(ToolDeniedError):
        await _tool_update_own_triggers_disposable(
            db_session, agent, {"triggers": {"on_any_message": {"enabled": True}}}
        )


async def test_disposable_dispatch_allows_disabling_on_any_message_without_expiry(db_session, redis_db):
    """enabled=False never needs expires_at/max_fires - only a newly-armed
    (enabled=True) flag is disposable-gated."""
    agent, _target_chat = await _setup(db_session)

    result = await _tool_update_own_triggers_disposable(
        db_session, agent, {"triggers": {"on_any_message": {"enabled": False}}}
    )
    assert result["triggers"]["on_any_message"]["enabled"] is False


async def test_disposable_dispatch_allows_deleting_a_chat_entry_via_none(db_session, redis_db):
    """Deleting an on_specific_chats entry (patch value None) never needs
    expiry fields either - only creating/editing one does."""
    agent, target_chat = await _setup(db_session)
    await _tool_update_own_triggers_disposable(
        db_session,
        agent,
        {"triggers": {"on_specific_chats": {str(target_chat): {"keywords": [], "max_fires": 1}}}},
    )

    result = await _tool_update_own_triggers_disposable(
        db_session, agent, {"triggers": {"on_specific_chats": {str(target_chat): None}}}
    )
    assert str(target_chat) not in result["triggers"]["on_specific_chats"]


async def test_builder_dispatch_still_allows_a_permanent_trigger(db_session, redis_db):
    """The plain (non-disposable) _tool_update_own_triggers - BUILDER's
    dispatch entry - must be unaffected: require_expiry defaults to False."""
    agent, target_chat = await _setup(db_session)

    result = await _tool_update_own_triggers(
        db_session, agent, {"triggers": {"on_specific_chats": {str(target_chat): {"keywords": []}}}}
    )
    assert result["triggers"]["on_specific_chats"][str(target_chat)] == {"keywords": []}


# --- delete_own_trigger -------------------------------------------------------

async def test_delete_own_trigger_removes_a_specific_chat_entry(db_session, redis_db):
    agent, target_chat = await _setup(db_session)
    await _tool_update_own_triggers(
        db_session, agent, {"triggers": {"on_specific_chats": {str(target_chat): {"keywords": []}}}}
    )

    result = await _tool_delete_own_trigger(
        db_session, agent, {"kind": "on_specific_chats", "chat_id": str(target_chat)}
    )
    assert str(target_chat) not in result["triggers"]["on_specific_chats"]


async def test_delete_own_trigger_on_missing_chat_entry_is_denied(db_session, redis_db):
    agent, target_chat = await _setup(db_session)

    with pytest.raises(ToolDeniedError):
        await _tool_delete_own_trigger(db_session, agent, {"kind": "on_specific_chats", "chat_id": str(target_chat)})


async def test_delete_own_trigger_disables_on_unknown_sender(db_session, redis_db):
    agent, _target_chat = await _setup(db_session)
    await _tool_update_own_triggers(db_session, agent, {"triggers": {"on_unknown_sender": {"enabled": True}}})

    result = await _tool_delete_own_trigger(db_session, agent, {"kind": "on_unknown_sender"})
    assert result["triggers"]["on_unknown_sender"]["enabled"] is False


async def test_delete_own_trigger_on_already_disabled_flag_is_denied(db_session, redis_db):
    agent, _target_chat = await _setup(db_session)

    with pytest.raises(ToolDeniedError):
        await _tool_delete_own_trigger(db_session, agent, {"kind": "on_any_message"})


async def test_delete_own_trigger_rejects_an_unknown_kind(db_session, redis_db):
    agent, _target_chat = await _setup(db_session)

    with pytest.raises(ToolDeniedError):
        await _tool_delete_own_trigger(db_session, agent, {"kind": "on_schedule"})
