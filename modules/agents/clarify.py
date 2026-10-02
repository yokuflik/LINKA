"""BuilderState.CLARIFY question generation (ADR 0093 Phase 1).

Same minimal-call pattern as judge.py::_generate_redirect_message (ADR
0076): a cheap, tool-free, history-free Gemini call, using its own model
tier (AGENT_CLARIFY_MODEL) rather than the main turn model, that authors a
single, short, same-language disambiguating question. Never wired into the
normal per-round-trip config-mode turn loop (invoke_worker.py special-cases
BuilderState.CLARIFY before that loop even starts) - unlike every other
builder_state, clarify never sees chat history or a tool schema.

Failure here always falls back to a fixed English question
(LOCAL_CLARIFY_QUESTION) - there is no "reject the turn" concept the way
judge.py has; the owner must always get *some* disambiguating question.
"""
import logging

from config import settings
from modules.agents.gemini_client import GeminiChatError, generate_structured
from modules.agents.models import Agent

logger = logging.getLogger(__name__)

_CLARIFY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}},
    "required": ["question"],
}

LOCAL_CLARIFY_QUESTION = (
    "Quick check - do you want me to do this just this once, or should I set it up to keep "
    "happening automatically going forward?"
)

_SYSTEM_PROMPT = (
    "You are this user's own AI agent, replying to them directly in their private chat with "
    "you. Their last message below could reasonably mean either of two things: (1) a one-off "
    "action they want done right now or at a specific future time, or (2) a standing behavior "
    "they want you to keep doing going forward (a persistent setup/configuration change). "
    "Write ONE short, natural question, in the SAME language as their message, that asks "
    "plainly which of the two they mean - nothing else. No greeting, no explanation, no "
    "preamble, no options list - just the single disambiguating question, the way a person "
    "would ask it in a quick chat message."
)


async def generate_clarify_question(agent: Agent, message_content: str) -> str:
    """Returns the disambiguating question text to post to the owner, or
    LOCAL_CLARIFY_QUESTION on any generation failure. Called from
    invoke_worker.py's config-mode special case for BuilderState.CLARIFY,
    never from the normal generate_turn tool-calling path."""
    if not message_content:
        return LOCAL_CLARIFY_QUESTION
    try:
        result = await generate_structured(
            model=settings.AGENT_CLARIFY_MODEL,
            system_prompt=_SYSTEM_PROMPT,
            user_text=message_content,
            response_schema=_CLARIFY_RESPONSE_SCHEMA,
        )
        question = str(result.get("question", "")).strip()
        return question or LOCAL_CLARIFY_QUESTION
    except (GeminiChatError, KeyError, TypeError) as exc:
        logger.warning("agent_clarify: question generation failed, falling back to template: %s", exc)
        return LOCAL_CLARIFY_QUESTION
