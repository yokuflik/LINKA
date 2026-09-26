# AI Agent - capacity self-awareness, shared send budget, token windows

Split out of `.claude_docs/ai_agent.md` on 2026-09-26 (file exceeded the
~300-line CLAUDE.md threshold again). Covers ADR 0057 (`get_capacity_status`
tool), ADR 0058 (agent shares the owner's WS send-message budget), and ADR
0059 (rolling token-usage windows + output cap). See `ai_agent.md` for the
full rate-limit table these all feed into.

## Capacity/rate-limit self-awareness tool (ADR 0057, 2026-09-26)

New Builder-state-only config tool `get_capacity_status`
(`modules/agents/tools/config_mode.py::_tool_get_capacity_status`): read-only
introspection of every rate limit relevant to the agent (activation quota,
Gemini calls/min, daily active-seconds budget, per-sender unknown-sender
daily quota, `restrictions.max_messages_per_day`, knowledge base
documents/chunks, schedule entries, `on_specific_chats` auto-registered
count) alongside current usage read live from Redis/Postgres - never
increments or enforces anything. New `infra/ratelimit/service.py::
peek_fixed_window(identifier, action)` (bare Redis `GET`, no `INCR`) is the
one new rate-limiter primitive this needed, since `agent_activation` and
`agent_gemini_calls` are plain `check_and_increment` fixed-window counters
(key `ratelimit:{action}:{id}`), not sliding windows - existing
`check_and_increment`/`check_sliding_window`/`enforce_sliding_window` all
record a hit, which a pure status-check tool must never do. Knowledge
counts reuse `crud.py::count_knowledge_documents`/`count_knowledge_chunks`
directly; schedule-entry count is `len(agent.triggers.get("on_schedule",
[]))` in-memory (no persisted counter exists for it, same as
`_check_schedule_quota`).

Also returns a `capacity_estimate.approx_new_conversations_per_hour` -
`min(activation_quota.max, gemini_calls.max * (activation_window /
gemini_window), daily_active_seconds.max / AGENT_ESTIMATED_SECONDS_PER_TURN)
- activation_quota.used`, using new `AGENT_ESTIMATED_SECONDS_PER_TURN`
(`config/agent_settings.py`, default 8s) - a rough single-turn cost
estimate used only for this projection, never in real enforcement. Labelled
as approximate in both the tool's own response (`disclaimer` field) and its
Gemini schema description, since real turns vary with tool use (e.g.
Agentic RAG round-trips).

Wired only into `BUILDER_STATE_TOOL_SCHEMAS[BuilderState.BUILDER]` (which
already spreads `CONFIG_TOOL_SCHEMAS` wholesale) - not Supervisor, not Help,
not execution mode; no `dispatch.py` change needed, same mechanism
`resolve_user` used when it was added. `BUILDER_PROMPT` now requires calling
it once near the end of the interview, before `finish_building_agent`, and
folding its estimate into the final summary message as an approximation,
never a guarantee. `HELP_PROMPT` confirmed not to need a matching update
(too generic to describe individual tool mechanics) per the standing
obligation logged in `ai_agent_changelog.md`. No schema/architecture change
beyond the one new config setting, no new tests (same gap as every prior
agents-module step) - import-smoke-tested only.

## Agent shares the owner's WS send_message budget (ADR 0058, 2026-09-26)

The agent sends messages in-process (`invoke_worker.py` -> `process_outgoing`,
stamped `sender_id=agent.owner_user_id`) - it never touches the Rust
`ws_gateway`, so it used to bypass that gateway's per-user `send_message`
sliding-window limiter entirely (`rlsw:send_message:{user_id}` 3/1s +
`rlsw:send_message_burst:{user_id}` 40/60s). Since every agent send *is* the
owner as far as any recipient can tell, it now draws from the exact same
Redis buckets rather than an unlimited or separate one.

New `_consume_owner_send_budget(agent)` in
`modules/agents/tools/common.py`, called from both `_tool_send_message` and
`_tool_reply_message` in `execution.py` (right after `_check_daily_send_quota`,
before `process_outgoing`): calls `infra.ratelimit.service.check_sliding_window`
against `agent.owner_user_id` for both `"send_message"` and
`"send_message_burst"` - byte-identical key format to what
`crates/common/src/ratelimit.rs` writes, so this reads/writes the owner's real
WS bucket, not a copy. On rejection it sleeps and retries with exponential
backoff (`AGENT_SEND_RATE_LIMIT_BACKOFF_MS` default 1500ms, doubling, capped at
`AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS` default 20000ms - both new in
`config/agent_settings.py`, independently tunable from the frontend's
`useOutbox.js` equivalents) rather than failing the tool call - it has no
deadline of its own; the outer per-turn `asyncio.wait_for(AGENT_TURN_TIMEOUT_
SECONDS)` in `invoke_worker.py` is what eventually cuts it off if the owner's
own WS traffic is simultaneously saturating the same bucket. No new "pending"
message state - the send is simply deferred until the check clears, no new
UI/event surface.

New `infra/ratelimit/service.py::peek_sliding_window(identifier, action,
window_seconds)` (bare `ZREMRANGEBYSCORE` + `ZCARD`, no `ZADD`) is the sliding-
window counterpart to ADR 0057's `peek_fixed_window`, used by
`get_capacity_status`'s new `send_message_quota` block (read-only, against
`agent.owner_user_id`'s bucket) so the Builder can tell the owner this budget
is shared with their own manual messages. `WS_SEND_MESSAGE_RATE_MAX`/
`_WINDOW_SECONDS`/`WS_SEND_MESSAGE_BURST_MAX`/`_WINDOW_SECONDS` already existed
in `config/security_settings.py` as the Rust-mirrored env values - no new
config needed there, just reused.

Only the activation quota (100/hour, gates waking up) was ever separate; this
closes the one gap where the agent's *sending* throughput had no real ceiling
matching the owner's own. Tests: `tests/infra/ratelimit/
test_rate_limit_sliding_window.py` (`peek_sliding_window`) +
`tests/modules/agents/tools/test_owner_send_budget.py` (shared-bucket
enforcement, independent-owner isolation, backoff shape) - the first real unit
tests in the agents module tree.

## Token usage windows (ADR 0059, 2026-09-26)

Two independent rolling budgets per agent, tracking combined input+output
tokens (industry-standard "usage window" shape, distinct from every other
limit in this file which counts calls or wall-clock seconds): **5 hours /
500,000 tokens** and **7 days / 3,000,000 tokens**. New module
`modules/agents/token_budget.py` - two Redis fixed-window `INCRBY` counters
(`ratelimit:agent_tokens_5h:{agent_id}` / `ratelimit:agent_tokens_7d:{agent_id}`),
same shape as `time_budget.py`'s daily active-seconds counter (chosen for
the same reason: `infra.ratelimit.service.check_and_increment` always adds
exactly 1 and can't be reused for a weighted counter). A **fixed** window,
not a sliding log, so "resets in" is just the key's Redis `TTL` - exact and
cheap, with the same known fixed-window burst trade-off every other
`check_and_increment`-based limit in this codebase already accepts.

`gemini_client.py::generate_turn` now returns a `TurnResult`
(`content`/`finish_reason`/`usage`) instead of a bare `content` dict -
`usage` is Gemini's `usageMetadata` (`prompt_tokens`/`completion_tokens`/
`total_tokens`), `finish_reason` is `candidates[0].finishReason`. Neither
existed anywhere in this codebase before this ADR. It also gained a
`max_output_tokens` param forwarded as `generationConfig.maxOutputTokens` -
also never set anywhere before this ADR.

In `invoke_worker.py::_run_turn`'s round-trip loop (shared-key calls only -
BYOK draws from the owner's own Gemini quota, same carve-out as the
`agent_gemini_calls` check): before each call, estimates the call's own
input cost with a `len(text) // 4` char-per-token heuristic
(`token_budget.estimate_tokens`) over `system_prompt + json.dumps(contents)
+ json.dumps(tool_schemas)`; if the tighter of the two windows' remaining
budget is under `max(AGENT_TOKEN_MIN_VIABLE_BUDGET=2000, estimated_input)`,
the call is skipped entirely (never spent - a call whose own input already
exceeds what's left is certain to fail/truncate to nothing) and the turn
ends. Otherwise `max_output_tokens = min(remaining - estimated_input,
AGENT_MAX_OUTPUT_TOKENS_CEILING=8192)` is passed into `generate_turn`. After
every call, `result.usage.total_tokens` is recorded into both windows
(`token_budget.record_tokens`) regardless of how the turn ends.

If `result.finish_reason == "MAX_TOKENS"`, the turn stops immediately - no
further round-trips, checked before `extract_function_call` is even
consulted. **Visibility rule (explicit user requirement):** the truncated
text is never forwarded to a real chat counterpart in execution mode - only
the owner sees it, and only when the cutoff happens inside their own
config-mode chat (posted via the existing `_post_config_reply`, same as any
other config-mode plain-text turn-end). An execution-mode cutoff against a
third party discards the partial text outright; that chat sees nothing.
Either way, a fixed English notice (`_TOKEN_BUDGET_EXHAUSTED_NOTICE`) is
posted into the owner's own `owner_agent_chat_id` - gated by a `SET NX`
cooldown key (`agent_token_budget_notice_sent:{5h|7d}:{agent_id}`, TTL = the
window length), the exact pattern `trigger_engine._notify_activation_quota_
exceeded` already uses, so a burst of blocked turns produces exactly one
notice per window per exhaustion, not one per turn.

`GET /agents/me/usage` (new, read-only, no side effect -
`token_budget.peek_usage`) returns `{window_5h, window_7d}`, each
`{used, limit, percent, resets_in_seconds, is_blocked}`. Frontend: new
`poc/components/UsageProgressBar.js` (two linear bars with a live "Resets
in HH:MM:SS" countdown) shown above the agent chat view; `useAgentConfig.js`
polls the endpoint every 30s while the drawer is open (`startAgentUsagePolling`/
`stopAgentUsagePolling`, started in `openAgentDrawer`, stopped in
`closeAgentDrawer`/`resetAgentConfig` - first "poll a REST endpoint on an
interval" precedent in `poc/`, no existing WS event covers this). When
either window is blocked (`agentUsageBlocked` computed), `AgentChatView.js`
disables the textarea, the `+` attachment button, and the send button, with
an inline note - UX convenience only, real enforcement is the server-side
gate above (same convention as every other limit in this file).

No backward compatibility - applies from deploy time forward, all agents
start at zero usage in both windows. Additive to, not a replacement for,
every existing call-count/wall-clock limit in `ai_agent.md`'s rate-limit
table. The LLM Judge (`generate_structured`) is deliberately **not** metered
against these windows - same reasoning ADR 0053 already used to keep it off
`agent_gemini_calls`.

**Bug fix (2026-09-26, user-reported):** the exhaustion notice originally
only fired inside the `finish_reason == "MAX_TOKENS"` branch - i.e. only
when the *triggering* call itself got truncated. A call that pushed
`used >= limit` but still finished with a normal `STOP` (the common case)
left the owner with zero notice, discovered when a window ran out entirely
inside a turn against a third-party chat. Fixed by moving the
`peek_usage`/`_notify_token_budget_exhausted` check to run unconditionally
right after every `record_tokens` call in `invoke_worker.py::_run_turn`
(not just the `MAX_TOKENS` branch, which now just reuses the same
already-fired notice via the existing per-window `SET NX` cooldown key -
no double-send). Notice still always lands in the owner's own
`owner_agent_chat_id` regardless of which chat the turn was serving.
