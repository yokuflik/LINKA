"""LLM Judge / Semantic Router gate (ADR 0053, jev classification backend
ADR 0076) - modules/agents/judge.py.

Runs against the real ephemeral Postgres (ADR 0032) and real Redis
(`redis_db`), since evaluate_message reads/writes AgentJudgeLog and queries
Message for the follow-up check, and the rate limit is a real Redis
fixed-window counter. What's mocked is the TypeSafe `classify` call (the
classification backend) and, on the reject path, the Gemini
`generate_structured` call (redirect-text generation only) - no real HTTP
calls, no tokens spent.

Behavioral expectations encoded here (confirmed with the user, not just
"whatever the code does"):

- Fail-open is the rule for every *classification* technical failure path: a
  `classify` call that raises TypeSafeError (or returns a malformed/
  incomplete response -> KeyError/TypeError/ValueError) must resolve to
  is_approved=True, never crash the turn, and never block the real agent
  response.
- The per-agent rate limit for the judge itself is also a fail-open path:
  over budget -> is_approved=True (with no redirect_message), not a
  rejection. The rejection path is reserved for jev actually running and
  saying no.
- A message with no text content (media-only, no caption) is auto-approved
  without ever calling jev and without consuming rate-limit budget.
- A genuine rejection (jev's on_topic below threshold, or any malicious
  sub-check above threshold) triggers exactly one Gemini call to author
  redirect_message; a failure in THAT call does not flip the verdict back to
  approved - it only leaves redirect_message empty, and the caller
  (invoke_worker) falls back to local_redirect_text.
- Every verdict - approved, rejected, or any fail-open variant - is logged
  to AgentJudgeLog exactly once per evaluate_message call.
- is_follow_up reflects the actual DB state (recent agent reply in the
  chat) independent of whether the verdict path even calls jev.
- evaluate_message never raises - it is a hard fail-open by contract (for
  classification failures).
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.judge import evaluate_message, local_redirect_text
from modules.agents.models import Agent, AgentJudgeLog, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.agents.typesafe_client import TypeSafeError
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import create_message
from modules.users.crud import create_user

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 950_000_000 + _ID


async def _make_user(session: AsyncSession) -> int:
    user_id = _next_id()
    await create_user(session, user_id=user_id, phone_number=f"+1555{user_id}")
    return user_id


async def _make_private_chat(session: AsyncSession, user_a: int, user_b: int) -> int:
    chat_id = _next_id()
    await create_chat(session, chat_id=chat_id, is_group=False)
    await add_participant_to_chat(session, chat_id=chat_id, user_id=user_a)
    await add_participant_to_chat(session, chat_id=chat_id, user_id=user_b)
    return chat_id


async def _make_agent(
    session: AsyncSession,
    owner_user_id: int,
    owner_agent_chat_id: int,
    *,
    active_skill: str = "support_agent",
    system_prompt: str | None = None,
) -> Agent:
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        is_enabled=True,
        active_skill=active_skill,
        system_prompt=system_prompt,
        triggers=json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)),
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    return agent


async def _send(
    session: AsyncSession,
    chat_id: int,
    sender_id: int,
    content: str = "hello",
    type: int = 1,
):
    message_id = _next_id()
    message = await create_message(
        session, message_id=message_id, chat_id=chat_id, sender_id=sender_id, content=content, type=type
    )
    await session.commit()
    return message


async def _judge_log_rows(session: AsyncSession, agent_id: int) -> list[AgentJudgeLog]:
    stmt = select(AgentJudgeLog).where(AgentJudgeLog.agent_id == agent_id)
    return (await session.execute(stmt)).scalars().all()


def _noul_answers(
    on_topic: float = 1.0,
    prompt_injection: float = 0.0,
    info_extraction: float = 0.0,
    code_execution: float = 0.0,
) -> dict:
    return {
        "on_topic": {"type": "noul", "noul": on_topic},
        "prompt_injection": {"type": "noul", "noul": prompt_injection},
        "info_extraction": {"type": "noul", "noul": info_extraction},
        "code_execution": {"type": "noul", "noul": code_execution},
    }


def _mock_classify(answers: dict | None = None, exc: Exception | None = None):
    """Patches modules.agents.judge.classify (the name judge.py imported into
    its own namespace), never the real typesafe_client function - so no HTTP
    call, no tokens, ever."""
    mock = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=answers)
    return patch("modules.agents.judge.classify", mock)


def _mock_redirect(text: str = "", exc: Exception | None = None):
    """Patches modules.agents.judge.generate_structured - the reject-path-
    only Gemini call that authors redirect_message."""
    result = {"redirect_message": text}
    mock = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=result)
    return patch("modules.agents.judge.generate_structured", mock)


# --- Approval / rejection ----------------------------------------------------

async def test_approved_verdict_skips_redirect_generation(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="what are your hours?")

    with _mock_classify(_noul_answers(on_topic=0.95)) as classify_mock, _mock_redirect() as redirect_mock:
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    classify_mock.assert_awaited_once()
    redirect_mock.assert_not_awaited()
    assert verdict.is_approved is True
    assert verdict.is_malicious is False
    assert verdict.redirect_message == ""

    rows = await _judge_log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].is_approved is True
    assert rows[0].chat_id == target_chat
    assert rows[0].message_id == message.id


async def test_off_topic_rejection_generates_redirect_message(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="what's the weather like on Mars?")

    redirect_text = "That's outside what I can help with here - happy to help with support questions though!"
    with _mock_classify(_noul_answers(on_topic=0.1)), _mock_redirect(redirect_text) as redirect_mock:
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    redirect_mock.assert_awaited_once()
    assert verdict.is_approved is False
    assert verdict.is_malicious is False
    assert verdict.redirect_message == redirect_text

    rows = await _judge_log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].is_approved is False
    assert rows[0].is_malicious is False


@pytest.mark.parametrize(
    "flag,expected_fragment",
    [
        ("prompt_injection", "prompt injection"),
        ("info_extraction", "confidential"),
        ("code_execution", "execute code"),
    ],
)
async def test_malicious_sub_checks_produce_specific_reason_and_escalate(
    db_session, redis_db, flag, expected_fragment
):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="malicious message")

    with _mock_classify(_noul_answers(on_topic=0.9, **{flag: 0.9})), _mock_redirect("sorry, can't help with that"):
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    assert verdict.is_approved is False
    assert verdict.is_malicious is True
    assert expected_fragment in verdict.reason

    rows = await _judge_log_rows(db_session, agent.id)
    assert rows[0].is_malicious is True


async def test_multiple_malicious_flags_join_into_one_reason(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="malicious message")

    with _mock_classify(_noul_answers(on_topic=0.9, prompt_injection=0.9, code_execution=0.9)), _mock_redirect():
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    assert verdict.is_malicious is True
    assert "prompt injection" in verdict.reason
    assert "execute code" in verdict.reason


# --- Fail-open paths ---------------------------------------------------------

async def test_typesafe_error_fails_open(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="hi there")

    with _mock_classify(exc=TypeSafeError("upstream 500")):
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    assert verdict.is_approved is True
    assert "failed open" in verdict.reason

    rows = await _judge_log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].is_approved is True


@pytest.mark.parametrize("malformed", [{}, {"on_topic": {"type": "noul", "noul": 0.9}}])
async def test_malformed_jev_response_fails_open(db_session, redis_db, malformed):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="hi there")

    with _mock_classify(malformed):
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    assert verdict.is_approved is True


async def test_judge_never_raises_even_on_unexpected_exception_type(db_session, redis_db):
    """evaluate_message only catches TypeSafeError/KeyError/TypeError/
    ValueError by contract; this test documents that boundary rather than
    asserting a blanket catch-all - an unexpected exception type is expected
    to propagate, not silently fail open. If this starts failing because the
    catch clause was broadened, that's a deliberate contract change, not a
    regression to just patch over."""
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="hi there")

    with _mock_classify(exc=RuntimeError("totally unexpected")):
        with pytest.raises(RuntimeError):
            await evaluate_message(db_session, agent, target_chat, message.id, message.content)


async def test_redirect_generation_failure_keeps_rejection_falls_back_to_template(db_session, redis_db):
    """jev already decided to reject - a failure in the (Gemini) redirect-
    text call must not flip the verdict back to approved, only leave
    redirect_message empty so the caller falls back to local_redirect_text."""
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="off topic message")

    from modules.agents.gemini_client import GeminiChatError

    with _mock_classify(_noul_answers(on_topic=0.1)), _mock_redirect(exc=GeminiChatError("upstream 500")):
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    assert verdict.is_approved is False
    assert verdict.redirect_message == ""


async def test_rate_limit_exceeded_fails_open_without_calling_jev(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    for _ in range(settings.AGENT_JUDGE_CALLS_PER_MINUTE):
        message = await _send(db_session, target_chat, sender, content=f"msg {_}")
        with _mock_classify(_noul_answers()):
            await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    over_budget_message = await _send(db_session, target_chat, sender, content="one too many")
    with _mock_classify(_noul_answers()) as mock:
        verdict = await evaluate_message(db_session, agent, target_chat, over_budget_message.id, over_budget_message.content)
        await db_session.commit()

    mock.assert_not_awaited()
    assert verdict.is_approved is True
    assert verdict.redirect_message == ""
    assert "rate limit" in verdict.reason

    rows = await _judge_log_rows(db_session, agent.id)
    assert len(rows) == settings.AGENT_JUDGE_CALLS_PER_MINUTE + 1


async def test_empty_content_auto_approves_without_calling_jev_or_rate_limit(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="[media]", type=1)

    with _mock_classify(_noul_answers()) as mock:
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, None)
        await db_session.commit()

    mock.assert_not_awaited()
    assert verdict.is_approved is True
    assert verdict.reason == "no text content to evaluate"

    # Doesn't touch the rate limit budget - burn the whole window with
    # empty-content calls and confirm a real text message right after still
    # gets to call jev.
    for _ in range(settings.AGENT_JUDGE_CALLS_PER_MINUTE):
        empty_msg = await _send(db_session, target_chat, sender, content="[media]")
        with _mock_classify(_noul_answers()):
            await evaluate_message(db_session, agent, target_chat, empty_msg.id, None)
        await db_session.commit()

    real_msg = await _send(db_session, target_chat, sender, content="real question")
    with _mock_classify(_noul_answers()) as mock2:
        verdict2 = await evaluate_message(db_session, agent, target_chat, real_msg.id, real_msg.content)
        await db_session.commit()

    mock2.assert_awaited_once()
    assert verdict2.is_approved is True


# --- is_follow_up ------------------------------------------------------------

async def test_is_follow_up_false_with_no_prior_agent_reply(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)
    message = await _send(db_session, target_chat, sender, content="first message ever")

    with _mock_classify(_noul_answers()):
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    assert verdict.is_follow_up is False


async def test_is_follow_up_true_with_recent_agent_reply(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    await _send(db_session, target_chat, owner, content="how can I help?", type=AGENT_REPLY_MESSAGE_TYPE)
    message = await _send(db_session, target_chat, sender, content="how much does it cost?")

    with _mock_classify(_noul_answers()) as mock:
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    assert verdict.is_follow_up is True
    call_kwargs = mock.await_args.kwargs
    on_topic_instructions = call_kwargs["questions"]["on_topic"]["instructions"]
    assert "active, ongoing conversation" in on_topic_instructions


async def test_is_follow_up_computed_even_when_content_is_empty(db_session, redis_db):
    """is_follow_up reflects real chat state regardless of whether jev is
    ever called - the empty-content short-circuit still needs an accurate
    flag on the logged verdict for tuning purposes."""
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    await _send(db_session, target_chat, owner, content="anything?", type=AGENT_REPLY_MESSAGE_TYPE)
    message = await _send(db_session, target_chat, sender, content="[media]")

    verdict = await evaluate_message(db_session, agent, target_chat, message.id, None)
    await db_session.commit()

    assert verdict.is_follow_up is True


# --- Domain description / question construction ------------------------

async def test_on_topic_instructions_include_persona_and_capped_owner_prompt(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    long_prompt = "x" * (settings.AGENT_JUDGE_SYSTEM_PROMPT_PREVIEW_CHARS + 100)
    agent = await _make_agent(db_session, owner, owner_agent_chat, active_skill="sales_agent", system_prompt=long_prompt)
    message = await _send(db_session, target_chat, sender, content="do you sell shoes?")

    with _mock_classify(_noul_answers()) as mock:
        await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    on_topic_instructions = mock.await_args.kwargs["questions"]["on_topic"]["instructions"]
    # The owner's raw prompt is capped, never injected in full - the whole
    # point of the cap is that an oversized prompt can't become an
    # injection surface against the judge itself.
    assert long_prompt not in on_topic_instructions
    assert "x" * settings.AGENT_JUDGE_SYSTEM_PROMPT_PREVIEW_CHARS in on_topic_instructions


async def test_unknown_active_skill_does_not_crash_domain_description(db_session, redis_db):
    owner = await _make_user(db_session)
    sender = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, sender)
    target_chat = await _make_private_chat(db_session, owner, sender)
    agent = await _make_agent(db_session, owner, owner_agent_chat, active_skill="not_a_real_skill")
    message = await _send(db_session, target_chat, sender, content="hello")

    with _mock_classify(_noul_answers()):
        verdict = await evaluate_message(db_session, agent, target_chat, message.id, message.content)
        await db_session.commit()

    assert verdict.is_approved is True


# --- local_redirect_text -----------------------------------------------------

@pytest.mark.parametrize(
    "active_skill,expected_fragment",
    [
        ("sales_agent", "sales"),
        ("support_agent", "support"),
        ("summarizer", "summarizing this chat"),
        ("one_off_executor", "helping with specific tasks"),
        ("some_unknown_skill", "helping with this"),
    ],
)
def test_local_redirect_text_maps_skill_to_domain_label(active_skill, expected_fragment):
    agent = Agent(id=1, owner_user_id=1, owner_agent_chat_id=1, active_skill=active_skill)
    text = local_redirect_text(agent)
    assert expected_fragment in text
