"""Knowledge-fired turns through the real `_run_turn` body (ADR 0085,
AGENT_RUN_TURN_TEST_PLAN.md Step 6). A knowledge-notice turn always targets
the owner-agent chat (config-mode by construction - `chat_id ==
agent.owner_agent_chat_id`), is seeded by `_build_knowledge_contents` from a
fully-formed instruction string, and has no triggering message/judge
involved. Only `invoke_turn_helpers.generate_turn` is mocked - the rest of
`_run_turn` (DB session, `_post_config_reply`, token accounting) runs for
real against the ephemeral test DB (ADR 0032) and real Redis.
"""
import pytest

from modules.agents.invoke_worker import _run_turn
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import get_chat_messages
from tests.modules.agents._factories import make_agent, make_chat, make_user
from tests.modules.agents._gemini_stub import mock_gemini_turn, text_result

pytestmark = pytest.mark.asyncio


async def test_knowledge_fired_turn_posts_report_to_owner_agent_chat(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat)

    with mock_gemini_turn(text_result("I've added your new pricing sheet to my knowledge base.")):
        await _run_turn(
            agent.id,
            owner_chat,
            knowledge_instruction="Document 'pricing.pdf' was successfully ingested (12 chunks).",
        )

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 1
    reply = messages[0]
    assert reply.content == "I've added your new pricing sheet to my knowledge base."
    assert reply.sender_agent_id == agent.id
    assert reply.type == AGENT_REPLY_MESSAGE_TYPE
