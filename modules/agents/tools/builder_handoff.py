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
from modules.agents.tools.config_mode import CONFIG_TOOL_HANDLERS, _tool_resume_paused_chat


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
        "instruction": "You are now the Builder Agent. Do not just announce the handoff - "
        "look at the user's own preceding message in this conversation and respond to it "
        "directly, continuing the interview from there.",
    }


async def _tool_transfer_to_help(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    updated = await update_agent_config(session, agent, {"builder_state": BuilderState.HELP.value})
    return {
        "status": "transferred",
        "to": updated.builder_state,
        "instruction": "You are now the Help Agent. Do not just announce the handoff - "
        "look at the user's own preceding message in this conversation and answer their "
        "actual question directly.",
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
        "transfer_to_builder": _tool_transfer_to_builder,
        "transfer_to_help": _tool_transfer_to_help,
        # ADR 0055: reachable straight from the Supervisor, without a full
        # Builder interview first - it's an action, not a setup step.
        "resume_paused_chat": _tool_resume_paused_chat,
    },
    BuilderState.BUILDER: {
        **CONFIG_TOOL_HANDLERS,
        "transfer_to_help": _tool_transfer_to_help,
        "finish_building_agent": _tool_finish_building_agent,
    },
    BuilderState.HELP: {"transfer_to_builder": _tool_transfer_to_builder},
}
