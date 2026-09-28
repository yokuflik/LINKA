# 00732 - Supersede an in-flight agent turn on a new message

Status: Accepted

## Context

ADR 0063 added a debounce/coalescing window (default 2s) before a turn
starts, plus a per-`(agent_id, chat_id)` turn mutex so two turns for the same
pair never run concurrently. But the mutex's fallback path only covers the
*queuing* side: if a new message matches a trigger while a previous turn for
that pair is **already running** (up to `AGENT_TURN_TIMEOUT_SECONDS`, 90s),
`process_entry` just calls `arm_debounce` again and returns - the in-flight
turn is left completely alone. It keeps reading the stale transcript it
started with, keeps making Gemini/tool round-trips, and - critically - is
still free to call `send_message`/`reply_message` and actually post its
reply to the chat before the mutex ever releases.

The result is exactly the UX problem this ADR exists to fix: a customer (or
the owner) sends a message while the agent is "still thinking" about an
earlier one, and gets two separate replies - one answering a thought that's
already been superseded, followed by a second one for the real, current
message. ADR 0063's own guardrails section explicitly chose not to
"cancel/preempt the in-flight turn"; this ADR revisits that choice for this
one case, because letting a stale turn's answer reach the chat is worse than
skipping it.

Prior research (see chat/task context) confirmed the Gemini API documents no
supported cancellation mechanism for an in-flight `generateContent`/
`streamGenerateContent` call, and unofficial reports suggest the backend may
keep generating (and billing) after the client disconnects. **This ADR does
not attempt real request cancellation and claims no token/cost savings.** It
only stops a stale turn's output from ever reaching a real chat.

## Decision

A new Redis flag, `agent_turn_superseded:{agent_id}:{chat_id}`, set the
moment a new message matches a trigger while `acquire_turn_lock` reports the
pair already locked (same call site in `process_entry` that today only calls
`arm_debounce`). The in-flight turn's own HTTP call to Gemini is **not**
interrupted - it runs to completion or timeout exactly as today - but
`_run_turn` checks the flag at two points and, if set, ends the turn without
delivering anything:

1. **At the top of every round-trip loop iteration** (alongside the existing
   Gemini-call-budget/MAX_TOKENS/round-trip-cap checks) - the cheapest, most
   common case: catches a stale turn between tool calls before it does any
   more work.
2. **Immediately before dispatching `send_message`/`reply_message`** (the
   last possible point before something actually leaves the process) - closes
   the race where a turn's function-call round-trip was already in flight
   over HTTP when the flag was set, so it only gets caught on return, not
   before the call was made.

Either check finding the flag set: skip `execute_tool_call`/
`_post_config_reply` entirely, do **not** re-arm the debounce (the message
that set the flag already did that via the existing `arm_debounce` call),
and let the turn's normal `finally` release the turn lock - the very next
debounce poll tick then starts a fresh turn seeded by `_build_initial_
contents`, which re-reads history and sees every message from the superseded
turn plus the new one that superseded it.

The flag is read with an atomic get-and-delete (`GETDEL`, matching the
existing `redis` client's async surface) so it can never leak into a later,
unrelated turn for the same pair.

Tool calls other than `send_message`/`reply_message` that a superseded turn
already made before being caught (e.g. a `read_history` or `resolve_user`
round-trip) are simply wasted - there is no attempt to hand their results to
the turn that replaces it. Accepted as-is: these tools have no
externally-visible side effect, so the only cost is a discarded Gemini
round-trip, not a user-visible inconsistency.

## Guardrails

- Flag TTL = `AGENT_TURN_TIMEOUT_SECONDS`, same self-healing bound as the
  turn lock itself (ADR 0063) - a flag nobody ever reads (e.g. the in-flight
  turn crashes before checking it) cannot outlive the turn it was meant for.
- Fire-and-forget: a failure to set/read the flag must never block message
  delivery - logged and swallowed, same posture as every other Redis call in
  this module (ADR 0045).
- No change to the debounce/coalescing window or the turn mutex's existing
  behavior for the normal (not-yet-started) case - this ADR only changes
  what happens to a turn that is already running when superseded.
- Config-mode turns (the owner's own agent chat) go through the same path as
  execution-mode, no special-case carve-out - consistent with ADR 0063.
- Schedule-fired/ephemeral-task turns (`chat_id=None` or a scoped system
  prompt) are unaffected in practice: `chat_id=None` never contends with
  anything (same reasoning as the turn mutex), and a real chat_id ephemeral
  task behaves like any other execution-mode turn.

## What this deliberately does not do

- **No real Gemini request cancellation.** The in-flight HTTP call is never
  aborted; this ADR only gates what happens with its result. No token/cost
  savings are claimed or expected.
- **No merging of a superseded turn's partial work** into the turn that
  replaces it - the replacement turn starts clean from `_build_initial_
  contents`, same as any other debounce-triggered turn.
- **No new owner-facing notice** for a superseded turn - from the owner's
  perspective in the agent drawer, this should read the same as any other
  turn that's still working (`agent_thinking` stays in whatever state it was
  in; a superseded turn does not currently get its own terminal status).
