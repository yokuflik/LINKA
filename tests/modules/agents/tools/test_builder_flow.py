"""Builder / Supervisor / Help flow (ADR 0049, ADR 0064) -
modules/agents/builder_flow.py + modules/agents/tools/builder_handoff.py.

Behavioral contract exercised here (written against ADR 0049/0064 / the
`.claude_docs/ai_agent.md` description of intended behavior, not against
however the code happens to already work):

- BuilderState has exactly four values (supervisor/builder_agent/
  help_general/help_agent_building) and BUILDER_STATE_PROMPTS/
  get_builder_state_prompt cover all four, each with a distinct, non-empty
  prompt.
- get_builder_state_prompt raises for anything that isn't already a
  BuilderState member - callers must coerce a stored string through
  BuilderState(...) first (same convention as personas.get_persona_system_prompt),
  it must never silently fall back to a default prompt.
- transfer_to_builder writes builder_state="builder_agent" via
  update_agent_config and returns a status/to/instruction payload that
  reflects the new state - it must never call sync_agent_cache (builder_state
  is not part of the trigger pre-filter cache payload: only is_enabled/
  triggers/active_skill/paused_chat_ids are cached, per modules/agents/cache.py).
- transfer_to_help_building/transfer_to_help_general mirror transfer_to_builder,
  writing builder_state="help_agent_building"/"help_general" respectively,
  also never touching sync_agent_cache.
- finish_building_agent writes builder_state="supervisor" AND is_enabled=True
  in the same update_agent_config call (a single patch, not two writes), then
  calls sync_agent_cache exactly once with the freshly updated agent - this is
  the one handoff tool that changes is_enabled and therefore must resync the
  cache. If update_agent_config raises, sync_agent_cache must never be called
  (no half-applied cache sync after a failed write).
- BUILDER_STATE_HANDLERS wires each BuilderState to its own tool-name set:
  supervisor -> {transfer_to_builder, transfer_to_help_building,
  transfer_to_help_general, resume_paused_chat, resolve_user,
  spawn_ephemeral_task} + the full execution-mode toolset (ADR 0062);
  builder_agent -> the 6 ADR 0047 config tools + resolve_user +
  resume_paused_chat + get_capacity_status + transfer_to_help_building +
  transfer_to_help_general + transfer_to_supervisor + finish_building_agent +
  the full execution-mode toolset too (same ADR 0062 reasoning - the Builder
  talks to its own supervised owner, so it can act directly mid-interview);
  help_agent_building -> {transfer_to_builder, transfer_to_help_general,
  transfer_to_supervisor} only; help_general -> {transfer_to_help_building,
  transfer_to_supervisor} only. Supervisor and builder_agent are the two
  deliberate exceptions to the config/execution no-overlap invariant; both
  Help states keep it strictly (transfer tools only, no execution/config
  tool of their own).
- Every handler referenced in BUILDER_STATE_HANDLERS is an async callable
  accepting (session, agent, arguments) - dispatch.execute_tool_call always
  calls builder-state handlers with that 3-arg signature (none of these tools
  are in CHAT_SCOPED_TOOL_NAMES).

Gemini is never called anywhere in this file - these are pure unit tests
against already-decided tool_name/arguments, with update_agent_config and
sync_agent_cache monkeypatched to lightweight fakes so no real DB/Redis
side effects occur.
"""
from types import SimpleNamespace

import pytest

from modules.agents.builder_flow import (
    BUILDER_STATE_PROMPTS,
    BuilderState,
    get_builder_state_prompt,
)
from modules.agents.tools import builder_handoff as builder_handoff_module
from modules.agents.tools.builder_handoff import BUILDER_STATE_HANDLERS


def _agent(builder_state: str = BuilderState.SUPERVISOR.value) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        owner_user_id=42,
        owner_agent_chat_id=555,
        builder_state=builder_state,
        is_enabled=False,
        restrictions={},
    )


@pytest.fixture
def fake_session():
    return SimpleNamespace()


# --- BuilderState / prompt coverage ------------------------------------------


