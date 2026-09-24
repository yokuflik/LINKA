"""Raw-httpx client for Gemini chat + function calling (ADR 0045, step 4) -
no Google SDK dependency, matching the project's existing manual-HTTP style
(modules/vector_search/gemini_client.py, ADR 0042).

Model: gemini-flash-latest. Reuses the same GEMINI_API_KEY/GEMINI_API_BASE/
GEMINI_HTTP_TIMEOUT_SECONDS settings as modules/vector_search (one Gemini
API key for the whole project, two different endpoints).

Per-agent rate limit (5 calls/minute, ADR 0045) is enforced by the caller
(invoke_worker.py) via infra.ratelimit.service, not in here - this module is
a thin, stateless transport.
"""
import logging
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

GEMINI_CHAT_MODEL = "gemini-flash-latest"


class GeminiChatError(Exception):
    """Raised on any HTTP/shape failure talking to the Gemini generateContent
    API. Callers treat this as "the turn failed" - never surfaced to the
    chat, just logged and the turn abandoned."""


def _require_api_key(api_key: Optional[str]) -> str:
    key = api_key or settings.GEMINI_API_KEY
    if not key:
        raise GeminiChatError("GEMINI_API_KEY is not configured")
    return key


def _to_gemini_value(value: Any) -> Any:
    """Function-response values must be JSON-plain; ids in this codebase are
    ints that can exceed 2^53 (Snowflake), so every id-shaped value already
    comes out of modules/agents/tools.py as a str - this is just a safety net
    for anything that slipped through as a raw int."""
    if isinstance(value, dict):
        return {k: _to_gemini_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_gemini_value(v) for v in value]
    return value


async def generate_turn(
    *,
    system_prompt: str,
    contents: list[dict],
    tool_schemas: list[dict],
    api_key: Optional[str] = None,
) -> dict:
    """One generateContent call. `contents` is the running conversation in
    Gemini's {role, parts} shape (role is "user" or "model" only - a
    functionResponse part is still sent with role "user", not "function").
    Returns the raw `candidates[0].content` dict - the caller inspects
    `parts` for a `functionCall` vs. a plain `text` part.

    `api_key` overrides the shared settings.GEMINI_API_KEY - used for BYOK
    owners (ADR 0046 decision 5); omit to use the shared key.
    """
    api_key = _require_api_key(api_key)
    url = f"{settings.GEMINI_API_BASE}/v1beta/models/{GEMINI_CHAT_MODEL}:generateContent"
    body: dict = {
        "contents": contents,
        "tools": [{"functionDeclarations": tool_schemas}],
    }
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    try:
        async with httpx.AsyncClient(timeout=settings.GEMINI_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, params={"key": api_key}, json=body)
        if resp.status_code == 429:
            raise GeminiChatError("Gemini rate limit / quota exceeded (429)")
        if resp.status_code >= 400:
            logger.warning("Gemini generateContent %s body: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            # Most commonly a prompt/safety block - no candidate at all.
            reason = data.get("promptFeedback", {}).get("blockReason", "no candidates")
            raise GeminiChatError(f"Gemini returned no candidates: {reason}")
        return candidates[0]["content"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Gemini generateContent failed: %s", exc)
        raise GeminiChatError(str(exc)) from exc


def extract_function_call(content: dict) -> Optional[dict]:
    """Returns {"name", "args"} for the first functionCall part, or None if
    this turn's response is plain text (conversation over)."""
    for part in content.get("parts", []):
        call = part.get("functionCall")
        if call:
            return {"name": call["name"], "args": call.get("args", {})}
    return None


def extract_text(content: dict) -> str:
    return "".join(part.get("text", "") for part in content.get("parts", []))


def function_response_part(name: str, response: dict) -> dict:
    return {"functionResponse": {"name": name, "response": _to_gemini_value(response)}}
