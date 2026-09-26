# 0058 - Agent shares the owner's WS send_message rate limit, with a queued/pending retry instead of a hard error

Status: Accepted
Date: 2026-09-26

## Context

The AI agent (ADR 0045+) sends messages by calling `message_service.process_outgoing`
directly in-process from `invoke_worker.py`, stamped with `sender_id =
agent.owner_user_id` (`modules/agents/tools/execution.py:73,105` ->
`modules/messaging/send.py:39`). This path never touches the Rust `ws_gateway`, so
it never hits the per-user `send_message` sliding-window limiter that a real WS
client is subject to (`rlsw:send_message:{user_id}` 3/1s +
`rlsw:send_message_burst:{user_id}` 40/60s, `crates/ws_gateway/src/ws.rs:399-430`,
`.claude_docs/security_and_rate_limiting.md:122-125`).

Net effect: the agent can burst sends far faster than the owner ever could over
their own WS connection - bounded only by the unrelated activation quota (100/hour,
gates *waking up*, not sending) and the optional, unlimited-by-default
`max_messages_per_day`. Since every agent-sent message is attributed to the owner's
`user_id` (it *is* the owner, as far as any recipient or downstream consumer can
tell), it should consume the same per-user send budget the owner's own manual
messages consume - one identity, one send budget - rather than a separate or
nonexistent one.

The desired behavior mirrors what a real client already does when *it* gets
rate-limited: `poc/composables/useOutbox.js` never surfaces a hard failure - it
requeues the message with a clock/pending indicator and exponential backoff
(`RATE_LIMIT_BACKOFF_MS=1500` -> cap `20000`) until the gateway's
`{"type":"error","code":"rate_limited"}` frame stops arriving or the 90s ack timeout
elapses. The agent's own send-message tool should behave the same way at the
worker level: check the *same* Redis keys the owner's WS traffic writes to, and if
over budget, wait/retry rather than erroring the tool call back to Gemini.

## Decision

1. **The agent's send-message tool checks the owner's existing WS `send_message`
   sliding-window keys before sending**, using the identical key format Rust already
   writes: `check_sliding_window(agent.owner_user_id, "send_message", ...)` and
   `check_sliding_window(agent.owner_user_id, "send_message_burst", ...)` via
   `infra/ratelimit/service.py` (`_SLIDING_KEY_PREFIX = "rlsw:"`, byte-identical to
   `crates/common/src/ratelimit.rs`'s `SLIDING_PREFIX` + action + identifier - both
   already confirmed compatible, no format change needed). Keyed on
   `agent.owner_user_id`, **not** a separate agent identity, so the owner's manual
   WS sends and the agent's in-process sends draw from the *same* bucket.
   - This is a **read-and-conditionally-consume** check, mirroring what the Rust
     gateway does inline - not a new independent limiter.
   - The 3/1s and 40/60s thresholds must be mirrored as Python constants (today
     they only exist in Rust's `AppState.config.limits`); add
     `WS_SEND_MESSAGE_RATE_MAX`/`_WINDOW_SECONDS` and
     `WS_SEND_MESSAGE_BURST_MAX`/`_WINDOW_SECONDS` to
     `config/security_settings.py` (or wherever the Rust-mirrored env values
     already live per `.claude_docs/security_and_rate_limiting.md:206-208`), sourced
     from the same env vars so a single value change stays in sync on both sides.

2. **On over-budget, the agent queues and retries with backoff - it never errors
   the send back to Gemini as a tool failure.** New helper in
   `modules/agents/tools/execution.py` (or a new small
   `modules/agents/send_queue.py` if the retry loop needs to outlive a single tool
   call - see decision 3): on a sliding-window rejection, sleep and re-check using
   the same backoff shape as `useOutbox.js`
   (`AGENT_SEND_RATE_LIMIT_BACKOFF_MS` default 1500, doubling, cap
   `AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS` default 20000 - new constants in
   `config/agent_settings.py`, named after but independent from the frontend's
   values so either can be tuned separately), up to the turn timeout
   (`AGENT_TURN_TIMEOUT_SECONDS`, 90s) or a small fixed retry cap, whichever is
   hit first.

3. **The message is persisted immediately in a "pending" state visible to the
   owner as the same clock/pending indicator a real queued client message shows -
   not silently delayed with no UI trace.** This requires the message to exist
   client-side (or be creatable) before the rate check clears, so:
   - Reuse the existing `Message` row + a lightweight pending marker rather than
     inventing a new table. Candidates to evaluate at implementation time: (a) an
     ordinary `process_outgoing` call is deferred until the limiter clears (message
     doesn't exist yet, so nothing to mark - the owner simply doesn't see it until
     it lands, indistinguishable from normal turn latency), vs (b) persist the
     message immediately with a `pending_rate_limit: true` transient flag/event so
     the owner's open chat can render the same clock icon `useOutbox.js` uses, then
     flip it to sent once the retry clears.
   - **Recommendation: option (a) - defer creation, no new pending state.** The
     agent's send is not happening over the owner's own WS connection, so there is
     no local optimistic bubble to reconcile against; inventing a
     visible-pending-agent-message concept is new UI/event surface for a case that,
     given decision 4's ceiling, should resolve within single-digit seconds. Revisit
     only if real usage shows owners confused by the delay.

4. **A bounded ceiling still applies - this is a shared budget, not a promise the
   agent will eventually get through.** If retries exhaust the turn timeout without
   the limiter clearing (only plausible if the owner's own WS traffic is
   simultaneously saturating the same bucket), the tool call fails back to Gemini
   as a normal tool error (existing `ToolDeniedError`-style path,
   `invoke_worker.py`'s round-trip budget), and the agent's turn ends gracefully -
   it does not hang the worker indefinitely.

5. **`get_capacity_status` (ADR 0057) is extended** to surface this shared budget:
   add a `send_message_quota: {used, max, window_seconds}` block reading
   `peek_fixed_window`/sliding-window peek on `rlsw:send_message:{owner_user_id}`
   (read-only, no consumption), so the Builder can tell the owner "this shares your
   own per-second send limit with your manual messages" - relevant capacity-planning
   context this ADR's feature otherwise has no way to surface.

## Consequences

- Fixes the flood gap identified 2026-09-26: the agent can no longer send
  materially faster than the owner's own client could, because it draws from the
  identical Redis-backed bucket.
- No Rust changes required - the gateway's existing sliding-window keys are read
  as-is; this is purely additive on the Python side.
- Couples the agent's send throughput to the owner's own concurrent WS activity: if
  the owner is themselves mid-burst sending manually, the agent's sends queue
  behind them in the same budget. This is the explicitly intended behavior (one
  identity, one budget), not a bug.
- Adds latency variance to agent replies under contention (new: retry/backoff loop
  bounded by the 90s turn timeout) where today there is none - acceptable given the
  alternative is unbounded flood potential.
- No new persisted "pending" message state (per decision 3's recommendation) -
  slightly less UI transparency than a real client's queued-message clock icon, but
  avoids new event/schema surface for a delay expected to be sub-second to
  low-single-digit-seconds in the common case.
- `.claude_docs/ai_agent.md` and `.claude_docs/security_and_rate_limiting.md` need a
  cross-reference update once implemented (new shared-bucket behavior, new
  `AGENT_SEND_RATE_LIMIT_BACKOFF_MS`/`_MAX_MS` constants) - per the routing rule,
  before/alongside the code.