def test_builder_state_has_exactly_four_values():
    assert {s.value for s in BuilderState} == {
        "supervisor",
        "builder_agent",
        "help_general",
        "help_agent_building",
    }


def test_every_builder_state_has_a_distinct_non_empty_prompt():
    assert set(BUILDER_STATE_PROMPTS.keys()) == set(BuilderState)
    prompts = [BUILDER_STATE_PROMPTS[s] for s in BuilderState]
    for prompt in prompts:
        assert isinstance(prompt, str)
        assert prompt.strip() != ""
    assert len(set(prompts)) == len(prompts)


@pytest.mark.parametrize("state", list(BuilderState))
def test_get_builder_state_prompt_returns_the_matching_prompt(state):
    assert get_builder_state_prompt(state) == BUILDER_STATE_PROMPTS[state]


def test_get_builder_state_prompt_accepts_a_plain_string_equal_to_a_member_value():
    """BuilderState is a (str, Enum), so a raw string with a matching value
    hashes/compares equal to the enum member and works as a dict key too -
    get_builder_state_prompt does not itself enforce strict enum identity.
    The "coerce through BuilderState(...) first" convention documented on the
    function is a discipline for callers reading a stored/untrusted value,
    not a runtime guarantee this function enforces."""
    assert get_builder_state_prompt("supervisor") == BUILDER_STATE_PROMPTS[BuilderState.SUPERVISOR]


def test_get_builder_state_prompt_raises_for_an_unrecognized_string():
    with pytest.raises(KeyError):
        get_builder_state_prompt("not_a_real_state")


def test_builder_state_construction_raises_for_an_unknown_value():
    """The actual enforcement point: coercing an untrusted/stored string
    through BuilderState(...) - as every real caller (dispatch.py,
    builder_handoff.py) does - raises for anything not a real state."""
    with pytest.raises(ValueError):
        BuilderState("not_a_real_state")


# --- transfer_to_builder ------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_to_builder_sets_builder_state_to_builder_agent(monkeypatch, fake_session):
    captured_patch = {}

    async def _fake_update(session, agent, patch):
        captured_patch.update(patch)
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)

    agent = _agent(builder_state=BuilderState.SUPERVISOR.value)
    result = await builder_handoff_module._tool_transfer_to_builder(fake_session, agent, {})

    assert captured_patch == {"builder_state": BuilderState.BUILDER.value}
    assert result["status"] == "transferred"
    assert result["to"] == BuilderState.BUILDER.value
    assert "instruction" in result and isinstance(result["instruction"], str)


@pytest.mark.asyncio
async def test_transfer_to_builder_never_touches_sync_agent_cache(monkeypatch, fake_session):
    """builder_state is not part of the Redis trigger pre-filter cache
    payload (only is_enabled/triggers/active_skill/paused_chat_ids are) - a
    plain state transfer must not trigger a cache resync."""
    sync_called = False

    async def _fake_sync(agent):
        nonlocal sync_called
        sync_called = True

    async def _fake_update(session, agent, patch):
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)
    monkeypatch.setattr(builder_handoff_module, "sync_agent_cache", _fake_sync)

    agent = _agent(builder_state=BuilderState.SUPERVISOR.value)
    await builder_handoff_module._tool_transfer_to_builder(fake_session, agent, {})

    assert sync_called is False


@pytest.mark.asyncio
async def test_transfer_to_builder_ignores_extra_arguments(monkeypatch, fake_session):
    """The tool schema takes no parameters - passing an unexpected arguments
    dict (a model hallucinating fields) must not raise or change behavior."""
    async def _fake_update(session, agent, patch):
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)

    agent = _agent()
    result = await builder_handoff_module._tool_transfer_to_builder(
        fake_session, agent, {"unexpected": "value"}
    )
    assert result["status"] == "transferred"


# --- transfer_to_help_building / transfer_to_help_general ---------------------


