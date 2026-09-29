"""Raw-httpx client for the TypeSafe `jev` classification model (ADR 0076).

Used only by the LLM Judge gate (modules/agents/judge.py) for the
approve/reject/malicious classification - a pure structured-answer
classifier, not a generative model. Mirrors gemini_client.py's manual-HTTP
style: no vendor SDK, one stateless async function per call shape.

Per-agent rate limiting is enforced by the caller (judge.py) via
infra.ratelimit.service against the existing agent_judge_calls bucket - this
module is a thin, stateless transport, same posture as gemini_client.py.
"""
import json
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

# Module-level shared client (connection pooling) instead of one short-lived
# AsyncClient per call - under concurrent judge/attachment-judge calls the
# per-call connect overhead was contributing to httpx timeouts (which
# stringify to "" with no message, making failures look blank in logs).
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.JEV_HTTP_TIMEOUT_SECONDS)
    return _client


class TypeSafeError(Exception):
    """Raised on any HTTP/shape failure talking to the TypeSafe API. Callers
    treat this exactly like GeminiChatError - the judge gate fails open on
    it, never surfaced to the chat."""


def _require_api_key() -> str:
    key = settings.JEV_API_KEY
    if not key:
        raise TypeSafeError("JEV_API_KEY is not configured")
    return key


async def classify(*, state: str, questions: dict[str, dict]) -> dict[str, dict]:
    """One `systemone` call evaluating `questions` against `state` in a
    single request (TypeSafe's own recommendation: atomic per-dimension
    questions in one call rather than one compound question or several
    round-trips). Each question is `{"type": "noul"|"choice"|"score",
    "instructions": str, ...}` per the TypeSafe API reference.

    Returns the raw `answers` dict, keyed the same as `questions`. Raises
    TypeSafeError on any HTTP/shape failure - callers decide their own
    failure posture (the judge gate fails open, per ADR 0053/0076).
    """
    api_key = _require_api_key()
    body = {
        "state": state,
        "model": settings.JEV_MODEL,
        "questions": questions,
    }

    logger.info("TypeSafe systemone request questions: %s", list(questions.keys()))

    try:
        resp = await _get_client().post(
            _ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        if resp.status_code == 429:
            raise TypeSafeError("TypeSafe rate limit / quota exceeded (429)")
        if resp.status_code >= 400:
            logger.warning("TypeSafe systemone %s body: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        answers = data["answers"]
        if not isinstance(answers, dict):
            raise TypeSafeError("TypeSafe response 'answers' was not an object")
        return answers
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
        # httpx timeout/connect exceptions often carry no message (str(exc)
        # == "") - include the exception type so a blank message is still
        # diagnosable (e.g. ReadTimeout vs ConnectTimeout vs PoolTimeout).
        logger.warning("TypeSafe systemone call failed: %s: %s", type(exc).__name__, exc)
        raise TypeSafeError(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__) from exc


def noul_value(answers: dict[str, dict], key: str) -> float:
    """Extracts a Noul question's [0, 1] score from a classify() result.
    Raises KeyError/TypeError on a missing/malformed answer - callers treat
    that as a shape failure, same as any other malformed judge response."""
    return float(answers[key]["noul"])
