"""Builder sub-state tools (ADR 0049, split from tools.py; restructured by
ADR 0093).

Only reachable from within config mode's builder_agent/one_off_action/help
states (see BUILDER_STATE_HANDLERS below) - never from an execution-mode
chat. All writes go through update_agent_config, the same single write path
every other config tool uses.

ADR 0093 Phase 3: the transfer_to_* handoff tools/handlers that used to live
here are deleted - builder_state now changes only via
modules/agents/owner_chat_router.py::route_owner_turn, which runs once
before every config-mode turn in invoke_worker.py. No tool in this module
writes builder_state anymore.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from modules.agents.builder_flow import BuilderState
from modules.agents.cache import sync_agent_cache
from modules.agents.crud import update_agent_config
from modules.agents.models import Agent
from modules.agents.tools.config_mode import (
    CONFIG_TOOL_HANDLERS,
    _tool_find_chat_by_name,
    _tool_no_reply_needed,
    _tool_resolve_user,
    _tool_resume_paused_chat,
    _tool_save_knowledge_from_text,
    _tool_spawn_ephemeral_task,
)
from modules.agents.tools.goal_task_tools import _tool_cancel_goal_task, _tool_start_goal_task
from modules.agents.tools.execution import (
    EXECUTION_TOOL_HANDLERS,
    _tool_delete_own_trigger,
    _tool_update_own_triggers,
    _tool_update_own_triggers_disposable,
)


async def _tool_finish_building_agent(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Wrap-up signal, not a bulk config-apply - the config tools already save
    incrementally during the interview (ADR 0049, confirmed with the user
    2026-09-24). Auto-enables the agent, unlike every other config tool
    write, so sync_agent_cache is required here (is_enabled is part of the
    trigger pre-filter cache payload).

    ADR 0093 Phase 3 final shape: sets only is_enabled - no builder_state
    write. The next config-mode turn's router pass decides where the owner
    lands next (almost always one_off_action, since the interview is done),
    rather than this tool guessing/hardcoding a destination."""
    updated = await update_agent_config(session, agent, {"is_enabled": True})
    await sync_agent_cache(updated)
    return {"status": "agent_activated"}


# Extends the config-mode gate (dispatch.is_config_mode - chat_id decides
# execution vs. config, unchanged). Within config mode, agent.builder_state
# picks one of these disjoint handler sets - never a fallback to another
# state's tools, same one-decision-point discipline as is_config_mode itself.
# ADR 0093 Phase 3: no handler set writes builder_state anymore - every
# transfer_to_* handler is gone, replaced entirely by
# owner_chat_router.py::route_owner_turn running once before every
# config-mode turn (invoke_worker.py).
BUILDER_STATE_HANDLERS = {
    BuilderState.ONE_OFF_ACTION: {
        # ADR 0062: the owner's own chat, while idle/routing, also gets the
        # full execution-mode toolset so a direct "send X a message"-style
        # command can be carried out directly. Handler bodies are unchanged -
        # same Agent.restrictions/quota enforcement as any other
        # execution-mode invocation.
        # pause_and_escalate excluded (ADR 0098): it always pauses the
        # triggering chat, which here is the owner's own agent chat.
        **{k: v for k, v in EXECUTION_TOOL_HANDLERS.items() if k != "pause_and_escalate"},
        # ADR 0055/0093: an action, not a setup step - stays reachable here.
        "resume_paused_chat": _tool_resume_paused_chat,
        # ADR 0061/0062/0093: resolving a named person, spawning a one-off
        # relay-and-summarize task, saving reference text, and scheduling a
        # one-time future action are all one-shot actions, not persistent
        # config.
        "resolve_user": _tool_resolve_user,
        "find_chat_by_name": _tool_find_chat_by_name,
        "spawn_ephemeral_task": _tool_spawn_ephemeral_task,
        # ADR 0099: multi-turn goal-driven conversation + its cancel.
        "start_goal_task": _tool_start_goal_task,
        "cancel_goal_task": _tool_cancel_goal_task,
        "no_reply_needed": _tool_no_reply_needed,
        "save_knowledge_from_text": _tool_save_knowledge_from_text,
        "schedule_one_off_task": CONFIG_TOOL_HANDLERS["schedule_one_off_task"],
        # ADR 0095: disposable-only trigger write access (every entry must
        # carry expires_at/max_fires, enforced server-side in the handler) -
        # a permanent trigger still requires BuilderState.BUILDER. Also fixes
        # a pre-existing drift bug: ONE_OFF_ACTION_PROMPT already mentioned
        # update_own_triggers but it was never actually wired in here.
        "update_own_triggers": _tool_update_own_triggers_disposable,
        "delete_own_trigger": _tool_delete_own_trigger,
    },
    # ADR 0093: zero-action state - only no_reply_needed. The router lands
    # here when it can't confidently distinguish one_off_action from
    # builder_agent; the model must ask a disambiguating question in plain
    # text instead of calling a tool.
    BuilderState.CLARIFY: {
        "no_reply_needed": _tool_no_reply_needed,
    },
    BuilderState.BUILDER: {
        # ADR 0093: the ADR 0062 execution-tool union is removed - builder_agent
        # owns persistent configuration only.
        "set_agent_persona": CONFIG_TOOL_HANDLERS["set_agent_persona"],
        "update_agent_rules": CONFIG_TOOL_HANDLERS["update_agent_rules"],
        "set_agent_identity": CONFIG_TOOL_HANDLERS["set_agent_identity"],
        "set_trigger": CONFIG_TOOL_HANDLERS["set_trigger"],
        "get_agent_status": CONFIG_TOOL_HANDLERS["get_agent_status"],
        "estimate_api_usage": CONFIG_TOOL_HANDLERS["estimate_api_usage"],
        "update_own_triggers": _tool_update_own_triggers,
        "delete_own_trigger": _tool_delete_own_trigger,
        "resolve_user": _tool_resolve_user,
        "find_chat_by_name": _tool_find_chat_by_name,
        "no_reply_needed": _tool_no_reply_needed,
        "finish_building_agent": _tool_finish_building_agent,
    },
    # ADR 0064/0093: two disjoint Help personas, purely zero-action - no
    # execution/config tool and no transfer tool, only no_reply_needed. ADR
    # 0092: their factual knowledge comes from static reference docs inlined
    # directly into their system prompts (modules/agents/help_docs.py), not
    # a knowledge-base lookup tool.
    BuilderState.HELP_BUILDING: {
        "no_reply_needed": _tool_no_reply_needed,
    },
    BuilderState.HELP_GENERAL: {
        "no_reply_needed": _tool_no_reply_needed,
    },
}
