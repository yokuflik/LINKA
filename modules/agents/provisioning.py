"""Agent + owner-agent chat provisioning (ADR 0104): shared by POST /agents/me
and the signup path."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from infra.ids.client import next_id
from modules.agents import crud as agent_crud
from modules.agents.cache import sync_agent_cache
from modules.agents.models import Agent
from modules.chats.common import ROLE_MEMBER
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE

_GREETING_TEXT = "Hi, I'm your AI agent. What would you like me to do today?"

logger = logging.getLogger(__name__)


async def provision_agent(session: AsyncSession, user_id: int) -> Agent:
    """Idempotent: returns the existing agent if the user already has one."""
    existing = await agent_crud.get_agent_by_owner(session, user_id)
    if existing is not None:
        return existing

    # Owner-agent chat: a permanent 1:1-shaped chat with the owner as its
    # only participant (the agent has no user_id of its own - it always acts
    # as the owner, ADR 0045). Created eagerly, not lazily, since the daily
    # time-budget notification depends on it existing.
    chat = await create_chat(session, chat_id=await next_id(), is_group=False)
    await add_participant_to_chat(session, chat_id=chat.id, user_id=user_id, role=ROLE_MEMBER)

    agent = Agent(id=await next_id(), owner_user_id=user_id, owner_agent_chat_id=chat.id)
    session.add(agent)
    await session.commit()
    await sync_agent_cache(agent)

    # Opening greeting: a real, persisted message (not a client-side-only
    # placeholder) so it survives reload / shows up on any device. Sent the
    # same way any agent reply is (process_outgoing, AGENT_REPLY_MESSAGE_TYPE)
    # so it's indistinguishable from a normal turn. process_outgoing
    # self-commits, so this runs after the chat/agent transaction above, not
    # inside it. Best-effort, same reasoning as the reset path below: the
    # agent itself already exists at this point, so a transient send-path
    # failure here must not turn into a 500 that hides a successful creation.
    try:
        await message_service.process_outgoing(
            session,
            sender_id=user_id,
            chat_id=chat.id,
            client_message_id=f"agent-greeting-{agent.id}",
            content=_GREETING_TEXT,
            type=AGENT_REPLY_MESSAGE_TYPE,
            sender_agent_id=agent.id,
        )
    except Exception:
        logger.exception("Failed to send greeting after agent creation for user_id=%s", user_id)

    return agent
