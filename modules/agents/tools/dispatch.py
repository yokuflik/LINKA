"""Hard tool-mode gate + execute_tool_call (ADR 0047 decision 4, split from
tools.py; security-critical).

Which tool schemas are sent to Gemini is decided purely by the triggering
chat_id, never by active_skill, system_prompt, or anything the model says
about itself. chat_id == agent.owner_agent_chat_id -> config mode; any
other chat, or a schedule-fired turn (chat_id is None or not the owner-
agent chat) -> execution mode. This is a hard boundary: an end customer
sending a prompt-injection payload into an execution-mode chat must be
structurally unable to reach a config tool, regardless of what the model
decides to believe.
"""
import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from modules.agents.builder_flow import BuilderState
from modules.agents.models import Agent
from modules.agents.tools.builder_handoff import BUILDER_STATE_HANDLERS
from modules.agents.tools.common import ToolDeniedError, _log_call
from modules.agents.tools.execution import CHAT_SCOPED_TOOL_NAMES, EXECUTION_TOOL_HANDLERS
from modules.agents.tools.schemas import BUILDER_STATE_TOOL_SCHEMAS, TOOL_SCHEMAS

logger = logging.getLogger(__name__)


def is_config_mode(agent: Agent, chat_id: Optional[int]) -> bool:
    """The sole decision point for tool-mode selection. chat_id is the
    triggering chat: None (schedule-fired turn with no chat target) is never
    config mode - config mode requires an explicit, real owner_agent_chat_id
    match, not the absence of a chat."""
    return chat_id is not None and chat_id == agent.owner_agent_chat_id


def get_tool_schemas_for_chat(agent: Agent, chat_id: Optional[int]) -> list:
    """Mode-aware schema selection (ADR 0047 decision 4) - the only inputs
    are agent and the triggering chat_id, never active_skill/system_prompt/
    anything model-controlled. Within config mode, ADR 0049 further narrows
    to the current builder_state's own disjoint schema set."""
    if not is_config_mode(agent, chat_id):
        return TOOL_SCHEMAS
    return BUILDER_STATE_TOOL_SCHEMAS[BuilderState(agent.builder_state)]


async def execute_tool_call(
    session: AsyncSession, agent: Agent, tool_name: str, arguments: dict, chat_id: Optional[int] = None,
) -> dict[str, Any]:
    """Runs one tool call for `agent`, enforcing Agent.restrictions, and
    always logs the attempt (allowed or denied) to AgentToolCallLog. Returns
    the dict handed back to Gemini as the function response - on denial this
    is {"error": reason} so the model can see why and adjust, rather than
    the log being the only record.

    `chat_id` is the turn's triggering chat (None for a schedule-fired turn
    with no chat target) - ADR 0047 decision 4's defense-in-depth recheck:
    independently re-derives the mode-appropriate allowlist here and rejects
    a mismatched tool_name even if the wrong schema list were ever leaked to
    Gemini by a bug upstream (same belt-and-suspenders pattern as the
    existing Agent.is_enabled recheck at worker dequeue time). ADR 0049
    further narrows the config-mode allowlist to the current builder_state."""
    config_mode = is_config_mode(agent, chat_id)
    if config_mode:
        mode_label = f"config/{agent.builder_state}"
        handlers = BUILDER_STATE_HANDLERS[BuilderState(agent.builder_state)]
    else:
        mode_label = "execution"
        handlers = EXECUTION_TOOL_HANDLERS
    allowlist = frozenset(handlers)

    if tool_name not in allowlist:
        logger.warning(
            "agent %s tool %s denied: not in %s-mode allowlist",
            agent.id, tool_name, mode_label,
        )
        await _log_call(
            session, agent.id, tool_name, arguments, allowed=False,
            denial_reason=f"tool not available in {mode_label} mode",
        )
        return {"error": f"tool '{tool_name}' is not available in this context"}

    handler = handlers.get(tool_name)
    if handler is None:
        await _log_call(session, agent.id, tool_name, arguments, allowed=False, denial_reason="unknown tool")
        return {"error": f"unknown tool: {tool_name}"}

    try:
        if tool_name in CHAT_SCOPED_TOOL_NAMES:
            result = await handler(session, agent, arguments, chat_id=chat_id)
        else:
            result = await handler(session, agent, arguments)
    except ToolDeniedError as exc:
        logger.info("agent %s tool %s denied: %s", agent.id, tool_name, exc.reason)
        await _log_call(session, agent.id, tool_name, arguments, allowed=False, denial_reason=exc.reason)
        return {"error": exc.reason}
    except Exception as exc:
        logger.exception("agent %s tool %s failed", agent.id, tool_name)
        await _log_call(session, agent.id, tool_name, arguments, allowed=False, denial_reason=f"error: {exc}")
        return {"error": str(exc)}

    await _log_call(session, agent.id, tool_name, arguments, allowed=True, denial_reason=None)
    return result
