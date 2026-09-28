# 0076: TypeSafe `jev` as the Judge gate's classification backend

Status: Accepted

## Context

ADR 0053 built the LLM Judge gate on a single Gemini `generateContent` call
(`gemini-flash-lite-latest`, `generate_structured`) returning a 4-field JSON
verdict (`is_approved`, `reason`, `redirect_message`, `is_malicious` - the
last added by ADR 0074). The judge runs on every execution-mode,
message-fired turn, so its per-call cost/latency multiplies across the
platform's whole message volume - a pure classification decision is being
made by a general-purpose generative model.

TypeSafe (`jev-latest`, `api.typesafe.ai/v1/systemone`) is a dedicated
classification model: given a `state` (free text) and a set of typed
`questions` (Noul/Choice/Score), it returns strict structured answers with
confidence/probabilities, no free-text generation. It is faster and cheaper
than a generative model for exactly the yes/no decisions the judge needs,
and its docs recommend splitting a compound judgment into atomic per-
dimension questions in one call rather than one compound question - which
also gives a real, specific rejection reason for free (which sub-check
fired) instead of a model-authored sentence.

The one thing jev cannot do is author free text: the judge's
`redirect_message` (a polite, same-language reply shown to the rejected
customer) requires generation, not classification.

## Decision

Split the judge gate into two backends:

1. **Classification (jev, `modules/agents/typesafe_client.py`, new)** - one
   `systemone` call per judged message with four atomic Noul questions in a
   single request:
   - `on_topic` - plausibly relates to the agent's configured domain, or is a
     legitimate short follow-up in an active conversation (same permissive
     rules ADR 0053 already established, now expressed as jev `instructions`
     built per-call from `_domain_description` + the existing
     `_is_follow_up_in_active_conversation` flag, replacing Gemini's
     `systemInstruction`).
   - `prompt_injection` - attempts to override/reveal/bypass the agent's own
     instructions.
   - `info_extraction` - asks for confidential/internal business information
     (pricing internals, credentials, vendor details, anything framed as
     secret).
   - `code_execution` - asks the agent to run code, shell, or system-level
     commands.

   Derived: `is_malicious = prompt_injection OR info_extraction OR
   code_execution`; `is_approved = on_topic AND NOT is_malicious`. A
   deterministic, specific `reason` string is built from whichever flags
   fired (e.g. "prompt injection attempt", "requested confidential business
   information", joined with "; " if more than one fired) - replacing the
   free-text `reason` Gemini used to author, and now readable straight off
   the classification instead of trusting model-written prose. This reason
   feeds both `AgentJudgeLog.reason` and, on `is_malicious`, the owner-facing
   `escalate_chat` notice (ADR 0074) - so the owner now sees which specific
   attack class was detected, not a paraphrase.

2. **Redirect-text generation (Gemini, reject path only)** - when
   `is_approved` is false, one minimal `generate_structured`-style Gemini
   call (renamed setting `AGENT_JUDGE_REDIRECT_MODEL`, same default
   `gemini-flash-lite-latest`, same shared `GEMINI_API_KEY`) with no tools,
   no history, no chat context beyond: the rejected message's text, the
   deterministic reason from step 1, and the short domain summary. Produces
   only `{redirect_message}` - the same shape of tiny, cheap call the judge
   already made for every message; now it only fires on the (small) rejected
   fraction of traffic. On failure, falls back to the existing
   `local_redirect_text(agent)` template - this is a wording fallback only,
   never a security fallback: jev has already made the approve/reject/
   malicious decision by this point and that decision is not revisited.

Fail-open stays exactly as ADR 0053 defined it, but now scoped to step 1
only: a `typesafe_client` call failure, malformed response, or the judge's
own rate-limit being exceeded resolves to `is_approved=True` (never calls
Gemini for redirect text, since there is nothing to reject). A step-2
(redirect wording) failure does NOT flip the verdict - it only changes which
template produces the customer-facing text.

`AGENT_JUDGE_CALLS_PER_MINUTE`/`_WINDOW_SECONDS` (the existing
`agent_judge_calls:{agent_id}` bucket) is consumed once per judged message
by the jev call, unchanged. No second rate bucket is added for the redirect-
text call - it is inherently bounded by how often messages are actually
rejected, which is a small fraction of what the judge bucket already caps.

`AgentJudgeLog` schema is unchanged (`is_approved`, `reason`,
`is_follow_up_flag`, `is_malicious`) - `reason` now holds the deterministic
string described above instead of free model text.

## Consequences

- Classification cost/latency drops (jev vs. a full Gemini call) for every
  judged message; Gemini is only spent on the minority that get rejected,
  and that call is now much smaller (no domain/security-rules system prompt,
  just message + reason + short domain label).
- Owners get a real, specific escalation reason instead of a model
  paraphrase - improves trust and auditability of `AgentJudgeLog`.
- New external dependency: `JEV_API_KEY` (already provisioned in `.env`) and
  `api.typesafe.ai` availability become part of the judge's critical path;
  covered by the same fail-open posture as Gemini was.
- `modules/agents/judge.py`'s public contract (`evaluate_message`,
  `JudgeVerdict`, `local_redirect_text`) is unchanged - `invoke_worker.py`
  and `tools/common.py::escalate_chat` need no changes beyond what already
  reads `verdict.reason`/`verdict.is_malicious`.