@pytest.mark.asyncio
async def test_transfer_to_help_building_sets_builder_state_to_help_agent_building(monkeypatch, fake_session):
    captured_patch = {}

    async def _fake_update(session, agent, patch):
        captured_patch.update(patch)
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)

    agent = _agent(builder_state=BuilderState.BUILDER.value)
    result = await builder_handoff_module._tool_transfer_to_help_building(fake_session, agent, {})

    assert captured_patch == {"builder_state": BuilderState.HELP_BUILDING.value}
    assert result["status"] == "transferred"
    assert result["to"] == BuilderState.HELP_BUILDING.value
    assert "instruction" in result and isinstance(result["instruction"], str)


@pytest.mark.asyncio
async def test_transfer_to_help_building_never_touches_sync_agent_cache(monkeypatch, fake_session):
    sync_called = False

    async def _fake_sync(agent):
        nonlocal sync_called
        sync_called = True

    async def _fake_update(session, agent, patch):
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)
    monkeypatch.setattr(builder_handoff_module, "sync_agent_cache", _fake_sync)

    agent = _agent(builder_state=BuilderState.SUPERVISOR.value)
    await builder_handoff_module._tool_transfer_to_help_building(fake_session, agent, {})

    assert sync_called is False


@pytest.mark.asyncio
async def test_transfer_to_help_general_sets_builder_state_to_help_general(monkeypatch, fake_session):
    captured_patch = {}

    async def _fake_update(session, agent, patch):
        captured_patch.update(patch)
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)

    agent = _agent(builder_state=BuilderState.SUPERVISOR.value)
    result = await builder_handoff_module._tool_transfer_to_help_general(fake_session, agent, {})

    assert captured_patch == {"builder_state": BuilderState.HELP_GENERAL.value}
    assert result["status"] == "transferred"
    assert result["to"] == BuilderState.HELP_GENERAL.value
    assert "instruction" in result and isinstance(result["instruction"], str)


@pytest.mark.asyncio
async def test_transfer_to_help_general_never_touches_sync_agent_cache(monkeypatch, fake_session):
    sync_called = False

    async def _fake_sync(agent):
        nonlocal sync_called
        sync_called = True

    async def _fake_update(session, agent, patch):
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)
    monkeypatch.setattr(builder_handoff_module, "sync_agent_cache", _fake_sync)

    agent = _agent(builder_state=BuilderState.BUILDER.value)
    await builder_handoff_module._tool_transfer_to_help_general(fake_session, agent, {})

    assert sync_called is False


# --- transfer_to_supervisor ------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_to_supervisor_sets_builder_state_to_supervisor(monkeypatch, fake_session):
    captured_patch = {}

    async def _fake_update(session, agent, patch):
        captured_patch.update(patch)
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)

    agent = _agent(builder_state=BuilderState.BUILDER.value)
    result = await builder_handoff_module._tool_transfer_to_supervisor(fake_session, agent, {})

    assert captured_patch == {"builder_state": BuilderState.SUPERVISOR.value}
    assert result["status"] == "transferred"
    assert result["to"] == BuilderState.SUPERVISOR.value
    assert "instruction" in result and isinstance(result["instruction"], str)


@pytest.mark.asyncio
async def test_transfer_to_supervisor_never_activates_agent_or_touches_cache(monkeypatch, fake_session):
    """Unlike finish_building_agent, this is an escape hatch, not a
    completion signal - it must never flip is_enabled or sync the cache."""
    sync_called = False

    async def _fake_sync(agent):
        nonlocal sync_called
        sync_called = True

    async def _fake_update(session, agent, patch):
        assert "is_enabled" not in patch
        agent.builder_state = patch["builder_state"]
        return agent

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)
    monkeypatch.setattr(builder_handoff_module, "sync_agent_cache", _fake_sync)

    agent = _agent(builder_state=BuilderState.BUILDER.value)
    await builder_handoff_module._tool_transfer_to_supervisor(fake_session, agent, {})

    assert sync_called is False


# --- finish_building_agent -----------------------------------------------------


