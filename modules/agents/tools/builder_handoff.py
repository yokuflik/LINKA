"""Builder sub-state handoff tools (ADR 0049, split from tools.py).

Only reachable from within config mode's builder_agent/supervisor/help_agent
states (see BUILDER_STATE_HANDLERS below) - never from an execution-mode
chat. All writes go through update_agent_config, the same single write path
every other config tool uses.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from modules.agents.builder_flow import BuilderState
from modules.agents.cache import sync_agent_cache
from modules.agents.crud import update_agent_config
from modules.agents.models import Agent
from modules.agents.tools.config_mode import (
    CONFIG_TOOL_HANDLERS,
    _tool_no_reply_needed,
    _tool_resolve_user,
    _tool_resume_paused_chat,
    _tool_spawn_ephemeral_task,
)
from modules.agents.tools.execution import EXECUTION_TOOL_HANDLERS


async def _tool_transfer_to_builder(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    updated = await update_agent_config(session, agent, {"builder_state": BuilderState.BUILDER.value})
    # invoke_worker.py now re-derives system_prompt/tool_schemas every
    # round-trip (ADR 0049 handoff fix, 2026-09-25), so the very next Gemini
    # call in this same turn already runs as the Builder with its own tools.
    # Nudge it explicitly to act on the user's own message that triggered the
    # handoff instead of just acknowledging the transfer - without this the
    # model tends to emit a generic "you're now with the builder" line and
    # stop, leaving the user's actual request unanswered until their next
    # message.
    return {
        "status": "transferred",
        "to": updated.builder_state,
        "instruction": "You are now the Builder Agent. This handoff is invisible to the user - "
        "never mention it, never say anything like 'switching you to the builder'. Just look at "
        "the user's own preceding message in this conversation and respond to it directly, "
        "continuing the interview from there.",
    }


async def _tool_transfer_to_help_building(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    updated = await update_agent_config(session, agent, {"builder_state": BuilderState.HELP_BUILDING.value})
    return {
        "status": "transferred",
        "to": updated.builder_state,
        "instruction": "You are now the Agent-Building Help Agent. This handoff is invisible to "
        "the user - never mention it, never say anything like 'switching you to help'. Just look "
        "at the user's own preceding message in this conversation and answer their actual "
        "question directly.",
    }


async def _tool_transfer_to_help_general(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    updated = await update_agent_config(session, agent, {"builder_state": BuilderState.HELP_GENERAL.value})
    return {
        "status": "transferred",
        "to": updated.builder_state,
        "instruction": "You are now the general Help Agent. This handoff is invisible to the "
        "user - never mention it, never say anything like 'switching you to help'. Just look at "
        "the user's own preceding message in this conversation and answer their actual question "
        "directly.",
    }


async def _tool_transfer_to_supervisor(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Escape hatch out of the Builder interview (or Help) without finishing
    the checklist and without activating the agent - unlike
    _tool_finish_building_agent. Whatever was already saved via the
    incremental config tools stays saved; this only flips builder_state."""
    updated = await update_agent_config(session, agent, {"builder_state": BuilderState.SUPERVISOR.value})
    return {
        "status": "transferred",
        "to": updated.builder_state,
        "instruction": "You are now back with the Supervisor. This handoff is invisible to the "
        "user - never mention it, never say anything like 'switching you back' or 'transferring "
        "you to the regular agent'. Just look at the user's own preceding message in this "
        "conversation and respond to it directly as the Supervisor would (or simply continue the "
        "conversation naturally if they just wanted to pause setup).",
    }


async def _tool_finish_building_agent(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Wrap-up signal, not a bulk config-apply - the 6 config tools already
    save incrementally during the interview (ADR 0049, confirmed with the
    user 2026-09-24). Auto-enables the agent, unlike every other config tool
    write, so sync_agent_cache is required here (is_enabled is part of the
    trigger pre-filter cache payload, unlike builder_state)."""
    updated = await update_agent_config(
        session, agent, {"builder_state": BuilderState.SUPERVISOR.value, "is_enabled": True}
    )
    await sync_agent_cache(updated)
    return {"status": "agent_activated"}


# Extends the config-mode gate (dispatch.is_config_mode - chat_id decides
# execution vs. config, unchanged). Within config mode, agent.builder_state
# picks one of three disjoint handler sets - never a fallback to another
# state's tools, same one-decision-point discipline as is_config_mode itself.
BUILDER_STATE_HANDLERS = {
    BuilderState.SUPERVISOR: {
        # ADR 0062: the owner's own chat, while idle/routing, also gets the
        # full execution-mode toolset so a direct "send X a message"-style
        # command can be carried out without first transferring into the
        # Builder interview flow. Handler bodies are unchanged - same
        # Agent.restrictions/quota enforcement as any other execution-mode
        # invocation.
        **EXECUTION_TOOL_HANDLERS,
        "transfer_to_builder": _tool_transfer_to_builder,
        "transfer_to_help_building": _tool_transfer_to_help_building,
        "transfer_to_help_general": _tool_transfer_to_help_general,
        # ADR 0055: reachable straight from the Supervisor, without a full
        # Builder interview first - it's an action, not a setup step.
        "resume_paused_chat": _tool_resume_paused_chat,
        # ADR 0061/0062: resolving a named person and spawning a one-off
        # relay-and-summarize task are config-mode-only tools, not part of
        # EXECUTION_TOOL_HANDLERS - added explicitly so Supervisor's
        # "message X and tell me what they say" path actually has both ends.
        "resolve_user": _tool_resolve_user,
        "spawn_ephemeral_task": _tool_spawn_ephemeral_task,
        "no_reply_needed": _tool_no_reply_needed,
    },
    BuilderState.BUILDER: {
        # Same ADR 0062 reasoning as Supervisor: the Builder is talking to
        # its own supervised owner, so it also gets the full execution-mode
        # toolset unioned in - a mid-interview "actually, send X a message"
        # request works directly instead of needing transfer_to_supervisor
        # first. Handler bodies unchanged - same restrictions/quota
        # enforcement as any other execution-mode call.
        **EXECUTION_TOOL_HANDLERS,
        **CONFIG_TOOL_HANDLERS,
        "transfer_to_help_building": _tool_transfer_to_help_building,
        "transfer_to_help_general": _tool_transfer_to_help_general,
        "transfer_to_supervisor": _tool_transfer_to_supervisor,
        "finish_building_agent": _tool_finish_building_agent,
    },
    # ADR 0064: two disjoint Help personas, neither with any execution/config
    # tool - only transfer tools, same zero-action posture the original
    # single help_agent state had.
    BuilderState.HELP_BUILDING: {
        "transfer_to_builder": _tool_transfer_to_builder,
        "transfer_to_help_general": _tool_transfer_to_help_general,
        "transfer_to_supervisor": _tool_transfer_to_supervisor,
        "no_reply_needed": _tool_no_reply_needed,
    },
    BuilderState.HELP_GENERAL: {
        "transfer_to_help_building": _tool_transfer_to_help_building,
        "transfer_to_supervisor": _tool_transfer_to_supervisor,
        "no_reply_needed": _tool_no_reply_needed,
    },
}
