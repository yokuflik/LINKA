"""ADR 0102 - continue_message: per-turn cap enforced in dispatch_tool_call."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import settings
from modules.agents import invoke_turn_loop
from modules.agents.invoke_turn_ctx import TurnCtx

pytestmark = pytest.mark.asyncio


def _ctx() -> TurnCtx:
    return TurnCtx(
        session=SimpleNamespace(commit=AsyncMock()),
        agent=SimpleNamespace(id=1),
        agent_id=1, chat_id=10, message_id=None,
        schedule_instruction=None, knowledge_instruction=None, scoped_system_prompt=None,
        owner_user_id=2, config_mode_turn=False,
    )


async def test_cap_refuses_without_executing(monkeypatch):
    run = AsyncMock(return_value={"message_id": "1"})
    monkeypatch.setattr(invoke_turn_loop, "execute_tool_call", run)
    ctx = _ctx()
    call = {"name": "continue_message", "args": {"chat_id": "10", "content": "part"}}
    limit = settings.AGENT_MAX_CONTINUATION_MESSAGES

    for used in range(limit):
        assert await invoke_turn_loop.dispatch_tool_call(ctx, call) is False
        assert ctx.continuations_used == used + 1
    assert run.await_count == limit
    assert ctx.contents[-1]["parts"][0]["functionResponse"]["response"]["continuations_remaining"] == 0

    # One past the cap: refused, handler never runs, counter unchanged.
    assert await invoke_turn_loop.dispatch_tool_call(ctx, call) is False
    assert run.await_count == limit
    assert ctx.continuations_used == limit
    assert "error" in ctx.contents[-1]["parts"][0]["functionResponse"]["response"]


async def test_failed_continue_does_not_consume_budget(monkeypatch):
    monkeypatch.setattr(invoke_turn_loop, "execute_tool_call", AsyncMock(return_value={"error": "denied"}))
    ctx = _ctx()
    await invoke_turn_loop.dispatch_tool_call(
        ctx, {"name": "continue_message", "args": {"chat_id": "10", "content": "x"}}
    )
    assert ctx.continuations_used == 0
