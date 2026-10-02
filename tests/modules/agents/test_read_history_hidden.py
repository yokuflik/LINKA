"""read_history is hidden from Gemini while the chat has no messages."""
import pytest

from modules.agents.invoke_turn_helpers import drop_read_history_if_chat_empty
from modules.agents.tools import get_tool_schemas_for_chat
from modules.chats.models.chat import Chat
from tests.modules.agents._factories import make_agent, make_chat, make_user

pytestmark = pytest.mark.asyncio


async def test_read_history_hidden_for_empty_chat_only(db_session):
    owner = await make_user(db_session)
    peer = await make_user(db_session)
    agent = await make_agent(db_session, owner, await make_chat(db_session, owner))
    chat_id = await make_chat(db_session, owner, peer)
    schemas = get_tool_schemas_for_chat(agent, chat_id)

    empty = await drop_read_history_if_chat_empty(db_session, schemas, chat_id)
    assert "read_history" not in {s["name"] for s in empty}
    assert "send_message" in {s["name"] for s in empty}

    chat = await db_session.get(Chat, chat_id)
    chat.last_message_id = 123
    await db_session.flush()
    full = await drop_read_history_if_chat_empty(db_session, schemas, chat_id)
    assert "read_history" in {s["name"] for s in full}
