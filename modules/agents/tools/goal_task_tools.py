"""Handlers for goal-driven conversational tasks (ADR 0099).

Two groups:
- config-mode (ONE_OFF_ACTION): start_goal_task / cancel_goal_task
- goal-turn (execution-mode turn in the task's target chat): the only tools a
  goal turn may use - GOAL_TASK_HANDLERS, selected by dispatch.py purely from
  agent.triggers + chat_id, never from anything the model says.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from modules.agents.goal_tasks import (
    COMPLETE_OUTCOMES,
    FAIL_REASONS,
    GoalTaskError,
    close_goal_task,
    find_goal_task_for_chat,
    spawn_goal_task,
)
from modules.agents.models import Agent
from modules.agents.tools.common import ToolDeniedError
from modules.agents.tools.execution import EXECUTION_TOOL_HANDLERS


async def _tool_start_goal_task(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    try:
        return await spawn_goal_task(
            session,
            agent,
            chat_id=str(arguments["chat_id"]),
            goal=str(arguments["goal"]),
            done_when=str(arguments["done_when"]),
            constraints=str(arguments.get("constraints") or ""),
            may_commit=bool(arguments.get("may_commit", False)),
            max_turns=arguments.get("max_turns"),
            timeout_minutes=arguments.get("timeout_minutes"),
        )
    except GoalTaskError as exc:
        raise ToolDeniedError(str(exc))


async def _tool_cancel_goal_task(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    match = find_goal_task_for_chat(agent.triggers, str(arguments["chat_id"]))
    if match is None:
        raise ToolDeniedError("no active goal task for that chat")
    await close_goal_task(session, agent, match[0], status="cancelled", summary="The owner cancelled this task.")
    return {"status": "cancelled"}


def _chat_gated(tool_name: str):
    """Wraps an execution handler so it only ever acts on the goal task's own
    chat - prompt injection from the counterpart cannot redirect the agent."""
    handler = EXECUTION_TOOL_HANDLERS[tool_name]

    async def gated(session: AsyncSession, agent: Agent, arguments: dict, *, chat_id: int, task_id: str) -> dict:
        # An omitted chat_id defaults to the task's own chat (the only legal one).
        if arguments.get("chat_id") in (None, ""):
            arguments = {**arguments, "chat_id": str(chat_id)}
        if str(arguments["chat_id"]) != str(chat_id):
            raise ToolDeniedError(f"this task may only act in its own chat; use chat_id={chat_id}")
        return await handler(session, agent, arguments)

    return gated


async def _tool_complete_task(
    session: AsyncSession, agent: Agent, arguments: dict, *, chat_id: int, task_id: str
) -> dict:
    outcome = str(arguments.get("outcome"))
    if outcome not in COMPLETE_OUTCOMES:
        raise ToolDeniedError(f"outcome must be one of {sorted(COMPLETE_OUTCOMES)}")
    await close_goal_task(session, agent, task_id, status=outcome, summary=str(arguments.get("summary") or ""))
    return {"status": "task_closed"}


async def _tool_fail_task(
    session: AsyncSession, agent: Agent, arguments: dict, *, chat_id: int, task_id: str
) -> dict:
    reason = str(arguments.get("reason"))
    if reason not in FAIL_REASONS:
        raise ToolDeniedError(f"reason must be one of {sorted(FAIL_REASONS)}")
    await close_goal_task(session, agent, task_id, status=f"failed: {reason}", summary=str(arguments.get("summary") or ""))
    return {"status": "task_closed"}


GOAL_TASK_HANDLERS = {
    "send_message": _chat_gated("send_message"),
    "reply_message": _chat_gated("reply_message"),
    "read_history": _chat_gated("read_history"),
    "complete_task": _tool_complete_task,
    "fail_task": _tool_fail_task,
}
