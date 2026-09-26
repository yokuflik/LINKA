# Agent shares owner's send_message rate limit — implementation plan

Ref: ADR 0058. Read `.claude_docs/ai_agent.md` and
`.claude_docs/security_and_rate_limiting.md` before touching code (per
CLAUDE.md routing rule).

## Goal

The agent sends messages in-process (`invoke_worker.py` -> `process_outgoing`),
bypassing the Rust `ws_gateway`'s per-user `send_message` sliding-window limiter
entirely. Today it can burst far faster than the owner's own WS client ever could.
Fix: make the agent check + consume the **same** Redis sliding-window keys the
owner's WS traffic uses (`rlsw:send_message:{owner_user_id}`,
`rlsw:send_message_burst:{owner_user_id}`) before each send, and on rejection,
retry with backoff instead of erroring — mirroring `useOutbox.js`'s
clock/pending/requeue behavior on the client, but inside the worker.

## Steps

1. **Mirror the Rust thresholds as Python constants.**
   `config/agent_settings.py` (or wherever the Rust-mirrored env values already
   live — check `.claude_docs/security_and_rate_limiting.md:206-208` for the
   existing `WS_SEND_MESSAGE_RATE_MAX`/`_BURST_MAX` naming): add/confirm
   `WS_SEND_MESSAGE_RATE_MAX` (3), `WS_SEND_MESSAGE_RATE_WINDOW_SECONDS` (1),
   `WS_SEND_MESSAGE_BURST_MAX` (40), `WS_SEND_MESSAGE_BURST_WINDOW_SECONDS` (60).
   Source from the same env vars Rust reads so one value change stays in sync on
   both sides — do not hardcode a second copy of the numbers.

2. **Add a shared-budget check helper.** New function, e.g.
   `modules/agents/tools/common.py::_check_owner_send_rate_limit(agent) -> bool`
   (or a small dedicated module if it grows), calling
   `infra/ratelimit/service.py::check_sliding_window(agent.owner_user_id,
   "send_message", WS_SEND_MESSAGE_RATE_MAX, WS_SEND_MESSAGE_RATE_WINDOW_SECONDS)`
   and the `"send_message_burst"` counterpart. **Key format must byte-match** what
   `crates/common/src/ratelimit.rs` builds (`rlsw:` + action + `:` + identifier) —
   confirmed identical to Python's `_SLIDING_KEY_PREFIX` in
   `infra/ratelimit/service.py`, no changes needed there, just reuse it.

3. **Retry/backoff loop around the send, not a hard error.** In
   `modules/agents/tools/execution.py`'s `_tool_send_message` /
   `_tool_reply_message` (lines ~51-79 and ~105 per prior research), before
   calling `process_outgoing`:
   - Call the check helper from step 2.
   - If over budget: `asyncio.sleep(backoff_ms / 1000)`, then retry — backoff
     starts at `AGENT_SEND_RATE_LIMIT_BACKOFF_MS` (new constant, default 1500,
     `config/agent_settings.py`), doubles each retry, caps at
     `AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS` (default 20000) — same shape as
     `useOutbox.js`'s `RATE_LIMIT_BACKOFF_MS`/cap, kept as independent constants
     (frontend vs backend tunable separately).
   - Stop retrying and fall through to a normal tool-error result (existing
     `ToolDeniedError`-style path) if the turn timeout
     (`AGENT_TURN_TIMEOUT_SECONDS`, 90s) would be exceeded by the next retry.
     Never block the worker indefinitely.

4. **No new "pending" message state or event** (ADR 0058 decision 3,
   recommendation (a)): the message is simply not created via `process_outgoing`
   until the check clears. No new column, no new WS event, no frontend change.
   Skip this step's alternative (b) — visible pending-agent-message marker —
   unless real usage later shows this is confusing.

5. **Extend `get_capacity_status` (ADR 0057, `modules/agents/tools/config_mode.py`)**
   with a new `send_message_quota: {used, max, window_seconds}` block, read-only,
   via `infra/ratelimit/service.py`'s sliding-window peek (may need a
   `peek_sliding_window` counterpart to the existing `peek_fixed_window` if one
   doesn't already exist for sliding windows — check before assuming it needs to
   be added) against `rlsw:send_message:{agent.owner_user_id}`. No consumption.
   Update `BUILDER_PROMPT` phrasing only if it already narrates other
   `get_capacity_status` fields in the finishing summary (ADR 0057 decision 4) —
   otherwise leave prompt wording alone.

6. **Update docs (same task, per CLAUDE.md auto-maintenance rule):**
   - `.claude_docs/ai_agent.md`: note the agent's sends now share the owner's WS
     `send_message` sliding-window budget; link ADR 0058.
   - `.claude_docs/security_and_rate_limiting.md`: note that
     `rlsw:send_message:{user_id}` / `_burst` are now read+written from two call
     sites (Rust gateway + Python agent worker), same keys, same semantics.
   - This root `CLAUDE.md`: no change needed (only touched for core rules/index
     entries, per its own AUTO-MAINTENANCE RULE) — the ADR index row for 0058
     already covers it.

7. **Tests:**
   - Unit test: two rapid agent sends within 1s for the same owner should hit the
     3/1s limiter on the second call and retry (mock `asyncio.sleep` or use a
     short test-only window) rather than raising.
   - Unit test: a manual owner WS send followed immediately by an agent send
     (same owner) should show the agent's check observing the owner's consumed
     slot — i.e. prove the bucket is actually shared, not independently zeroed.
   - Test: exhausting the turn timeout via sustained contention falls through to
     a tool-error result, not an infinite loop/hang.

## Explicitly out of scope

- No change to the activation quota (100/hour) — that's a separate, existing
  concern (gates waking up, not sending).
- No change to `max_messages_per_day`, Gemini call budget, or daily active-seconds
  budget — all independent, already correct per their own purpose.
- No Rust/`ws_gateway` changes — this is additive on the Python side only, reading
  keys Rust already writes.
- No new frontend UI (per step 4).

## Open question for the user before implementation

Should the shared-budget check apply to **every** agent send (including
scheduled/`on_schedule`-triggered turns and config-mode confirmations), or only to
sends that happen while the owner might plausibly be sending manually at the same
time (e.g. skip the check for `on_schedule` fires that happen at 3am when the
owner is certainly not on WS)? Defaulting to "always check, every send path" for
simplicity and correctness (a shared budget should behave the same regardless of
what triggered it) unless told otherwise.
