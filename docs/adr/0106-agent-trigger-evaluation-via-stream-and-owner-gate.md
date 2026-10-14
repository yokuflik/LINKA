# ADR 0106 - Agent trigger evaluation moves off `create_task` onto a gated stream

Status: Accepted

## Context

`modules/messaging/send.py::process_outgoing` ended with
`asyncio.create_task(evaluate_triggers(message))` (ADR 0045). Every persisted
message - for every user, agent or not - spawned an unbounded task inside the
send worker's process that opened its own DB session and ran at least
`get_agent_by_owner_chat` + `get_chat_participants` against Postgres before the
Redis pre-filter (ADR 0046, `agent:enabled_owners`) was even consulted. At scale:

- no backpressure (task count unbounded under load);
- a Postgres round-trip per message even when nobody in the chat has an agent
  (the overwhelmingly common case);
- trigger work competes with ordinary message persistence for the send worker's
  DB pool;
- no durability (process crash drops in-flight evaluations).

## Decision

1. **Gate in the fan-out worker.** `fan_out_message` already loads the chat's
   participants. At its end it runs one pipelined `SMISMEMBER agent:enabled_owners
   <recipient_ids>`. If no recipient has an enabled agent, nothing else happens
   (cost: one Redis call, no DB). Special case: a chat whose only participant is
   the sender (the owner<->agent chat, ADR 0048) checks `sender_id` instead.
   System messages / null senders are skipped (same rule the engine applies).
2. **Stream, not pub/sub.** On a hit, `XADD agent_trigger_stream {message_id,
   chat_id}` (new `modules/agents/trigger_queue.py`). Pub/sub was rejected: it is
   not durable (an absent subscriber drops the event). The SET answers "should
   this be queued at all", the stream is the queue.
3. **Consumer in `agent_worker`.** A new `AgentTriggerConsumer`
   (`modules/agents/trigger_worker.py`, a `BaseStreamConsumer`, single stream,
   shared consumer group) runs in the existing `agent_worker` process next to the
   invoke consumer and poll loops. It loads the message row and runs the
   unchanged `_evaluate_triggers` (which still re-checks everything in Postgres,
   so the SET is only a coarse filter, never a security boundary).
4. **Owner-keyed, not chat-keyed.** The set stays keyed by *owner user id*
   (maintained only on agent enable/disable by `sync_agent_cache` /
   `remove_agent_cache`), not by chat id. A chat-keyed set would need an update
   per chat on enable/disable (an owner can have thousands) plus on every
   join/leave/new chat.
5. **The SET is now authoritative for the gate -> reconcile.** Previously a lost
   `SADD` self-healed because `_load_trigger_cfg` fell back to Postgres. With the
   gate, a missing member silently skips triggers. So `reconcile_enabled_owners`
   (diff against `agents.is_enabled`: SADD missing, SREM stale) runs at
   `agent_worker` start and every `AGENT_ENABLED_OWNERS_RECONCILE_SECONDS`
   (default 600). Residual window: an enable that races a reconcile snapshot can
   be missed for up to one interval.
6. **Failure policy (per owner, inside `_evaluate_triggers`).** The per-owner body is
   split into `_evaluate_owner`; each rate-limit counter it bumps is recorded
   (`_consume`) so a failure can refund it (`infra.ratelimit.service.refund_increment`).
   - **Infra error** (SQLAlchemy / Redis / OSError / timeout): refund, then the
     consumer re-XADDs the entry with `attempts+1`, restricted to the failed
     `owner_ids` (already-handled owners are not re-evaluated), up to
     `AGENT_TRIGGER_MAX_ATTEMPTS` (3, 1s*n backoff); then dropped with a warning.
     A server crash mid-evaluation is accepted as a dropped trigger.
   - **Invalid agent data** (`AgentConfigError`, raised only while parsing the
     agent's own stored triggers/restrictions/paused_chat_ids): refund, no retry,
     and a cooldown-gated system message in the owner's agent chat says the agent's
     settings look invalid. Deliberately narrow - an unexpected exception elsewhere
     (possibly our bug) is logged, refunded and dropped, never blamed on the owner.
   - The old entry is always acked (no stream redelivery, which would double-count
     quota). A missing message row is acked.
   The XADD in the fan-out worker is best-effort (never fails delivery).

`evaluate_triggers` / `_evaluate_triggers` keep their signatures, so the existing
trigger-engine tests are unchanged. `send.py` no longer imports the trigger
engine.

## Consequences

- Plain messages cost one Redis call and zero extra DB work in the send/fan-out
  path; trigger evaluation load is bounded by the stream and runs in a separate
  process/container (horizontal scale = more `agent_worker` containers).
- Trigger evaluation is delayed by one stream hop (ms); negligible against the
  2s debounce (ADR 0063).
- If `agent_worker` is down, entries accumulate (approximate MAXLEN trim) and are
  evaluated when it returns; previously triggers ran inside the app regardless.
- New config: `AGENT_TRIGGER_STREAM_KEY/MAXLEN/GROUP/BATCH/BLOCK_MS/CLAIM_IDLE_MS`, `AGENT_TRIGGER_MAX_ATTEMPTS`,
  `AGENT_ENABLED_OWNERS_RECONCILE_SECONDS`.
