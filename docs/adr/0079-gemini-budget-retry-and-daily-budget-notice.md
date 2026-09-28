# 0079 - In-place retry for a mid-turn Gemini call budget stall + a notice for daily budget exhaustion

Status: Accepted

## Context

User-reported: three messages sent back-to-back in the owner's own agent chat
got no response at all - no reply, no error, no notice anywhere, not even the
drawer's `agent_thinking` "error" flip. Investigation found two genuine silent
dead-ends in `modules/agents/invoke_worker.py`, both pre-existing and distinct
from the supersede-chain behavior (ADR 00732/0075/0077), which is by design
silent for its own in-scope case (a stale turn correctly yielding to a newer
one) but was not the actual cause here:

1. **`_run_turn`'s per-minute Gemini call budget check**
   (`AGENT_GEMINI_CALLS_PER_MINUTE`, a `check_and_increment` fixed window).
   On exhaustion this ended the turn with a bare `return` - no notice, no
   retry, nothing. Unlike every *other* exhaustion path in this file
   (`GeminiChatError`, `MAX_TOKENS`, the round-trip cap, the outer
   `process_entry` timeout), which all post an owner-facing notice per the
   frontend error UX rule ("never leave a turn silently dead with no
   notice"), this one path was missed. Worse, since this is a fixed *window*
   that resets on its own within a minute, immediately giving up is the
   wrong response in the first place - the budget will very likely clear on
   its own within seconds.

2. **`process_entry`'s daily active-time budget check**
   (`time_budget.has_budget_remaining`). This is checked *before* `_run_turn`
   is ever called, so it's not just missing a notice inside `_run_turn` - the
   turn never starts, `_publish_agent_thinking` is never reached, and there
   is no owner-facing signal of any kind. Unlike the per-minute Gemini budget
   above, this window is ADR 0045's 1-hour/day processing-time budget and
   does not reset again soon - retrying in place makes no sense here, but a
   notice does.

## Decision

**1. In-place retry for the per-minute Gemini call budget.** When
`_check_gemini_call_budget` fails inside `_run_turn`'s round-trip loop, retry
up to `AGENT_GEMINI_BUDGET_MAX_RETRIES` (default 6) times, sleeping
`AGENT_GEMINI_BUDGET_RETRY_SECONDS` (default 5) between attempts, instead of
ending the turn immediately. Both new settings, `config/agent_settings.py`.

- The retry sub-loop re-checks `is_superseded` on every wake-up (ADR 00732) -
  a newer message superseding this turn while it slept is handled by falling
  through to the normal top-of-loop supersede branch, not by continuing to
  retry a now-stale turn.
- The sub-loop does **not** consume a `round_trip` slot from
  `AGENT_TURN_MAX_TOOL_ROUNDTRIPS` - that budget counts actual
  functionCall round-trips, unrelated to a rate-limit backoff. A `while`-style
  retry inside the same iteration is used rather than `continue`-ing the
  outer `for round_trip in range(...)` loop, which would silently advance it.
- If all retries are exhausted, the turn now falls through to the existing
  `_post_config_reply` pattern (same one `GeminiChatError`/the round-trip cap
  already use) with a short "handling a lot of requests" notice into the
  owner's own agent chat, then ends - matching every other exhaustion path's
  UX instead of being the one silent exception.
- Both retries and the sleeps stay well inside `AGENT_TURN_TIMEOUT_SECONDS`
  (90s default) and the turn lock's TTL (same value) even at the max retry
  count (6 x 5s = 30s), so no interaction with `process_entry`'s outer
  `asyncio.wait_for`/turn-lock TTL.

**2. Fixing a latent case-A/case-B misclassification introduced by the
retry.** ADR 0075's case A vs. case B branch (top of the round-trip loop)
previously used `round_trip == 0` as a proxy for "no Gemini call has been
made yet for this turn." The new retry sub-loop's supersede-while-waiting
path re-enters that branch via `continue`, which advances `round_trip` -
meaning a turn superseded while still waiting on its *first* Gemini call
(never actually made) could have been misread as case B (a call already
happened) purely because `round_trip` was no longer 0. Replaced with an
explicit `gemini_call_made` flag, set immediately before the one place
`_generate_turn_or_supersede` is actually invoked, and checked in place of
`round_trip == 0` in the case A/B branch. No other behavior change to ADR
0075's case A/B logic itself.

**3. One-time notice for daily active-time budget exhaustion.** New
`modules/agents/time_budget.seconds_until_reset(agent_id)`: reads the
counter key's own Redis TTL rather than computing a reset time, since this
is a rolling-from-first-use 24h window (TTL set once, on the first
increment of the day, per the existing `record_active_seconds`), not a
calendar-day reset - there is no fixed reset time to compute from.

New `invoke_worker._notify_daily_budget_exhausted`, called from
`process_entry` right before its existing silent `return` on
`has_budget_remaining() == False`. Same `SET NX` cooldown pattern as ADR
0059's `_notify_token_budget_exhausted` (one notice per exhaustion event,
not one per dropped trigger), cooldown TTL = the remaining seconds until
reset (floored at 60s to avoid a zero/negative TTL right at the boundary).
Notice text states an approximate wait ("up to N hours") computed from that
same remaining-seconds value, into the owner's own agent chat.

No retry here - unlike the per-minute Gemini budget, this window does not
clear again soon, so silently retrying would just repeat the same failure
for up to a day.

## Guardrails

- Both new settings (`AGENT_GEMINI_BUDGET_RETRY_SECONDS`,
  `AGENT_GEMINI_BUDGET_MAX_RETRIES`) are env-overridable, following every
  other tunable in `config/agent_settings.py`.
- The Gemini-budget retry path is fire-and-forget with respect to the wider
  system exactly like every other branch in this file - a Redis failure
  inside the retry's `is_superseded`/`_check_gemini_call_budget` calls is
  already handled by those functions' own try/except (unchanged here).
- `seconds_until_reset` is read-only (`TTL`, no side effect), safe to call
  from the notice path without affecting the counter itself.
- The daily-budget notice reuses the exact `send_system_message` +
  `session.commit()` pattern already established by
  `_notify_token_budget_exhausted`, so no new posting mechanism was
  introduced.

## What this deliberately does not do

- **No retry for the daily active-time budget.** A 1-hour/day budget
  exhausting mid-conversation is not something that clears within a
  reasonable in-turn retry window - only a notice makes sense.
- **No change to the supersede-chain's own silence** (ADR 00732/0075/0077) -
  a turn correctly yielding to a genuinely newer message still ends with no
  owner-facing notice, by design; that was never the bug here. This ADR only
  closes the two paths that ended silently for reasons *unrelated* to a
  newer message ever having arrived.
- **No attempt to make the per-minute retry adaptive** (e.g. reading the
  fixed window's actual remaining TTL to sleep exactly that long) - a flat
  5s x 3 back-off was judged simple enough and short enough relative to the
  60s window.
