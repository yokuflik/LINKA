"""Test helper for exercising the real `_run_turn` body (AGENT_RUN_TURN_TEST_PLAN.md).

`_run_turn` never calls Gemini's HTTP API directly - it always goes through
`modules.agents.invoke_turn_helpers.generate_turn` (imported by name from
gemini_client.py). Patching that one reference lets the entire rest of
`_run_turn` run for real (DB session, tool dispatch, token/time budget
accounting, supersede checks, message persistence) with zero network calls
and no API key required.
"""
from unittest.mock import AsyncMock, patch

from modules.agents.gemini_client import TurnResult, TurnUsage


def text_result(text: str, *, finish_reason: str = "STOP", usage: tuple[int, int] = (10, 5)) -> TurnResult:
    prompt_tokens, completion_tokens = usage
    return TurnResult(
        content={"role": "model", "parts": [{"text": text}]},
        finish_reason=finish_reason,
        usage=TurnUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def function_call_result(
    name: str, args: dict, *, finish_reason: str = "STOP", usage: tuple[int, int] = (10, 5)
) -> TurnResult:
    prompt_tokens, completion_tokens = usage
    return TurnResult(
        content={"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]},
        finish_reason=finish_reason,
        usage=TurnUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def mock_gemini_turn(*results_or_exceptions):
    """Patches invoke_turn_helpers.generate_turn with a side_effect list -
    each call `_run_turn`'s loop makes pops the next scripted result/exception.

    Usage: `with mock_gemini_turn(text_result("hi")):`
    """
    return patch(
        "modules.agents.invoke_turn_helpers.generate_turn",
        new=AsyncMock(side_effect=list(results_or_exceptions)),
    )
