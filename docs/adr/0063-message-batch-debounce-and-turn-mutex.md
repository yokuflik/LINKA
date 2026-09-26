# 0063 - Message-batch debounce + per-chat turn mutex

Status: Accepted

## Context

A customer sending several messages in quick succession (a burst, or a
self-correction like "I want the blue iPhone" / "wait, actually purple")
currently produces one independent `agent_invoke_stream` entry per matched
message (`trigger_engine.py::_evaluate_triggers` → `enqueue_invocation`, one
call per message). With `AGENT_WORKER_CONCURRENCY` > 1 and
`AGENT_TURN_TIMEOUT_SECONDS` = 90, two (or more) full Gemini turns for the
*same* `(agent_id, chat_id)` can run concurrently: the second turn's
`_build_initial_contents` re-reads chat history at whatever point it happens
to dequeue, with no ordering guarantee against the first turn's in-flight
tool calls/reply. This can produce duplicate/contradictory replies, or a
turn that calls a tool (e.g. `resolve_user`, `send_message`) against
information a same-burst follow-up message already superseded.

A prompt-only fix (read the transcript backward, let the latest message in a
correction sequence override earlier ones) helps *within* one turn's
transcript, but does nothing about two turns racing - the transcript a
second concurrent turn reads may not yet even contain the first turn's
output, and nothing stops both from firing tools independently. This ADR
adds the queuing-side fix; the prompt instruction (added to
`personas.py`'s system prompts) is a complementary, cheap addition on top,
not a substitute.

## Decision

Two independent mechanisms, both scoped to `(agent_id, chat_id)`:

**1. Debounce/coalescing before enqueue.** `trigger_engine.py` no longer
calls `enqueue_invocation` directly from `_evaluate_triggers`. Instead, a
matched trigger `ZADD`s member `"{agent_id}:{chat_id}"` onto a new Redis
ZSET `agent_invoke_debounce_due`, score = `now + AGENT_INVOKE_DEBOUNCE_SECONDS`
(default 2s). `ZADD` on an existing member overwrites its score - so a
second message for the same pair arriving before the first one fires simply
pushes the due-time forward, coalescing the burst into a single fire. All
quota/permission checks (`agent_activation`, judge gate, `blocked_read_chat_ids`,
ephemeral-task matching, etc.) stay exactly where they are today, evaluated
per-message at match time - only the final `enqueue_invocation` call moves
into this delayed step. At fire time the turn re-reads chat history fresh
(`_build_initial_contents`/`get_message_history`, unchanged), so a coalesced
batch naturally shows up as consecutive "Customer: ..." lines in one
transcript.

Reuses the existing `agent_worker` schedule-poll loop pattern (ADR 0046
decision 3's `on_schedule` due-ZSET) rather than a new process: a new tight
poll (`AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS`, default 1s) inside
`invoke_worker.py::run_forever`, alongside the existing 30s schedule poll,
pops due members (`ZRANGEBYSCORE` + `ZREM`) and calls `enqueue_invocation`
for each with the chat's latest matched `message_id` (re-queried, not
carried on the ZSET member - avoids a second Redis structure to keep in
sync).

**2. Per-chat turn mutex.** A debounced fire can still land while a *previous*
turn for the same `(agent_id, chat_id)` is still running (up to 90s) - the
debounce window is deliberately short (seconds, for UX) and turn latency is
not. `AgentInvokeConsumer.process_entry` acquires a Redis lock
(`agent_turn_lock:{agent_id}:{chat_id}`, `SET NX EX AGENT_TURN_TIMEOUT_SECONDS`)
before calling `_run_turn`, releasing it in a `finally`. If the lock is held,
the entry does **not** run a second concurrent turn and does **not** drop
the message: it re-arms the debounce ZSET for that pair
(`AGENT_INVOKE_DEBOUNCE_SECONDS` out, same coalescing path as case 1) so the
message is retried right after the current turn finishes, rather than
cancelling/preempting the in-flight turn or busy-waiting for it.

## Guardrails

- `AGENT_INVOKE_DEBOUNCE_SECONDS` (default 2) - coalescing window; short
  enough to feel instant, long enough to catch a fast correction.
- `AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS` (default 1) - new poll tick
  inside the existing `agent_worker` process, not a new service.
- Lock TTL = `AGENT_TURN_TIMEOUT_SECONDS`, so a crashed worker holding the
  lock self-heals within the same bound the turn itself is already capped
  at - no separate expiry knob to keep in sync.
- Fire-and-forget semantics unchanged (ADR 0045): a dropped/failed debounce
  step must never block message delivery; failures here are logged and
  swallowed exactly like `enqueue_invocation` already is treated by its
  caller.
- Config-mode turns (owner's own agent chat) go through the same mutex and
  debounce path as execution-mode - no special-case carve-out; a burst of
  owner messages to their own agent coalesces the same way.

## What this deliberately does not change

`_evaluate_triggers`'s matching logic, quota checks, the Judge gate (ADR
0053), `on_schedule`/`on_ephemeral_task` firing (those already go through
`enqueue_schedule_fire`, a separate call not touched here - though the same
turn mutex in `process_entry` applies to them too, since it's keyed only on
`(agent_id, chat_id)` and a schedule-fired turn with `chat_id=None` simply
never contends). No change to `AGENT_WORKER_CONCURRENCY` or the semaphore -
concurrency across *different* chats/agents is unaffected.
