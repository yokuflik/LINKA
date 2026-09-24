# 0045 - AI Agent (service account, Gemini tool calling)

Status: Accepted

## Context

Add an optional autonomous AI agent per Linka user, powered by Gemini 1.5
Flash with Function Calling. The agent acts as a service account: it can
perform a restricted set of messaging actions on the owner's behalf
(`user_id` = the owner's), gated by wake-up triggers the owner configures
and by hard, server-enforced restrictions.

Full design history (permission model debate, execution-model rejection of
epoch/time-slicing scheduling in favor of a standard async worker pool,
rate-limit layering) lives in the assistant's session memory
(`project_linka_ai_agent_plan`); this ADR captures the final, implementation
-governing shape only.

## Decision

**Permission model — denylist, not allowlist.**
`Agent.restrictions` is a JSONB blob (default = full permission within the
messaging domain). There is no account-management tool at all (no
profile/password/settings changes) - those actions are simply absent from
the tool registry, not "blocked". Restrictions only ever narrow within the
messaging domain:

```json
{
  "can_send_messages": true,
  "can_message_groups": true,
  "can_message_private": true,
  "can_message_new_private_contacts": true,
  "can_leave_groups": true,
  "blocked_read_chat_ids": [],
  "max_messages_per_day": null
}
```

- `can_send_messages=false` disables `send_message` + `reply_message`
  entirely (master switch).
- `can_message_new_private_contacts=false` blocks only `create_chat`
  (opening a brand-new 1:1); replying inside an existing 1:1 chat is still
  governed by `can_message_private` alone.
- `blocked_read_chat_ids`: the Trigger Rule Engine skips these chats
  entirely at evaluation time - the agent is never invoked for them, not
  merely refused at the tool layer. Cheaper and matches "can't read"
  literally.
- `max_messages_per_day`: a cumulative send cap independent of the Gemini
  API rate limit below - guards against a *technically* rate-limit-compliant
  agent still blasting one chat with dozens of messages in a burst.
- JSONB (not per-capability DB columns) matches the ADR 0002 `UserSettings`
  pattern for storage shape only. **Enforcement is still hard**: every tool
  call is checked against `Agent.restrictions` server-side in
  `execute_tool_call`, never left to the system prompt as an instruction.
- `system_prompt` (free text) is an explicit **soft** constraint - behavioral
  guidance injected into the Gemini system_instruction, not a security
  boundary. The config UI must visually separate soft (free text) from hard
  (enforced toggles) sections so this distinction isn't lost on the owner.
- Global kill switch: `Agent.is_enabled`, checked both at trigger-evaluation
  time and again at worker-dequeue time (defense in depth for the
  enqueue-to-dequeue window).
- One agent per user (`Agent.owner_user_id` unique).

**Trigger mechanism ("Gatekeeper") - not always-on listening.**
`Agent.triggers` JSONB, owner-configured:

```json
{
  "on_time_window": {"enabled": false, "start": "09:00", "end": "22:00"},
  "on_specific_chats": {
    "<chat_id>": {"keywords": []}
  }
}
```

- `on_specific_chats`: per-chat opt-in. Empty `keywords` = wake on *any*
  message in that chat; non-empty = substring (case-insensitive, not regex -
  avoids ReDoS from user-supplied keyword input) match required.
- `on_time_window`: the agent only wakes for messages received inside this
  daily local-time window.
- A dedicated tool, `update_own_triggers`, lets the agent modify its *own*
  `Agent.triggers` at runtime (e.g. add a keyword, narrow its own window).
  `execute_tool_call` hard-scopes this to the calling `agent_id` only - it
  can never touch `restrictions`, another agent's row, or anything outside
  `triggers`.
- Evaluation happens synchronously but cheaply inside the existing
  message-persistence path (`modules/messaging/service.py`, right after a
  message is saved, in parallel with the existing fan-out - not blocking
  it). Short-circuits immediately if no participant in the chat owns an
  enabled agent.
- Hourly activation quota: 20 triggers/hour per agent, Redis fixed-window
  counter (`agent:{agent_id}:activations`, TTL 3600s), reusing the existing
  `infra/ratelimit` engine. Exceeding it silently drops the trigger (message
  still delivered normally; the agent just doesn't respond) - no
  backlog/queueing of missed triggers.
- A matched trigger (quota allowing) pushes an event onto a new Redis Stream
  `agent_invoke_stream` (payload: `agent_id`, `chat_id`, `message_id`).

**Owner-agent chat.**
Every agent gets one auto-created, permanent 1:1 `Chat` with its owner
(`Agent.owner_agent_chat_id`), created alongside the `Agent` row. Used for
system notifications (e.g. daily time-budget exhaustion, see below) and as
a general channel for the owner to talk to their agent directly.

**Execution model - standard async worker pool, not time-slicing.**
Agents are I/O-bound (Gemini HTTP + DB), so asyncio already yields at every
`await`; an artificial epoch/round-robin scheduler would only reinvent
cooperative multitasking with added state-resumption complexity, for no
benefit at this workload profile. Instead:

- `agent_invoke_stream` (Redis Stream, consumer groups) consumed by a
  dedicated async worker pool - horizontal scaling is "add more
  `agent_worker` container instances", no code change.
- Concurrency bounded by `asyncio.Semaphore(N)` inside each worker, not by
  time-slicing.
- The worker runs in its **own Docker Compose service** (`agent_worker`),
  fully decoupled from the main Uvicorn/FastAPI process, so a crash in the
  agent worker cannot affect the main event loop / WS gateway.
- Per-turn wall-clock timeout via `asyncio.wait_for` aborts a stuck turn
  cleanly instead of "resuming later".

**Daily active-time budget - 1 hour/day per agent.**
Separate from the hourly *activation* quota above. Counts actual processing
wall-clock time per turn (Gemini calls + tool execution), accumulated in a
Redis fixed-window counter keyed per agent, resetting daily. When an agent's
accumulated processing time for the day would exceed 1 hour, the worker:

1. Finishes the current turn if already in flight (not interrupted
   mid-turn).
2. Sends one message into the agent's `owner_agent_chat_id` announcing it
   has used up its time for today.
3. Goes dormant (ignores further triggers) until the daily window resets.

This mirrors the existing hourly-activation-quota pattern (a Redis
fixed-window counter reused from `infra/ratelimit`), but the unit counted is
elapsed processing seconds, not activation count.

**Rate limiting (independent layers, all via `infra/ratelimit`, ADR 0012).**
1. Gemini API calls: 5 calls/minute, strictly per-agent
   (`agent:{agent_id}:gemini_calls`).
2. Function-calling recursion cap: 4 round-trips per turn (so 1 initial call
   + up to 4 tool-response round-trips = 5 calls fits inside the 5/min
   budget without being cut off mid-conversation).
3. Hourly trigger/activation quota: 20/hour per agent - gates queue entry,
   separate from the Gemini-call limit which gates API usage once a worker
   is processing a turn.
4. Daily active-time budget: 1 hour/day per agent (above) - gates whether
   the agent processes at all that day, independent of both limits above.
5. Actual tool-call side effects (`send_message`, `leave_group`, etc.) reuse
   the SAME existing per-user rate limits as a normal human client - the
   agent acts with the owner's `user_id` through the same
   `modules.messaging.service.send_message` etc., so it is not a channel to
   bypass normal user rate limits, only a consumer of the same budget.
6. No separate "confirmation token" step exists for destructive actions
   (`leave_group`, `purge`) - the `restrictions` JSONB is the only gate, no
   added friction, consistent with the "default allow unless restricted"
   model.

## Schema

```python
class Agent(Base):
    __tablename__ = "agents"
    id: BigInteger            # Snowflake, PK
    owner_user_id: BigInteger  # FK -> users.id, UNIQUE (one agent per user)
    owner_agent_chat_id: BigInteger  # FK -> chats.id, the permanent 1:1 owner<->agent chat
    system_prompt: str        # soft constraint, default ""
    restrictions: dict        # JSONB, see above
    triggers: dict            # JSONB, see above
    is_enabled: bool          # kill switch, default True
    created_at, updated_at

class AgentToolCallLog(Base):
    __tablename__ = "agent_tool_call_log"  # unpartitioned to start; promote to
                                            # partitioned later if volume warrants (ADR 0005 pattern)
    id: BigInteger  # Snowflake, PK
    agent_id: BigInteger  # FK -> agents.id
    tool_name: str
    arguments: dict  # JSONB
    allowed: bool
    denial_reason: str | None
    created_at
```

No migration system exists yet (per repo convention) - both tables are
added by extending `scripts/init_db.py`.

## Tool registry

Messaging domain only, no account-management tools exist at all:
`send_message`, `create_chat`, `leave_group`, `reply_message`,
`read_history`, `update_own_triggers`. Each maps to the existing service
handler (`modules.messaging.service.*`, `modules.chats.service.*`) plus a
`target_field` (e.g. `chat_id` or `target_user_id`) checked against
`restrictions.blocked_target_user_ids`-equivalent fields, except
`update_own_triggers` which is hard-scoped to the caller's own `agent_id`
and never checked against `restrictions` (it doesn't touch it).

Context window for the agent's Gemini conversation: last 20 messages of the
relevant chat.

## Consequences

- New tables `agents` / `agent_tool_call_log`, no changes to existing
  tables.
- A new Redis Stream (`agent_invoke_stream`) and a new Docker Compose
  service (`agent_worker`) are required before the agent can run end-to-end
  - deferred to a later implementation step.
- The daily time-budget notification depends on the owner-agent chat
  existing, so `owner_agent_chat_id` must be populated at `Agent` creation
  time, not lazily.
