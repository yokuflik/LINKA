"""Shared DB factories for agent tests (moved out of
test_invoke_worker_schedule.py, AGENT_RUN_TURN_TEST_PLAN.md Step 1) - every
`_run_turn`-adjacent test file needs a user/chat/agent to exist in the real
ephemeral test DB (ADR 0032), so this is the one place that builds them.
"""
import json

from sqlalchemy.ext.asyncio import AsyncSession

from modules.agents.models import Agent, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.agents.schedule import sync_schedule_zset
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.users.crud import create_user

_ID = 0


def next_id() -> int:
    global _ID
    _ID += 1
    return 990_000_000 + _ID


async def make_user(session: AsyncSession) -> int:
    user_id = next_id()
    await create_user(session, user_id=user_id, phone_number=f"+1555{user_id}")
    return user_id


async def make_chat(session: AsyncSession, *user_ids: int) -> int:
    chat_id = next_id()
    await create_chat(session, chat_id=chat_id, is_group=False)
    for uid in user_ids:
        await add_participant_to_chat(session, chat_id=chat_id, user_id=uid)
    return chat_id


async def make_agent(
    session: AsyncSession,
    owner_user_id: int,
    owner_agent_chat_id: int,
    *,
    is_enabled: bool = True,
    on_schedule: list | None = None,
    builder_state: str = "one_off_action",
) -> Agent:
    triggers = json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS))
    triggers["on_schedule"] = on_schedule or []
    agent = Agent(
        id=next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        is_enabled=is_enabled,
        builder_state=builder_state,
        triggers=triggers,
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    await sync_schedule_zset(agent)
    return agent
