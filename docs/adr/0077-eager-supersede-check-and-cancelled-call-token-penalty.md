# 0077 - Eager supersede check in the Trigger Rule Engine + a fixed token penalty for a cancelled mid-call turn

Status: Accepted

## Context

ADR 0063/0073/0075 already guarantee that a stale turn's reply never reaches
a chat once a newer message supersedes it, and that only one turn per
`(agent_id, chat_id)` ever runs at a time. But the *detection* of "a turn is
already running for this pair" only happens on the consumer side
(`invoke_worker.process_entry`'s `acquire_turn_lock` call), which only runs
once a debounce-ZSET member becomes due. `trigger_engine._evaluate_triggers`
itself never checks turn state - it unconditionally calls `arm_debounce` for
every matching message.

This means a second message arriving while a turn is already mid-Gemini-call
is not detected until the *next* debounce window elapses and the poll loop
tries to fire it again - up to `AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS`
(1s) + `AGENT_INVOKE_DEBOUNCE_SECONDS` (2s) after the second message was
actually sent, roughly 3 seconds in the worst case. Only then does
`mark_superseded` get called, and only then does the in-flight turn's own
polling (`_generate_turn_or_supersede`, every 0.5s) or per-round-trip check
notice it. The user-visible risk in that window: if the second message is
itself a cancellation/correction of the first ("wait, never mind" / "לא
משנה בעצם עזוב"), the stale turn may already be past the last supersede
checkpoint and deliver an answer to the *original* (now-withdrawn) request
before the flag is ever set.

Separately: when a turn *is* caught mid-Gemini-call (`_TurnSuperseded`
raised inside `_generate_turn_or_supersede`, `invoke_worker.py`), the call to
Gemini is cancelled client-side (`call_task.cancel()`), but no
`TurnResult`/`usageMetadata` is ever returned, so `record_tokens` is never
called for that call. Real input+output tokens were plausibly spent on
Google's side regardless (no real cancellation exists, per ADR 00732's
Context), but today's token-usage windows (ADR 0059) show zero cost for it -
an undercount, not an overcount.

## Decision

**1. Eager supersede check in `trigger_engine._evaluate_triggers`.** Before
calling `arm_debounce` for a matched trigger, check whether a turn is
already running for this `(agent_id, chat_id)` pair via a plain, read-only
`EXISTS`-style check against the existing turn-lock key
(`agent_turn_lock:{agent_id}:{chat_id}`, ADR 0063) - no new Redis structure,
no `SET NX` (only the worker acquires/releases the lock itself; the trigger
engine only ever reads it). If the lock is held:

- Call `mark_superseded(agent_id, chat_id)` immediately (same flag ADR 00732
  already defines), so the in-flight turn's next checkpoint (round-trip top,
  the 0.5s mid-call poll, or the pre-send gate) catches it right away instead
  of waiting for the next debounce cycle to rediscover the conflict.
- Call `arm_debounce_now(agent_id, chat_id)` (ADR 0075's immediate-refire
  variant, score = now) instead of the normal `arm_debounce` - there is
  nothing to gain from a fresh debounce wait once it's already known a turn
  is running and has just been marked superseded.
- Still update the stashed message_id exactly as `arm_debounce` normally
  does (reuses the same helper's message_id side-write), so the replacement
  turn's `_build_initial_contents` (or, for a still-pre-first-call turn,
  ADR 0075's case-A merge) reflects the newest message.

If the lock is not held (the common case - no turn running), behavior is
completely unchanged: plain `arm_debounce`, same as today.

This does not change the turn mutex, the coalescing window for the
not-yet-started case, or either of ADR 0075's case A/case B branches - it
only moves the *detection* of "a turn is already running" earlier, from the
consumer's next dequeue attempt to the moment the very next message is
persisted. Worst-case detection latency drops from
`AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS + AGENT_INVOKE_DEBOUNCE_SECONDS`
(~3s) to a single Redis round-trip (effectively immediate), bounded further
by whichever in-flight checkpoint (round-trip top, 0.5s mid-call poll, or
pre-send gate) the running turn hits next.

**2. Fixed token penalty for a mid-call cancellation.** When
`_generate_turn_or_supersede` raises `_TurnSuperseded` (the call was
cancelled while genuinely in flight to Gemini, not merely between
round-trips), `invoke_worker.py`'s `except _TurnSuperseded:` block now calls
`record_tokens(agent_id, AGENT_SUPERSEDED_CALL_TOKEN_PENALTY)` before
re-arming and returning. New setting, `AGENT_SUPERSEDED_CALL_TOKEN_PENALTY`
(default 500) - small relative to a normal round-trip's typical usage and a
rounding error against the 5h/1,000,000 and 7d/5,000,000 windows (ADR 0059),
but non-zero: real input+output tokens were plausibly spent on a call this
codebase chose to stop waiting for, and the owner's usage display should not
silently show that as free. This is a flat estimate, not a measurement - no
attempt is made to learn or approximate the real cost of the specific
cancelled call.

This penalty applies **only** to the mid-call cancellation path
(`_TurnSuperseded`). It does not apply to a turn caught superseded between
round-trips or right before `send_message`/`reply_message` - those paths
already have a real `TurnResult.usage` from a completed call, and
`record_tokens` already runs for them via the normal flow before the
supersede check is reached.

## Guardrails

- The eager check in `trigger_engine.py` is a single Redis `GET`
  (existence-only) per matched trigger - negligible added cost on the
  already-Redis-heavy hot path (ADR 0046 decision 1's pre-filter cache),
  and it is skipped entirely for the non-matching common case (the check
  only runs after a trigger has already matched, same as `arm_debounce`
  today).
- Fire-and-forget posture unchanged: a failure to read the lock, set the
  supersede flag, or arm the immediate refire must never block message
  delivery - logged and swallowed, same as every other Redis call in this
  module.
- No change to `acquire_turn_lock`/`release_turn_lock` semantics themselves
  - the trigger engine never acquires or releases the lock, only reads it.
- `AGENT_SUPERSEDED_CALL_TOKEN_PENALTY` is a flat, agent-independent
  constant (not scaled by model, prompt size, or persona) - simplest
  possible accounting for an inherently unknowable real cost.
- No change to case A of ADR 0075 (pre-first-call merge) - the eager check
  can also fire while a turn is at `round_trip == 0` before its first
  `generateContent` call; in that case the turn's own top-of-loop check
  still finds `is_superseded` true and merges in place exactly as today,
  no penalty charged (no call was ever cancelled).

## What this deliberately does not do

- **No real Gemini request cancellation.** Same posture as ADR 00732/0075 -
  the in-flight HTTP call is not aborted at the network level, and no
  token/cost savings are claimed.
- **No per-call measured penalty.** The penalty is a fixed estimate, not a
  reconciliation against Gemini's actual billed usage for the cancelled
  call (no such data is available after cancellation).
- **No change to the debounce/coalescing window** for the case where no
  turn is yet running - only the already-running case is affected.
