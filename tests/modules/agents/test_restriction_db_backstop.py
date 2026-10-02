"""DB-level Agent.restrictions backstop (ADR 0066) + leak fixes (ADR 0097).

Every test here writes through the messaging/chat CRUD directly - never via
the tool handlers - to prove Postgres itself refuses, independent of Python
checks. Also covers the blocked_read_chat_ids filter on owner-wide search.
"""
import pytest
from sqlalchemy.exc import DBAPIError

from modules.agents.tools.execution import _tool_search_messages
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.crud import create_message
from tests.modules.agents._factories import make_agent, make_chat, make_user, next_id

pytestmark = pytest.mark.asyncio


async def _setup(session):
    owner = await make_user(session)
    other = await make_user(session)
    owner_chat = await make_chat(session, owner)
    agent = await make_agent(session, owner, owner_chat)
    group_id = next_id()
    await create_chat(session, chat_id=group_id, is_group=True, title="G")
    await add_participant_to_chat(session, chat_id=group_id, user_id=owner)
    private_id = await make_chat(session, owner, other)
    await session.commit()
    return owner, agent, owner_chat, group_id, private_id


async def _set_restriction(session, agent, **kw):
    agent.restrictions = {**agent.restrictions, **kw}
    await session.commit()


async def _send(session, agent, chat_id, content="hello"):
    await create_message(
        session, message_id=next_id(), chat_id=chat_id,
        sender_id=agent.owner_user_id, content=content, sender_agent_id=agent.id,
    )
    await session.commit()


async def test_db_blocks_group_message_when_can_message_groups_false(db_session):
    _, agent, _, group_id, _ = await _setup(db_session)
    await _set_restriction(db_session, agent, can_message_groups=False)
    with pytest.raises(DBAPIError, match="agent_restricted:can_message_groups"):
        await _send(db_session, agent, group_id)


async def test_db_blocks_all_sends_when_can_send_messages_false(db_session):
    _, agent, _, _, private_id = await _setup(db_session)
    await _set_restriction(db_session, agent, can_send_messages=False)
    with pytest.raises(DBAPIError, match="agent_restricted:can_send_messages"):
        await _send(db_session, agent, private_id)


async def test_db_blocks_blocked_chat(db_session):
    _, agent, _, _, private_id = await _setup(db_session)
    await _set_restriction(db_session, agent, blocked_read_chat_ids=[str(private_id)])
    with pytest.raises(DBAPIError, match="agent_restricted:blocked_read_chat_ids"):
        await _send(db_session, agent, private_id)


async def test_owner_chat_still_receives_agent_messages_when_sending_disabled(db_session):
    # ADR 0097: restrictions concern third parties, never the agent's own owner chat.
    _, agent, owner_chat, _, _ = await _setup(db_session)
    await _set_restriction(db_session, agent, can_send_messages=False, can_message_private=False)
    await _send(db_session, agent, owner_chat, "status update")


async def test_global_search_excludes_blocked_chats(db_session):
    owner, agent, _, _, private_id = await _setup(db_session)
    other_chat = await make_chat(db_session, owner)
    for cid in (private_id, other_chat):
        await create_message(
            db_session, message_id=next_id(), chat_id=cid,
            sender_id=owner, content="zebrastripes report",
        )
    await db_session.commit()
    await _set_restriction(db_session, agent, blocked_read_chat_ids=[str(private_id)])

    result = await _tool_search_messages(db_session, agent, {"query": "zebrastripes"})

    chat_ids = {r["chat_id"] for r in result["results"]}
    assert chat_ids == {str(other_chat)}