@pytest.mark.asyncio
async def test_finish_building_agent_sets_supervisor_and_enables_in_one_patch(monkeypatch, fake_session):
    captured_patch = {}

    async def _fake_update(session, agent, patch):
        captured_patch.update(patch)
        agent.builder_state = patch["builder_state"]
        agent.is_enabled = patch["is_enabled"]
        return agent

    async def _fake_sync(agent):
        pass

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)
    monkeypatch.setattr(builder_handoff_module, "sync_agent_cache", _fake_sync)

    agent = _agent(builder_state=BuilderState.BUILDER.value)
    agent.is_enabled = False
    result = await builder_handoff_module._tool_finish_building_agent(fake_session, agent, {})

    assert captured_patch == {
        "builder_state": BuilderState.SUPERVISOR.value,
        "is_enabled": True,
    }
    assert result == {"status": "agent_activated"}


@pytest.mark.asyncio
async def test_finish_building_agent_calls_sync_agent_cache_exactly_once_with_updated_agent(
    monkeypatch, fake_session
):
    sync_calls = []

    async def _fake_update(session, agent, patch):
        agent.builder_state = patch["builder_state"]
        agent.is_enabled = patch["is_enabled"]
        return agent

    async def _fake_sync(agent):
        sync_calls.append(agent)

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)
    monkeypatch.setattr(builder_handoff_module, "sync_agent_cache", _fake_sync)

    agent = _agent(builder_state=BuilderState.BUILDER.value)
    await builder_handoff_module._tool_finish_building_agent(fake_session, agent, {})

    assert len(sync_calls) == 1
    assert sync_calls[0] is agent
    assert sync_calls[0].is_enabled is True
    assert sync_calls[0].builder_state == BuilderState.SUPERVISOR.value


@pytest.mark.asyncio
async def test_finish_building_agent_never_calls_sync_agent_cache_if_update_fails(monkeypatch, fake_session):
    """If the config write itself fails, the cache must not be resynced with
    a state that was never actually persisted - no half-applied cache sync
    after a failed write."""
    sync_called = False

    async def _fake_update(session, agent, patch):
        raise RuntimeError("db write failed")

    async def _fake_sync(agent):
        nonlocal sync_called
        sync_called = True

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)
    monkeypatch.setattr(builder_handoff_module, "sync_agent_cache", _fake_sync)

    agent = _agent(builder_state=BuilderState.BUILDER.value)
    with pytest.raises(RuntimeError):
        await builder_handoff_module._tool_finish_building_agent(fake_session, agent, {})

    assert sync_called is False


# --- BUILDER_STATE_HANDLERS wiring: disjoint tool sets ------------------------


def test_supervisor_handler_set_is_exactly_the_expected_tools():
    # ADR 0062: Supervisor gets its own tools plus the full execution-mode
    # toolset, so the owner can act directly ("send X a message") from their
    # own agent chat without transferring into the Builder interview flow.
    from modules.agents.tools.execution import EXECUTION_TOOL_HANDLERS

    assert set(BUILDER_STATE_HANDLERS[BuilderState.SUPERVISOR]) == {
        "transfer_to_builder",
        "transfer_to_help_building",
        "transfer_to_help_general",
        "resume_paused_chat",
        "resolve_user",
        "find_chat_by_name",
        "spawn_ephemeral_task",
        "no_reply_needed",
        "save_knowledge_from_text",
        *EXECUTION_TOOL_HANDLERS,
    }


def test_builder_handler_set_includes_every_expected_config_and_handoff_tool():
    # Builder also gets the full execution-mode toolset unioned in (same
    # ADR 0062 reasoning as Supervisor) so it can act directly mid-interview.
    from modules.agents.tools.execution import EXECUTION_TOOL_HANDLERS

    expected = {
        "set_agent_persona",
        "update_agent_rules",
        "set_trigger",
        "get_agent_status",
        "estimate_api_usage",
        "schedule_one_off_task",
        "resolve_user",
        "find_chat_by_name",
        "resume_paused_chat",
        "get_capacity_status",
        "spawn_ephemeral_task",
        "no_reply_needed",
        "save_knowledge_from_text",
        "transfer_to_help_building",
        "transfer_to_help_general",
        "transfer_to_supervisor",
        "finish_building_agent",
        *EXECUTION_TOOL_HANDLERS,
    }
    assert set(BUILDER_STATE_HANDLERS[BuilderState.BUILDER]) == expected


