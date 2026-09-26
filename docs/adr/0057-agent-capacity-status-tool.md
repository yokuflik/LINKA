# 0057 - Agent capacity/rate-limit self-awareness tool

Status: Accepted
Date: 2026-09-26

## Context

The owner configuring an agent through the Builder has no way to know what
the various hard limits in `.claude_docs/security_and_rate_limiting.md` /
`.claude_docs/ai_agent.md` actually mean for their specific use case - "how
many customers can this handle," "how many messages before it stops
responding," "will it survive a busy hour." Today those numbers
(`AGENT_ACTIVATION_QUOTA_PER_HOUR`, `AGENT_GEMINI_CALLS_PER_MINUTE`,
`AGENT_DAILY_ACTIVE_SECONDS_BUDGET`, `AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY`,
`AGENT_MAX_AUTO_CHATS`, `AGENT_MAX_SCHEDULE_ENTRIES`, restrictions'
`max_messages_per_day`) only exist as env-configured constants and Redis
counters; nobody surfaces them to the owner in plain language, and nothing
projects them into "roughly N customers/hour" style guidance.

The user asked for exactly this: a backend tool the Builder can call for a
full picture of the agent's current limits + current usage, so it can
calculate for the owner what capacity that implies (e.g. messages/hour a
single customer can expect a reply within, how many concurrent customers
before the hourly activation quota is the bottleneck), and so the finishing
summary after `finish_building_agent` always includes a rough capacity
estimate.

## Decision

1. **New read-only config-mode tool `get_capacity_status`** (Builder state
   only - not Supervisor, not Help, not execution mode). Returns every
   relevant rate limit's configured max alongside the agent's current usage
   read live from Redis/Postgres, in one call:
   - `activation_quota`: `{used, max, window_seconds}` from
     `ratelimit:agent_activation:{agent_id}` (fixed-window `GET`, no
     increment).
   - `gemini_calls`: `{used, max, window_seconds}` from
     `ratelimit:agent_gemini_calls:{agent_id}` (fixed-window `GET`, no
     increment) - both `agent_activation` and `agent_gemini_calls` are plain
     `check_and_increment` fixed-window counters, not sliding windows, so a
     new `infra/ratelimit/service.py::peek_fixed_window(identifier, action)`
     (bare Redis `GET`, no `INCR`) is the one new primitive this ADR adds to
     the rate-limiter engine - everything else reuses functions/counts that
     already exist.
   - `daily_active_seconds`: `{used, max}` from
     `ratelimit:agent_active_seconds:{agent_id}`.
   - `unknown_sender_daily_quota`: `{max, window_seconds}` - the Redis key
     (`ratelimit:agent_unknown_sender:{agent_id}:{sender_user_id}`) is keyed
     per-sender, so there is no single agent-wide "used" number; reported as
     a per-customer cap only (max/window), no `used` field.
   - `max_messages_per_day`: the owner's own configured
     `Agent.restrictions.max_messages_per_day` (or `null` = unlimited) - the
     one limit that's owner-configured rather than a platform constant.
   - `knowledge_base`: `{documents_used, documents_max, chunks_used,
     chunks_max}` - reuses the existing `modules/agents/crud.py::count_
     knowledge_documents`/`count_knowledge_chunks`, no new queries.
   - `schedule_entries`: `{used, max}` - `used` is simply
     `len(agent.triggers.get("on_schedule", []))`, the same in-memory count
     `crud.py::_check_schedule_quota` already does (no persisted counter or
     separate table exists for this).
   - `auto_registered_chats`: `{used, max}` (ADR 0051's `AGENT_MAX_AUTO_
     CHATS`, FIFO-evicted subset of `on_specific_chats`).

   This is a **read**, not an enforcement point - it never raises, never
   blocks a real action, and reading it costs no quota. It exists purely so
   the model (and, through it, the owner) can reason about headroom.

2. **The tool does the arithmetic, not just returns raw numbers.** Along
   with the raw counters, `get_capacity_status` returns a
   `capacity_estimate` block doing the one non-obvious calculation an owner
   actually wants - "how many people can message this agent and get a
   response within an hour" - as
   `min(activation_quota.max, gemini_calls.max * (activation_quota.
   window_seconds / gemini_calls.window_seconds), floor(daily_active_
   seconds.max / AGENT_ESTIMATED_SECONDS_PER_TURN)) - activation_quota.used`,
   labelled clearly as an approximation (a real turn's Gemini-call count and
   wall-clock cost vary with tool use, e.g. Agentic RAG round-trips), not a
   guarantee. `AGENT_ESTIMATED_SECONDS_PER_TURN` (new constant,
   `config/agent_settings.py`, default 8) is a rough single-turn cost
   estimate used only for this projection - never used anywhere in real
   enforcement.

3. **Where it's wired**: `modules/agents/tools/config_mode.py` gets a new
   handler; `CONFIG_TOOL_SCHEMAS` gets its schema; `BUILDER_STATE_TOOL_
   SCHEMAS[BuilderState.BUILDER]` (the only builder-state array that
   includes `CONFIG_TOOL_SCHEMAS` wholesale) picks it up automatically, so
   no `dispatch.py` changes are needed - same mechanism `resolve_user` used
   when it was added. Supervisor and Help do not get this tool: capacity
   planning is only relevant mid-build.

4. **`finish_building_agent` summary always includes a capacity estimate.**
   `BUILDER_PROMPT` (`modules/agents/builder_flow.py`) is amended to
   require calling `get_capacity_status` once, near the end of the
   interview (after the four checklist items are settled, before calling
   `finish_building_agent`), and to fold its `capacity_estimate` into the
   final natural-language summary message - phrased as a rough estimate
   ("at current settings this can handle roughly N new conversations an
   hour before it needs to catch up"), not a hard promise. This is a
   prompt-level requirement, not code-enforced (consistent with every other
   "narrate every save" instruction already in `BUILDER_PROMPT` - there is
   no tool coupling `finish_building_agent` to a prior `get_capacity_status`
   call).

## Consequences

- No new schema, no new Redis keys - purely reads keys that already exist
  for enforcement elsewhere (`infra/ratelimit`, `time_budget.py`,
  `modules/agents/crud.py`'s document/chunk/schedule counts).
- No behavior change to any existing limit; this is observability, not a
  new constraint.
- The capacity estimate is necessarily approximate (real turns vary in
  Gemini-call count and duration); the tool's docstring, schema
  `description`, and the Builder's phrasing instructions all say so
  explicitly to avoid the owner reading it as a guarantee.
- `HELP_PROMPT` does not need a matching update per the standing obligation
  in `.claude_docs/ai_agent.md` - it's generic/conceptual and doesn't
  describe specific tool mechanics; if a future change makes Help describe
  the Builder's tools in detail, this one should be added then.
