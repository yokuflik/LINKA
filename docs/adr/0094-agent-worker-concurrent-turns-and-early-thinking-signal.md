# 0094 - Agent worker concurrent turns + early "thinking" signal

Status: Accepted

## Context

Two related gaps found by direct user testing (sending messages into two
different chats at once and watching only one agent turn progress at a time):

1. **`agent_invoke_stream` is drained fully sequentially, despite the
   existing config implying otherwise.** `AgentInvokeConsumer.process_entry`
   (`modules/agents/invoke_worker.py`) already wraps its body in
   `async with self._semaphore:`, and `AGENT_WORKER_CONCURRENCY` (default 10,
   `config/agent_settings.py`) is documented as "how many invocations one
   agent_worker process runs concurrently." In reality, the shared
   `BaseStreamConsumer._drain_shard` (`realtime/fanout/base_worker.py:126-138`)
   `await`s `process_entry` once per entry inside a plain `for` loop before
   reading the next batch - so at most one `process_entry` call is ever in
   flight, and the semaphore never has more than one holder. Two turns for
   two different `(agent_id, chat_id)` pairs - which the ADR 0063 turn mutex
   was always meant to allow to run concurrently - instead queue behind each
   other for up to `AGENT_TURN_TIMEOUT_SECONDS` (90s) each.

2. **No "thinking" signal in the owner-agent chat until the turn actually
   starts.** `_publish_agent_thinking(owner_user_id, "started")` fires from
   inside `_run_turn` (`invoke_worker.py:169`), i.e. only once a worker slot
   is free and the turn's own setup has begun. Combined with gap 1, a owner
   message sent while another chat's turn is mid-flight shows nothing in the
   drawer for up to 90s, then jumps straight to a reply - indistinguishable
   from the agent being unresponsive.

`BaseStreamConsumer` is shared with the persist and fan-out workers
(`realtime/fanout/worker.py`, `fanout_worker.py`), where today's
batch-sequential, single-session draining is deliberate (ADR 0001) - those
workers need the atomicity of "a batch is persisted/fanned-out and acked
in order" and are not the problem being fixed here. This ADR only changes
`AgentInvokeConsumer`'s own draining, not the shared base class's default
behavior.

## Decision

### 1. Real bounded concurrency for agent turns

First attempt (superseded within this same ADR, caught by direct testing
before shipping): `AgentInvokeConsumer` overriding only `_drain_shard` to
`asyncio.gather` a batch's entries. That fixed concurrency *within* one
`xreadgroup` batch, but `BaseStreamConsumer._run_shard`'s loop still `await`s
`drain_once` (i.e. the whole batch, including up to `AGENT_TURN_TIMEOUT_SECONDS`
per entry) before looping back to read again - an entry landing in Redis
*after* the current batch was already read, while that batch is still
mid-flight, simply isn't read until the in-flight batch fully drains. Found
by a real repro: two messages ~7s apart landed in separate batches, and the
second didn't start until the first's turn (several seconds) had already
ended - looking identical to full serialization from the outside, because
the bottleneck was the *read* loop, not the semaphore.

**Final design:** `AgentInvokeConsumer` overrides `run_forever` itself
(not `_drain_shard`/`_run_shard`), decoupling "read entries from the
stream" from "wait for them to finish processing" completely. One pump
loop calls `xreadgroup`/`XAUTOCLAIM` on a tight cycle (bounded by
`block_ms`) and fire-and-forget dispatches an `asyncio.create_task` per
entry; it never awaits a dispatched task before reading the next batch.
The same `asyncio.Semaphore(AGENT_WORKER_CONCURRENCY)` from before is
acquired *inside* each task, so the pump can read arbitrarily far ahead of
what's actually allowed to run - the semaphore still caps how many turns
are doing real work at once, it just no longer also throttles how often
the stream gets polled.

Consequences of the override:
- Each task opens its **own** `session_scope()` - `AsyncSession` is not
  safe for concurrent use from multiple tasks.
- XACK happens per entry, inside its own task, right after
  `process_entry` returns - not batched.
- A transient failure in one task only leaves that task's entry unacked
  for reclaim; it can never roll back a sibling task's uncommitted work,
  since sessions are never shared across tasks.
- `BaseStreamConsumer.run_forever`/`_run_shard`/`_drain_shard` are
  untouched and still used as-is by every other worker (persist, fan-out) -
  this override lives entirely on `AgentInvokeConsumer`.
- `AGENT_WORKER_CONCURRENCY` default lowered **10 -> 3** - sized for the
  1GB single-host demo deploy (ADR 0007): each concurrent turn holds a DB
  session/connection and runs an in-flight Gemini call; 3 concurrent turns
  is a conservative starting point, raised later once real memory headroom
  under load is observed.
- The per-`(agent_id, chat_id)` turn mutex (ADR 0063,
  `modules/agents/invoke_debounce.py::acquire_turn_lock`) is untouched and
  still the only thing preventing two turns for the *same* pair from running
  together - this ADR only removes the accidental serialization across
  *different* pairs.

### 2. Early "thinking" signal on debounce arm, not turn start

`_publish_agent_thinking(agent.owner_user_id, "started")` is additionally
called from `modules/agents/trigger_engine.py`, right where the owner-chat
branch calls `_arm_debounce_eager` (`trigger_engine.py:283`) - i.e. the
moment a message in the owner's own agent chat is confirmed to enqueue a
turn, before any worker slot, debounce window, or turn-mutex wait. This is
scoped to that one call site only (not the general per-agent trigger match
loop at `:389`, which fires for third-party chats the agent is restricted
to replying in, not the owner's own drawer) - the drawer's "thinking"
indicator exists to answer "is my agent working on what I just asked it,"
which only applies to the owner's own chat with it.

Firing `_publish_agent_thinking` from two places (here, and the existing
call inside `_run_turn`) is safe without new dedup logic:
`agent_thinking` is explicitly ephemeral/non-persisted/non-replayed
(`invoke_notify.py` docstring), and the frontend's `applyAgentThinking`
(`useAgentConfig.js:210-226`) overwrites a single ref and resets one timer -
idempotent against a duplicate "started" event, not an append/counter.

## Consequences

- Two different chats' agent turns (including two different owners' agents)
  now genuinely run at once, up to `AGENT_WORKER_CONCURRENCY` (3) per
  `agent_worker` process - horizontal scaling (more containers) still
  stacks on top of this, unchanged.
- The owner sees "thinking" in the drawer as soon as their message is
  confirmed to queue a turn, even while a worker slot is still occupied by
  an unrelated turn or the debounce window hasn't elapsed yet - closes the
  up-to-90s blank-drawer gap from gap 2 above.
- `BaseStreamConsumer`'s shared sequential draining is preserved for every
  other worker (persist, fan-out) - no behavior change there.
- New failure surface: a crashed `agent_worker` process with several
  in-flight tasks now loses up to `AGENT_WORKER_CONCURRENCY` unacked
  entries at once instead of one - already tolerated by the existing
  `XAUTOCLAIM`/`claim_idle_ms` reclaim path, no new handling needed.
