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
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

GEMINI_CHAT_MODEL = "gemini-flash-latest"


@dataclass
class TurnUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class TurnResult:
    """generate_turn's return shape (ADR 0059) - was a bare `content` dict
    before this ADR; now also carries usageMetadata (for the token-budget
    counters, modules/agents/token_budget.py) and finishReason (so the caller
    can detect a MAX_TOKENS cutoff and stop the turn immediately)."""

    content: dict
    finish_reason: Optional[str]
    usage: Optional[TurnUsage]


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
    max_output_tokens: Optional[int] = None,
) -> TurnResult:
    """One generateContent call. `contents` is the running conversation in
    Gemini's {role, parts} shape (role is "user" or "model" only - a
    functionResponse part is still sent with role "user", not "function").
    Returns a TurnResult: `.content` is the raw `candidates[0].content` dict
    (the caller inspects `parts` for a `functionCall` vs. a plain `text`
    part), `.finish_reason` is Gemini's `candidates[0].finishReason` (e.g.
    "STOP", "MAX_TOKENS"), `.usage` is the parsed `usageMetadata` (ADR 0059 -
    fed into modules/agents/token_budget.py's counters).

    `api_key` overrides the shared settings.GEMINI_API_KEY - used for BYOK
    owners (ADR 0046 decision 5); omit to use the shared key.

    `max_output_tokens` caps generationConfig.maxOutputTokens (ADR 0059) so a
    single completion can't exceed the caller's remaining token budget; omit
    to leave Gemini's own default cap in place.
    """
    api_key = _require_api_key(api_key)
    url = f"{settings.GEMINI_API_BASE}/v1beta/models/{GEMINI_CHAT_MODEL}:generateContent"
    body: dict = {
        "contents": contents,
        "tools": [{"functionDeclarations": tool_schemas}],
    }
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    if max_output_tokens is not None:
        body["generationConfig"] = {"maxOutputTokens": max_output_tokens}

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
        usage_raw = data.get("usageMetadata")
        usage = None
        if usage_raw:
            usage = TurnUsage(
                prompt_tokens=int(usage_raw.get("promptTokenCount", 0)),
                completion_tokens=int(usage_raw.get("candidatesTokenCount", 0)),
                total_tokens=int(usage_raw.get("totalTokenCount", 0)),
            )
        return TurnResult(
            content=candidates[0]["content"],
            finish_reason=candidates[0].get("finishReason"),
            usage=usage,
        )
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


async def generate_structured(
    *,
    model: str,
    system_prompt: str,
    user_text: str,
    response_schema: dict,
) -> dict:
    """One generateContent call for a single, isolated, tool-free
    classification response (ADR 0053's LLM Judge). Deliberately a separate
    function from generate_turn rather than a mode flag on it: no `contents`
    history, no `tools`/function-calling, structured output only via
    `responseMimeType: application/json` + `responseSchema`. Always uses the
    shared settings.GEMINI_API_KEY (the judge never runs under BYOK - it's a
    platform-level cost/safety gate, not a per-owner turn).

    Returns the parsed JSON object. Raises GeminiChatError on any HTTP/shape
    failure, exactly like generate_turn - callers decide their own failure
    posture (the judge gate fails open, per the ADR).
    """
    api_key = _require_api_key(None)
    url = f"{settings.GEMINI_API_BASE}/v1beta/models/{model}:generateContent"
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    try:
        async with httpx.AsyncClient(timeout=settings.GEMINI_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, params={"key": api_key}, json=body)
        if resp.status_code == 429:
            raise GeminiChatError("Gemini rate limit / quota exceeded (429)")
        if resp.status_code >= 400:
            logger.warning("Gemini structured generateContent %s body: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "no candidates")
            raise GeminiChatError(f"Gemini returned no candidates: {reason}")
        content = candidates[0]["content"]
        text = extract_text(content)
        if not text:
            raise GeminiChatError("Gemini structured response had no text part")
        return json.loads(text)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Gemini structured generateContent failed: %s", exc)
        raise GeminiChatError(str(exc)) from exc
