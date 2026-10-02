"""Mutable per-turn state shared by the `_run_turn` phase modules
(invoke_turn.py, invoke_turn_pre.py, invoke_turn_loop.py, invoke_turn_steps.py).

Replaces the local variables of the former single-function `_run_turn`
(split out of invoke_worker.py by ADR 0100). Holds state only - no behaviour.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class TurnCtx:
    session: AsyncSession
    agent: Any
    agent_id: int
    chat_id: int | None
    message_id: int | None
    schedule_instruction: str | None
    knowledge_instruction: str | None
    scoped_system_prompt: str | None
    owner_user_id: int
    config_mode_turn: bool
    # "error" until a phase explicitly ends the turn cleanly; read by
    # `_run_turn`'s finally to publish the drawer's done/error status.
    ended_status: str = "error"
    contents: list = field(default_factory=list)
    # The triggering message / instruction text, for the ADR 0096 outcome check.
    goal_text: str = ""
    # ADR 0096: last tool outcome, inspected only at the two turn-ending branches.
    last_tool_error: str | None = None
    last_tool_name: str | None = None
    # ADR 0099: active goal task for this chat (a `begin_goal_turn` result), if any.
    goal_turn: Any = None
    goal_acted: bool = False
    # Explicit flag rather than `round_trip == 0` (see steps.wait_for_gemini_budget).
    gemini_call_made: bool = False
    peer_typing_task: asyncio.Task | None = None
    # ADR 0102: successful `continue_message` calls so far this turn.
    continuations_used: int = 0
