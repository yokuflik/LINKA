"""BuilderState.CLARIFY question generation (ADR 0093 Phase 1) -
modules/agents/clarify.py.

Same isolation pattern as test_judge.py's redirect-text tests:
modules.agents.clarify.generate_structured is monkeypatched directly (the
name clarify.py imported into its own namespace) - no real HTTP call, no
Gemini API key needed. generate_clarify_question is currently unreachable
from a real turn (nothing transitions builder_state to "clarify" until
Phase 3's router lands) - these are pure unit tests against the function
itself.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modules.agents.clarify import LOCAL_CLARIFY_QUESTION, generate_clarify_question
from modules.agents.gemini_client import GeminiChatError

pytestmark = pytest.mark.asyncio


def _agent() -> SimpleNamespace:
    return SimpleNamespace(id=1, owner_user_id=42)


def _mock_generate(question: str = "", exc: Exception | None = None):
    result = {"question": question}
    mock = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=result)
    return patch("modules.agents.clarify.generate_structured", mock)


async def test_returns_the_generated_question_on_success():
    with _mock_generate(question="Do you want this once, or should it keep happening?"):
        result = await generate_clarify_question(_agent(), "remind me every day at 9am")

    assert result == "Do you want this once, or should it keep happening?"


async def test_falls_back_to_local_question_on_gemini_chat_error():
    with _mock_generate(exc=GeminiChatError("boom")):
        result = await generate_clarify_question(_agent(), "remind me tomorrow at noon")

    assert result == LOCAL_CLARIFY_QUESTION


async def test_falls_back_to_local_question_on_empty_generated_text():
    with _mock_generate(question=""):
        result = await generate_clarify_question(_agent(), "send this to the group")

    assert result == LOCAL_CLARIFY_QUESTION


async def test_falls_back_to_local_question_on_missing_key_in_response():
    mock = AsyncMock(return_value={})
    with patch("modules.agents.clarify.generate_structured", mock):
        result = await generate_clarify_question(_agent(), "tell everyone the sale starts today")

    assert result == LOCAL_CLARIFY_QUESTION


async def test_returns_local_question_immediately_for_empty_message_without_calling_gemini():
    mock = AsyncMock(return_value={"question": "should never be reached"})
    with patch("modules.agents.clarify.generate_structured", mock):
        result = await generate_clarify_question(_agent(), "")

    assert result == LOCAL_CLARIFY_QUESTION
    mock.assert_not_called()


async def test_uses_the_dedicated_clarify_model_not_the_main_turn_model():
    from config import settings
    from modules.agents.gemini_client import GEMINI_CHAT_MODEL

    captured = {}

    async def _fake_generate(*, model, system_prompt, user_text, response_schema):
        captured["model"] = model
        return {"question": "one or many?"}

    with patch("modules.agents.clarify.generate_structured", AsyncMock(side_effect=_fake_generate)):
        await generate_clarify_question(_agent(), "do this now")

    assert captured["model"] == settings.AGENT_CLARIFY_MODEL
    assert captured["model"] != GEMINI_CHAT_MODEL
