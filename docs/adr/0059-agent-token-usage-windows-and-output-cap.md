# 0059 - Agent token usage windows (5h/7d) with a hard pre-flight gate and output cap

Status: Accepted

## Context

Every existing agent limit (`AGENT_GEMINI_CALLS_PER_MINUTE`, `AGENT_ACTIVATION_QUOTA_PER_HOUR`,
`AGENT_DAILY_ACTIVE_SECONDS_BUDGET`, ADR 0045/0047) counts *calls* or
*wall-clock seconds*, never *tokens*. An agent with a short conversation
history makes cheap calls; an agent with a long history/knowledge-base
context (Agentic RAG, `get_knowledge_index`/`fetch_chunk`, ADR 0047 decision
5) can burn far more tokens per call while staying under every existing
limit. The user wants an industry-standard usage model (Claude/ChatGPT-style):
a short rolling window (5 hours) and a long rolling window (7 days), each
tracking combined input+output tokens, surfaced to the owner as a progress
bar with an exact reset time, and backed by a real server-side hard stop -
not just a UI nicety.

Two extra requirements from the user, beyond the display:

1. Once an agent is close to (or over) its budget, the *next* Gemini call's
   `maxOutputTokens` must be capped to whatever's left, so a single huge
   completion can't blow past the budget in one call. If the model still gets
   cut off mid-answer (`finishReason == "MAX_TOKENS"`), the turn stops
   immediately - no further round-trips.
2. Cost/latency: don't call Gemini at all when the remaining budget is too
   small to plausibly complete anything useful - estimate the turn's input
   cost up front (`len(text) // 4` char-per-token heuristic, applied to
   system prompt + contents + serialized tool schemas) and hard-block before
   spending an API call if remaining budget is under a fixed floor.

No backward compatibility is required - this replaces nothing, applies
`from now on`, and existing agents simply start at zero usage in both windows
the first time this ships.

## Decision

**1. Two independent fixed-window token counters per agent**, following the
exact precedent of `modules/agents/time_budget.py` (a hand-rolled Redis
`INCRBY` + `EXPIRE`-on-first-hit counter, chosen there specifically because
`infra.ratelimit.service.check_and_increment` always adds exactly 1 and
can't be reused for a weighted counter) - new module
`modules/agents/token_budget.py`:

- `ratelimit:agent_tokens_5h:{agent_id}` - TTL 5h (18000s), cap
  `AGENT_TOKEN_BUDGET_5H` = **500,000** tokens.
- `ratelimit:agent_tokens_7d:{agent_id}` - TTL 7d (604800s), cap
  `AGENT_TOKEN_BUDGET_7D` = **3,000,000** tokens.

A **fixed** window (not a sliding log) is a deliberate simplification: the
"Resets in" time is just the key's remaining TTL (`TTL` command), which is
exact and cheap - a real sliding window would need the token-weighted Lua
variant described as a gap in `infra/ratelimit/service.py` (member weight
isn't summable via the existing `ZCARD`-based script). The trade-off (a
fixed window can burst up to 2x at the boundary, same known trade-off
`check_and_increment` already accepts elsewhere in this codebase) is
acceptable for a usage-display feature, not a security boundary.

**2. Pre-flight low-budget gate** - before spending any Gemini call, estimate
this turn's input tokens with a `len(text) // 4` heuristic over
`system_prompt + serialized contents + serialized tool_schemas`. If the
*remaining* budget in either window is below `AGENT_TOKEN_MIN_VIABLE_BUDGET`
= **2,000** tokens, skip Gemini entirely - a turn's fixed overhead (system
prompt + tool schemas + at least some transcript) realistically starts
around 1,500-2,500 input tokens alone per the codebase's own transcript cap
(`AGENT_HISTORY_TRANSCRIPT_MAX_CHARS` = 4000 chars ~ 1000 tokens, plus system
prompt + ~10 tool schemas), so anything under 2,000 remaining is not enough
to do anything useful and would likely fail or truncate to nothing anyway.

**3. Output cap threaded into the Gemini request** - `generate_turn` gains a
`max_output_tokens: Optional[int]` parameter, forwarded as
`generationConfig.maxOutputTokens` in the REST body (a field Gemini already
supports; never set anywhere in this codebase before this ADR). The worker
computes it per-call as `min(remaining_budget_across_both_windows,
AGENT_MAX_OUTPUT_TOKENS_CEILING)` (ceiling = 8192, a sane technical cap
independent of the user's budget - never ask Gemini for an absurdly large
completion just because the 7-day window happens to be nearly full).

