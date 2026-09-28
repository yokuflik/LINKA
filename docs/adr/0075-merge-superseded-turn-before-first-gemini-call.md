# 0075 - Merge a superseded turn into one reply instead of discarding it

Status: Accepted

## Context

ADR 00732 stops a stale turn's reply from reaching the chat once a newer
message supersedes it, but it always discards the stale turn outright and
waits for the *next* debounce-fired turn to answer both messages together.
In practice this only works cleanly when the newer message arrives *before*
the debounce window elapses (ADR 0063's coalescing already merges it there).

Observed bug (2026-09-27): a customer sent "בא לי לקנות מחשב לתואר סטודנט",
then, while the agent's turn for that message was already running (past the
debounce window, lock acquired, history already read into `contents`), sent
"לא משנה בעצם עזוב". The reply that arrived answered only the *first*
message in full, as if the second had never been sent. It was not resent
after via a rushed second reply either - one reply, stale content, no error.

Root cause: `mark_superseded`/`is_superseded` (ADR 00732) only gate *whether a
reply is delivered*, not *what history a turn reasons over*. The in-flight
turn's `contents` were already built from `_build_initial_contents` (one
history snapshot, read once before the loop) at the moment the second
message landed. Two races both lead to a single one-sided reply:

1. The second message's trigger match calls `arm_debounce` (the plain,
   not-yet-contending path in `trigger_engine.py`) while the first turn has
   not yet reached `acquire_turn_lock` - no supersede flag is ever set, the
   ZSET score is bumped, but if the first turn's dequeue/lock/history-read
   already happened by the time the poll loop would have re-fired, the
   second message's own arm never produces a distinct turn: `due_pairs`
   already popped the pair for turn 1, and turn 1's `contents` snapshot
   predates message 2 in the DB.
2. Even when `mark_superseded` *does* fire (lock genuinely held), the
   in-flight turn is discarded wholesale with no attempt to hand its
   in-progress reasoning to the replacement turn - the replacement always
   restarts cold from `_build_initial_contents`, and only fires after a
   fresh `AGENT_INVOKE_DEBOUNCE_SECONDS` (2s) wait counted from the moment
   `mark_superseded`/`arm_debounce` ran, during which the peer-visible
   `typing` indicator (tied to the now-cancelled turn's own task) has
   nothing keeping it alive.

The desired behavior (confirmed with the user): when a second message
arrives while a turn for the same `(agent_id, chat_id)` is still working,
the agent must answer **both messages in one reply**, never two replies and
never a reply that silently ignores the newer message. The `typing`
indicator must stay visible continuously through the whole window, not
flicker off between the discarded turn and the replacement.

## Decision

Split the supersede response into two cases, keyed on whether the in-flight
turn has made its first Gemini call yet:

**Case A - pre-first-call (round_trip == 0, no `generateContent` sent yet):**
Do not discard the turn. Re-run `_build_initial_contents` to get a fresh
history snapshot (now including the newer message) and replace `contents`
in place, then continue the *same* turn/lock/typing-loop with the merged
history. This is the common case for the observed bug: the debounce window
already delayed the first call long enough that a fast second message lands
before any Gemini round-trip has actually started, so merging is free - no
wasted call, no extra latency, no second reply.

**Case B - mid-turn (a Gemini call already in flight or completed at least
one round-trip):** Keep ADR 00732's existing behavior (cancel the in-flight
call via `_generate_turn_or_supersede`, end the turn, no reply) since
merging into a live tool-calling loop is not safe (functionCall/
thoughtSignature state is mid-sequence). But instead of waiting for the next
regular debounce fire (`AGENT_INVOKE_DEBOUNCE_SECONDS`, 2s), the code that
marks a turn superseded now enqueues the replacement turn's due time
immediately (score = now, not now + debounce) - the merge already happened
implicitly (the replacement's `_build_initial_contents` reads history fresh,
which includes the newer message), so there is nothing left to gain by
waiting out the debounce again.

**Typing indicator:** ownership of the peer-visible `typing` loop moves out
of `_run_turn` and up to the trigger-match / re-arm call sites
(`trigger_engine._evaluate_triggers`, and the `mark_superseded` call site in
`invoke_worker.process_entry`) via a small Redis marker
(`agent_typing_active:{agent_id}:{chat_id}`, TTL a few seconds, refreshed by
whichever turn currently owns the loop) - so a new match against a chat that
already has a live indicator does not need to start a second one, and a turn
that gets superseded before delivering anything does not tear the indicator
down and force a fresh grace-period gap before the replacement turn's own
loop spins up. The loop's lifetime is now scoped to "there is unanswered
agent activity for this chat", not "this specific turn object is still
alive".

## Guardrails

- No change to ADR 0063's coalescing before the first turn is even
  dequeued - this ADR only changes what happens once a turn has already
  started running.
- No change to ADR 0053 (Judge) or ADR 0074 (malicious-intent escalation) -
  the merge only touches the plain history-snapshot path
  (`_build_initial_contents`), and the Judge still evaluates the triggering
  message_id as it does today (whichever message_id was live when the
  Judge ran); a message that lands after the Judge already approved the
  turn is not re-judged mid-turn - accepted as-is, same posture as ADR 00732
  ("no merging of a superseded turn's partial work").
- Case A never merges more than once per turn iteration - if a *third*
  message lands while still at round_trip == 0, the same merge logic simply
  re-reads history again (idempotent - it always re-reads whatever is
  currently in the DB, not the previous merge's snapshot).
- Case B's immediate re-fire still goes through the normal
  `acquire_turn_lock`/`_run_turn` path - no bypass of the turn mutex itself,
  only the debounce wait is skipped.
- Typing-loop ownership marker is fire-and-forget, same posture as every
  other Redis call in this module (ADR 0045) - a failure to set/refresh it
  degrades to today's per-turn loop lifetime, never blocks message
  delivery.

## What this deliberately does not do

- **No merge for case B.** Still no attempt to splice a superseded turn's
  partial tool-call results into the replacement turn, consistent with
  ADR 00732.
- **No new owner-facing notice.** From the customer's side this should read
  as one continuous "typing…" followed by one reply that addresses
  everything they said, exactly the target behavior - no visible sign a
  merge or supersede happened at all.
