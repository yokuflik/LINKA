"""Owner asks the agent to message themselves: the tool must hard-deny (send_message)
or flag is_owner (resolve_user) without opening a self-chat, and every denial
carries the generic DENIAL_HINT so the model explains instead of hunting for
workarounds."""
from types import SimpleNamespace

import pytest

from modules.agents.builder_flow import BuilderState
from modules.agents.tools import config_mode as config_mode_module
from modules.agents.tools import dispatch as dispatch_module
from modules.agents.tools.common import DENIAL_HINT, SELF_TARGET_REASON, ToolDeniedError
from modules.agents.tools.dispatch import execute_tool_call
from modules.agents.tools.execution import _tool_send_message

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_session():
    return SimpleNamespace()

OWNER_ID = 42


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        owner_user_id=OWNER_ID,
        owner_agent_chat_id=555,
        builder_state=BuilderState.ONE_OFF_ACTION.value,
        restrictions={},
        triggers={},
    )


async def test_send_message_to_owner_user_id_is_denied(fake_session):
    with pytest.raises(ToolDeniedError) as exc:
        await _tool_send_message(fake_session, _agent(), {"content": "hi", "target_user_id": str(OWNER_ID)})
    assert exc.value.reason == SELF_TARGET_REASON


async def test_resolve_user_flags_owner_without_opening_chat(monkeypatch, fake_session):
    async def _owner(session, phone):
        return SimpleNamespace(id=OWNER_ID, display_name=None, username="me", phone_number=phone)

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("self-chat must never be opened")

    monkeypatch.setattr(config_mode_module, "get_user_by_phone", _owner)
    monkeypatch.setattr(config_mode_module.chat_service, "get_or_create_private_chat", _must_not_run)

    result = await config_mode_module._tool_resolve_user(fake_session, _agent(), {"phone_number": "0501234567"})
    assert result["is_owner"] is True
    assert "chat_id" not in result


async def test_dispatch_denial_carries_hint(monkeypatch, fake_session):
    async def _noop_log(*args, **kwargs):
        return None

    monkeypatch.setattr(dispatch_module, "_log_call", _noop_log)
    result = await execute_tool_call(
        fake_session, _agent(), "send_message", {"content": "hi", "target_user_id": str(OWNER_ID)}, chat_id=555
    )
    assert result["error"] == SELF_TARGET_REASON
    assert result["hint"] == DENIAL_HINT
