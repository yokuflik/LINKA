"""One-off-action / Clarify / Builder / Help flow (ADR 0049, ADR 0064,
restructured by ADR 0093) -
modules/agents/builder_flow.py + modules/agents/tools/builder_handoff.py.

Behavioral contract exercised here (written against ADR 0093 Phase 3 / the
`.claude_docs/ai_agent.md` description of intended behavior, not against
however the code happens to already work):

- BuilderState has exactly five values (one_off_action/clarify/builder_agent/
  help_general/help_agent_building) and BUILDER_STATE_PROMPTS/
  get_builder_state_prompt cover all five, each with a distinct, non-empty
  prompt.
- get_builder_state_prompt raises for anything that isn't already a
  BuilderState member - callers must coerce a stored string through
  BuilderState(...) first (same convention as personas.get_persona_system_prompt),
  it must never silently fall back to a default prompt.
- No transfer_to_* tool exists anywhere anymore (ADR 0093 Phase 3): the only
  way builder_state changes is modules/agents/owner_chat_router.py::
  route_owner_turn, run once per config-mode turn by invoke_worker.py -
  never a model-called tool.
- finish_building_agent writes only is_enabled=True (no builder_state write -
  the Phase 1 stopgap self-transition is gone), then calls sync_agent_cache
  exactly once with the freshly updated agent - this is the one handoff tool
  that changes is_enabled and therefore must resync the cache. If
  update_agent_config raises, sync_agent_cache must never be called (no
  half-applied cache sync after a failed write).
- BUILDER_STATE_HANDLERS wires each BuilderState to its own tool-name set:
  one_off_action -> {resume_paused_chat, resolve_user, find_chat_by_name,
  spawn_ephemeral_task, no_reply_needed, save_knowledge_from_text,
  schedule_one_off_task} + the full execution-mode toolset (ADR 0062,
  inherited from the old supervisor state);
  clarify -> {no_reply_needed} only (ADR 0093, zero-action state);
  builder_agent -> {set_agent_persona, update_agent_rules, set_agent_identity,
  set_trigger, get_agent_status, estimate_api_usage, update_own_triggers,
  resolve_user, find_chat_by_name, no_reply_needed, finish_building_agent}
  - the ADR 0062 execution-tool union (and resume_paused_chat/
  spawn_ephemeral_task/save_knowledge_from_text/schedule_one_off_task) are
  removed by ADR 0093: builder_agent owns persistent configuration only;
  help_agent_building -> {no_reply_needed} only; help_general ->
  {no_reply_needed} only - both purely zero-action, no transfer tool left
  either (ADR 0093 Phase 3). one_off_action is the one deliberate exception
  to the config/execution no-overlap invariant now (ADR 0062); builder_agent
  and both Help states keep it strictly.
  get_capacity_status was removed entirely (dead tool, never actually wired
  into any builder_state's schema list).
  update_own_triggers was removed from EXECUTION_TOOL_HANDLERS and is no
  longer reachable by execution-mode personas talking to a third party
  (prompt-injection surface: it let attacker-controlled chat text rewrite
  the agent's own wake-up triggers) - ADR 0093 also removed it from
  one_off_action (ex-supervisor), leaving it config-mode-only and
  builder_agent-only.
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


def _agent(builder_state: str = BuilderState.ONE_OFF_ACTION.value) -> SimpleNamespace:
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


def test_builder_state_has_exactly_five_values():
    assert {s.value for s in BuilderState} == {
        "one_off_action",
        "clarify",
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
    assert get_builder_state_prompt("one_off_action") == BUILDER_STATE_PROMPTS[BuilderState.ONE_OFF_ACTION]


def test_get_builder_state_prompt_raises_for_an_unrecognized_string():
    with pytest.raises(KeyError):
        get_builder_state_prompt("not_a_real_state")


def test_builder_state_construction_raises_for_an_unknown_value():
    """The actual enforcement point: coercing an untrusted/stored string
    through BuilderState(...) - as every real caller (dispatch.py,
    builder_handoff.py, owner_chat_router.py) does - raises for anything not
    a real state."""
    with pytest.raises(ValueError):
        BuilderState("not_a_real_state")


def test_builder_state_construction_rejects_the_old_supervisor_value():
    """ADR 0093: "supervisor" is no longer a valid BuilderState - the
    seeded/existing dev-DB backfill (scripts/init_db.py) exists exactly
    because this would otherwise raise for any pre-ADR-0093 agent row."""
    with pytest.raises(ValueError):
        BuilderState("supervisor")


def test_no_transfer_tool_exists_anywhere():
    """ADR 0093 Phase 3: every transfer_to_* handoff tool is gone - the
    router is the sole mechanism that changes builder_state."""
    for state in BuilderState:
        for tool_name in BUILDER_STATE_HANDLERS[state]:
            assert not tool_name.startswith("transfer_to_"), (
                f"{state}/{tool_name}: transfer_to_* tools were deleted by ADR 0093 Phase 3"
            )
    assert not hasattr(builder_handoff_module, "_tool_transfer_to_builder")
    assert not hasattr(builder_handoff_module, "_tool_transfer_to_help_building")
    assert not hasattr(builder_handoff_module, "_tool_transfer_to_help_general")
    assert not hasattr(builder_handoff_module, "_tool_transfer_to_one_off_action")


# --- finish_building_agent -----------------------------------------------------


@pytest.mark.asyncio
async def test_finish_building_agent_sets_only_is_enabled(monkeypatch, fake_session):
    """ADR 0093 Phase 3 final shape: no builder_state write at all - the
    next config-mode turn's router pass decides where the owner lands
    next."""
    captured_patch = {}

    async def _fake_update(session, agent, patch):
        captured_patch.update(patch)
        agent.is_enabled = patch["is_enabled"]
        return agent

    async def _fake_sync(agent):
        pass

    monkeypatch.setattr(builder_handoff_module, "update_agent_config", _fake_update)
    monkeypatch.setattr(builder_handoff_module, "sync_agent_cache", _fake_sync)

    agent = _agent(builder_state=BuilderState.BUILDER.value)
    agent.is_enabled = False
    result = await builder_handoff_module._tool_finish_building_agent(fake_session, agent, {})

    assert captured_patch == {"is_enabled": True}
    assert agent.builder_state == BuilderState.BUILDER.value
    assert result == {"status": "agent_activated"}


@pytest.mark.asyncio
async def test_finish_building_agent_calls_sync_agent_cache_exactly_once_with_updated_agent(
    monkeypatch, fake_session
):
    sync_calls = []

    async def _fake_update(session, agent, patch):
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


def test_one_off_action_handler_set_is_exactly_the_expected_tools():
    # ADR 0062/0093: one_off_action (ex-supervisor) gets its own tools plus
    # the full execution-mode toolset, so the owner can act directly ("send X
    # a message") from their own agent chat. No transfer_to_* tool - the
    # router decides the next turn's state. ADR 0095: also gets
    # update_own_triggers (disposable-only here) + delete_own_trigger, fixing
    # a prompt/registry drift bug - a permanent trigger still requires
    # BuilderState.BUILDER.
    from modules.agents.tools.execution import EXECUTION_TOOL_HANDLERS

    assert set(BUILDER_STATE_HANDLERS[BuilderState.ONE_OFF_ACTION]) == {
        "resume_paused_chat",
        "resolve_user",
        "find_chat_by_name",
        "spawn_ephemeral_task",
        "start_goal_task",
        "cancel_goal_task",
        "no_reply_needed",
        "save_knowledge_from_text",
        "schedule_one_off_task",
        "update_own_triggers",
        "delete_own_trigger",
        # ADR 0098: pause_and_escalate is excluded (it would pause the owner's own chat).
        *(set(EXECUTION_TOOL_HANDLERS) - {"pause_and_escalate"}),
    }
    assert "pause_and_escalate" not in BUILDER_STATE_HANDLERS[BuilderState.ONE_OFF_ACTION]


def test_clarify_handler_set_is_only_no_reply_needed():
    # ADR 0093: zero-action state - the model can only end the turn silently
    # or (per CLARIFY_PROMPT) ask a plain-text question; it has no tool that
    # touches a real chat or writes config.
    assert set(BUILDER_STATE_HANDLERS[BuilderState.CLARIFY]) == {"no_reply_needed"}


def test_builder_handler_set_includes_every_expected_config_tool():
    # ADR 0093 Phase 3: the execution-tool union (and resume_paused_chat/
    # spawn_ephemeral_task/save_knowledge_from_text/schedule_one_off_task,
    # all moved to one_off_action) are gone, and so is every transfer_to_*
    # tool - builder_agent keeps only persistent-configuration tools plus
    # finish_building_agent.
    expected = {
        "set_agent_persona",
        "update_agent_rules",
        "set_agent_identity",
        "set_trigger",
        "get_agent_status",
        "estimate_api_usage",
        "update_own_triggers",
        "delete_own_trigger",
        "resolve_user",
        "find_chat_by_name",
        "no_reply_needed",
        "finish_building_agent",
    }
    assert set(BUILDER_STATE_HANDLERS[BuilderState.BUILDER]) == expected


def test_builder_handler_set_has_no_execution_only_tool():
    # ADR 0093's central change: builder_agent no longer unions in the full
    # execution-mode toolset (ADR 0062 is now one_off_action-only).
    from modules.agents.tools.execution import EXECUTION_TOOL_HANDLERS

    execution_only_names = set(EXECUTION_TOOL_HANDLERS) - {"no_reply_needed"}
    assert not (execution_only_names & set(BUILDER_STATE_HANDLERS[BuilderState.BUILDER]))


def test_help_building_handler_set_is_only_no_reply_needed():
    # ADR 0093 Phase 3: no transfer tool left - purely zero-action, same as
    # CLARIFY. ADR 0092: factual knowledge is inlined into the system prompt
    # (help_docs.py), not a tool call.
    assert set(BUILDER_STATE_HANDLERS[BuilderState.HELP_BUILDING]) == {"no_reply_needed"}


def test_help_general_handler_set_is_only_no_reply_needed():
    assert set(BUILDER_STATE_HANDLERS[BuilderState.HELP_GENERAL]) == {"no_reply_needed"}


def test_only_builder_has_finish_building_agent():
    assert "finish_building_agent" not in BUILDER_STATE_HANDLERS[BuilderState.ONE_OFF_ACTION]
    assert "finish_building_agent" not in BUILDER_STATE_HANDLERS[BuilderState.CLARIFY]
    assert "finish_building_agent" not in BUILDER_STATE_HANDLERS[BuilderState.HELP_BUILDING]
    assert "finish_building_agent" not in BUILDER_STATE_HANDLERS[BuilderState.HELP_GENERAL]
    assert "finish_building_agent" in BUILDER_STATE_HANDLERS[BuilderState.BUILDER]


def test_resume_paused_chat_is_one_off_action_only():
    # ADR 0093: one-shot action, not persistent config - never duplicated
    # into builder_agent or either Help state.
    assert "resume_paused_chat" in BUILDER_STATE_HANDLERS[BuilderState.ONE_OFF_ACTION]
    for state in (BuilderState.CLARIFY, BuilderState.BUILDER, BuilderState.HELP_BUILDING, BuilderState.HELP_GENERAL):
        assert "resume_paused_chat" not in BUILDER_STATE_HANDLERS[state]


def test_no_execution_only_tool_leaks_into_either_help_handler_set():
    # one_off_action is the deliberate ADR 0062-style exception (talks to the
    # agent's own supervised owner) - neither Help state must ever see a
    # tool that touches a real chat or writes config. ADR 0092 dropped the
    # ADR 0084 knowledge-base-tool carve-out too, so there is no exception
    # left for Help states. ADR 0093 also removed the execution-tool union
    # from builder_agent, but this test is scoped to Help as before.
    execution_only_tools = {
        "send_message",
        "reply_message",
        "leave_group",
        "read_history",
        "search_messages",
        "pause_and_escalate",
        "search_knowledge_semantic",
        "get_knowledge_index",
        "fetch_chunk",
    }
    for state in (BuilderState.HELP_BUILDING, BuilderState.HELP_GENERAL):
        assert not (execution_only_tools & set(BUILDER_STATE_HANDLERS[state]))


@pytest.mark.parametrize("state", list(BuilderState))
def test_every_handler_in_every_state_is_an_async_callable(state):
    import inspect

    for tool_name, handler in BUILDER_STATE_HANDLERS[state].items():
        assert inspect.iscoroutinefunction(handler), f"{state}/{tool_name} handler must be async"