def test_help_building_handler_set_is_exactly_its_three_transfer_tools():
    # ADR 0065: every builder_state also gets no_reply_needed.
    assert set(BUILDER_STATE_HANDLERS[BuilderState.HELP_BUILDING]) == {
        "transfer_to_builder",
        "transfer_to_help_general",
        "transfer_to_supervisor",
        "no_reply_needed",
    }


def test_help_general_handler_set_is_exactly_its_two_transfer_tools():
    # ADR 0065: every builder_state also gets no_reply_needed.
    assert set(BUILDER_STATE_HANDLERS[BuilderState.HELP_GENERAL]) == {
        "transfer_to_help_building",
        "transfer_to_supervisor",
        "no_reply_needed",
    }


def test_builder_state_never_has_transfer_to_builder_available():
    """Only supervisor/help_agent_building can hand off *into* the builder -
    the Builder's own tool set must never include a way to transfer to
    itself, and help_general reaches the builder only via help_building."""
    assert "transfer_to_builder" not in BUILDER_STATE_HANDLERS[BuilderState.BUILDER]
    assert "transfer_to_builder" not in BUILDER_STATE_HANDLERS[BuilderState.HELP_GENERAL]


def test_only_supervisor_and_builder_have_finish_building_agent():
    assert "finish_building_agent" not in BUILDER_STATE_HANDLERS[BuilderState.SUPERVISOR]
    assert "finish_building_agent" not in BUILDER_STATE_HANDLERS[BuilderState.HELP_BUILDING]
    assert "finish_building_agent" not in BUILDER_STATE_HANDLERS[BuilderState.HELP_GENERAL]
    assert "finish_building_agent" in BUILDER_STATE_HANDLERS[BuilderState.BUILDER]


def test_both_help_states_can_reach_each_other_and_the_supervisor():
    """Each Help state can hand off directly to its sibling and to the
    Supervisor (the escape hatch for when it's unsure what's being asked) -
    neither Help state has an execution/config tool of its own."""
    assert "transfer_to_help_general" in BUILDER_STATE_HANDLERS[BuilderState.HELP_BUILDING]
    assert "transfer_to_supervisor" in BUILDER_STATE_HANDLERS[BuilderState.HELP_BUILDING]
    assert "transfer_to_help_building" in BUILDER_STATE_HANDLERS[BuilderState.HELP_GENERAL]
    assert "transfer_to_supervisor" in BUILDER_STATE_HANDLERS[BuilderState.HELP_GENERAL]


def test_supervisor_and_builder_both_have_both_help_transfers_and_resume_paused_chat():
    """These tool names are intentionally duplicated across supervisor and
    builder_agent; neither Help state has resume_paused_chat."""
    for state in (BuilderState.HELP_BUILDING, BuilderState.HELP_GENERAL):
        assert "resume_paused_chat" not in BUILDER_STATE_HANDLERS[state]
    for tool_name in ("transfer_to_help_building", "transfer_to_help_general", "resume_paused_chat"):
        assert tool_name in BUILDER_STATE_HANDLERS[BuilderState.SUPERVISOR]
        assert tool_name in BUILDER_STATE_HANDLERS[BuilderState.BUILDER]


def test_no_execution_only_tool_leaks_into_either_help_handler_set():
    # Supervisor and Builder are the deliberate ADR 0062-style exceptions
    # (both talk to the agent's own supervised owner) - neither Help state
    # must ever see an execution-only tool.
    execution_only_tools = {
        "send_message",
        "reply_message",
        "create_chat",
        "leave_group",
        "read_history",
        "update_own_triggers",
        "search_messages",
        "get_knowledge_index",
        "fetch_chunk",
        "pause_and_escalate",
    }
    for state in (BuilderState.HELP_BUILDING, BuilderState.HELP_GENERAL):
        assert not (execution_only_tools & set(BUILDER_STATE_HANDLERS[state]))


@pytest.mark.parametrize("state", list(BuilderState))
def test_every_handler_in_every_state_is_an_async_callable(state):
    import inspect

    for tool_name, handler in BUILDER_STATE_HANDLERS[state].items():
        assert inspect.iscoroutinefunction(handler), f"{state}/{tool_name} handler must be async"