**4. Mid-response cutoff enforcement** - `generate_turn` now also returns
`finish_reason` and `usage` (`{prompt_tokens, completion_tokens,
total_tokens}` from the response's `usageMetadata`) alongside the existing
`content`. After every call, the worker records `total_tokens` into both
counters (`token_budget.record_tokens`). If `finish_reason == "MAX_TOKENS"`,
the turn stops immediately - no further round-trips, regardless of whether
the (necessarily truncated) response contained a function call or plain
text. This is a turn-ending condition checked before `extract_function_call`
is even consulted for routing.

**5. Visibility rule - partial text never reaches the real counterpart.**
When a turn ends via the `MAX_TOKENS` cutoff:
- **Execution-mode turns** (any chat except the owner's own agent chat, or a
  schedule-fired turn): the truncated text is **discarded**, never sent via
  `send_message`/`reply_message` and never posted to that chat directly. The
  other party sees nothing extra - not a half-sentence, not an error. Only
  the owner is told, via the fixed notice below posted into their own
  `owner_agent_chat_id`.
- **Config-mode turns** (the owner's own chat, Supervisor/Builder/Help): the
  partial text *is* posted via the existing `_post_config_reply` path -
  it's the owner's own conversation with their own agent, so showing them
  what the model got out before being cut off is useful, not a leak.

This is a deliberate asymmetry: the owner is a trusted operator who benefits
from seeing partial output and diagnostic detail; a third-party chat
counterpart is never shown internal capacity/billing mechanics or a
broken-looking half-reply, matching the project's existing convention of
never leaking backend/internal state into a stranger-facing chat (id-masking
precedent, `.claude_docs/ai_agent.md`'s "internal ids hard-masked" entry).

**6. Fixed English notice, once per exhaustion, owner-only.** A single fixed
string (`_TOKEN_BUDGET_EXHAUSTED_NOTICE`), sent via
`send_system_message(session, agent.owner_agent_chat_id, ...)` exactly once
per exhaustion event, gated by a `SET NX` cooldown key
(`agent_token_budget_notice_sent:{5h|7d}:{agent_id}`, TTL = that window's
remaining time) - the exact pattern already used by
`trigger_engine._notify_activation_quota_exceeded`. Fires both when the
pre-flight gate blocks a turn before calling Gemini, and when a call comes
back with `finish_reason == "MAX_TOKENS"`. Text:

> "Your agent has used up its token budget for this time window and will
> pause responding until it resets. It'll pick back up automatically."

**7. `GET /agents/me/usage` endpoint** - returns, per window (`5h`, `7d`):
`used`, `limit`, `percent`, `resets_in_seconds` (from Redis `TTL`, `-1`/`-2`
mapped to the full window length / 0), and `is_blocked` (used >= limit).
Read-only, no side effect (`token_budget.peek_usage`, mirroring
`peek_fixed_window`'s read-only `GET`).

**8. Frontend: `UsageProgressBar.js`** - two linear progress bars (5h, 7d) in
the agent drawer, each with a percentage and a live "Resets in HH:MM:SS"
countdown (client-side ticker off the fetched `resets_in_seconds`, no
server polling more than once every ~30s). When either window's
`is_blocked` is true, the chat input textbox, the `+` attachment button, and
the send button in `AgentChatView.js` are disabled (`:disabled`, greyed out,
`cursor-not-allowed`) with a short inline note ("You've hit your usage limit
- try again in ...") - this is a UX convenience only; the real enforcement
is server-side (steps 2/4 above), matching this project's standing
frontend-is-not-the-boundary convention for every other limit.

## Consequences

- New Redis keys, no schema/migration. No backward-compatibility shim - all
  agents start at zero usage in both windows from deploy time forward.
- `generate_turn`'s return shape changes (was a bare `content` dict, now a
  small wrapper carrying `content`/`finish_reason`/`usage`) - its one caller
  (`invoke_worker.py::_run_turn`) is updated in the same change.
- This is additive to, not a replacement for, `AGENT_GEMINI_CALLS_PER_MINUTE`
  / `AGENT_ACTIVATION_QUOTA_PER_HOUR` / `AGENT_DAILY_ACTIVE_SECONDS_BUDGET` -
  an agent can still be blocked by any of those first; this is one more
  layer, the only one that looks at token volume instead of call count or
  wall-clock time.
- The LLM Judge (ADR 0053, `generate_structured`) is a separate, much
  cheaper call path with its own model and own rate bucket
  (`agent_judge_calls`) - deliberately **not** metered against these token
  windows, same reasoning ADR 0053 already used to keep it off
  `agent_gemini_calls`: judge traffic must never starve real turns' budget.
