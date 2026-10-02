# 0095 - Self-expiring triggers; `one_off_action` gets scoped trigger write access

Status: Accepted

## Context

The owner asked for a concrete scenario: "buy me a MacBook from this company,
find out all the details and tell me the timeline, and once you're done,
delete the trigger." Today this does not work end-to-end:

1. `BuilderState.ONE_OFF_ACTION` (ADR 0093's default/idle owner-chat state,
   where a direct request like this lands) has no reachable trigger-writing
   tool at all. `update_own_triggers`/`set_trigger` were scoped to
   config-mode-only states in ADR 0091 (prompt-injection hardening - an
   execution-mode persona talking to a third party must never be able to
   rewrite its own wake conditions) and never added back to
   `ONE_OFF_ACTION` when ADR 0093 introduced it as a new state distinct from
   the old Supervisor. Confirmed by code inspection:
   `ONE_OFF_ACTION_PROMPT` (`modules/agents/builder_flow.py`) already
   *mentions* `update_own_triggers` in its tool list, but
   `BUILDER_STATE_HANDLERS[ONE_OFF_ACTION]`
   (`modules/agents/tools/builder_handoff.py`) and
   `BUILDER_STATE_TOOL_SCHEMAS[ONE_OFF_ACTION]`
   (`modules/agents/tools/schemas.py`) both omit it - a pre-existing
   prompt/tool-registry drift bug, fixed as part of this ADR.
2. Even from `BuilderState.BUILDER`, where trigger-writing *is* reachable,
   no trigger type supports any form of self-expiry. `on_schedule`
   (`kind=once`) and `on_ephemeral_task` (ADR 0061, lazy-expiry) already
   clean up after themselves - those are explicitly out of scope here. But
   `on_specific_chats`/`on_unknown_sender`/`on_any_message` persist forever
   until an owner (or another config-mode turn) edits them out by hand. A
   one-off instruction like "wake up on replies from this company until you
   get a final answer, then stop" has no way to express "stop" on its own.

## Decision

### Scoped trigger-write access from `ONE_OFF_ACTION`

`ONE_OFF_ACTION` gains two tools, both enforcing a hard server-side rule that
a trigger entry **written from this state must always carry `expires_at`
and/or `max_fires`** - `ONE_OFF_ACTION` can create disposable, self-cleaning
triggers only, never a standing/permanent one. Creating or editing a
*permanent* trigger (no expiry, no fire cap) stays exclusive to
`BuilderState.BUILDER`, preserving the existing "one-off actions don't
rewrite persistent configuration" boundary.

- `update_own_triggers` (existing handler/schema, `modules/agents/tools/
  execution.py` + `schemas.py`) is added to
  `BUILDER_STATE_HANDLERS[ONE_OFF_ACTION]` /
  `BUILDER_STATE_TOOL_SCHEMAS[ONE_OFF_ACTION]`, fixing the prompt/registry
  drift noted above. The handler (`_tool_update_own_triggers`) gains a
  `require_expiry: bool` keyword (default `False`, unchanged for `BUILDER`'s
  call site); `ONE_OFF_ACTION`'s dispatch passes `require_expiry=True`. When
  set, every `on_specific_chats` entry and every `on_unknown_sender`/
  `on_any_message` object in the patch must include at least one of
  `expires_at`/`max_fires`, or the call raises `ToolDeniedError` naming the
  offending key - never silently stripped, never silently made permanent.
- New config-mode tool `delete_own_trigger` (handler `modules/agents/tools/
  execution.py::_tool_delete_own_trigger`, schema in `schemas.py`,
  CRUD helper `modules/agents/crud.py::delete_agent_trigger`): takes a
  `kind` (`"on_specific_chats"` + `chat_id`, `"on_unknown_sender"`, or
  `"on_any_message"`) and removes that trigger outright. This is the
  explicit, model-invoked path for "I'm done, stop watching this" - chosen
  over an implicit/automatic heuristic (e.g. the model inferring completion
  from its own reply) because the model deciding for itself that a
  real-world condition was satisfied is exactly the kind of judgment call
  that should be an explicit, auditable tool call, not inferred control
  flow. Added to `BUILDER_STATE_HANDLERS`/`BUILDER_STATE_TOOL_SCHEMAS` for
  both `ONE_OFF_ACTION` and `BUILDER`.

### Trigger schema: `expires_at` / `max_fires`

Both fields are optional, additive, and orthogonal - a trigger entry may
carry either, both, or neither (neither = permanent, unchanged default
behavior):

- `expires_at`: ISO-8601 UTC datetime. Once past, the trigger stops matching
  and is deleted on the next evaluation that would have matched it (lazy
  expiry - the exact pattern already used for `Agent.paused_chat_ids`,
  ADR 0054/`_active_paused_chat_ids`).
- `max_fires`: positive int. Decremented by one on every successful match;
  deleted once it reaches zero (checked *before* decrementing, so the fire
  that brings it to zero still goes through).

No schema/migration needed (`Agent.triggers` is JSONB, no-migrations
convention, ADR-style additive default - existing rows with no `expires_at`/
`max_fires` on an entry behave exactly as before).

Shape, per trigger type:

- `on_specific_chats[chat_id]`: `{"keywords": [...], "expires_at": "...",
  "max_fires": N}` - both new fields optional, alongside the existing
  `keywords`.
- `on_unknown_sender` / `on_any_message`: `{"enabled": true, "expires_at":
  "...", "max_fires": N}` - same two optional fields added to the existing
  object shape.

`on_schedule`/`on_ephemeral_task` are unchanged - explicitly out of scope,
they already self-clean via their own mechanisms.

### Evaluation + auto-delete (`modules/agents/trigger_engine.py`)

- `_matches_trigger_config` (per-chat `on_specific_chats` entries) and the
  `on_unknown_sender`/`on_any_message` checks each gain an expiry/fire-count
  guard: an expired or exhausted entry is treated as a non-match (`return
  False`) exactly as if it didn't exist.
- The actual deletion happens in `evaluate_triggers`, after a match is
  confirmed and the turn is about to be enqueued (not inside the pure
  `_matches_*` predicates, which stay read-only) - mirrors how
  `on_unknown_sender`'s auto-registration (ADR 0051) and the eager-supersede
  check (ADR 0077) already do their own post-match DB writes from
  `evaluate_triggers`'s own session. A matched entry with `max_fires` is
  decremented (deleted if it hits zero); a matched entry with `expires_at`
  that has now lapsed is deleted; either case re-runs `sync_agent_cache`
  (same as every other trigger-config write) so the pre-filter cache
  (ADR 0046 decision 1) stays correct for the next message.
- Fail-open on a malformed `expires_at` (unparseable string) - same
  discipline as `_within_time_window`'s existing malformed-config handling;
  a config error must never silently and permanently gag the agent.

### Prompt changes

`ONE_OFF_ACTION_PROMPT` gets an explicit rule: when the owner's request
implies "do X, then stop watching/stop replying" (a bounded task with a
natural end condition), create the trigger via `update_own_triggers` with
`max_fires`/`expires_at` set to match that condition as closely as possible
(e.g. "until you get a final answer" -> a generous `expires_at`, since
`max_fires` can't express an open-ended wait); call `delete_own_trigger`
once the task is actually confirmed complete rather than leaving a
one-fire-remaining trigger dangling. Never create a trigger with neither
field set from this state (the tool enforces it server-side regardless, but
the prompt should not even attempt it).

## Consequences

- Closes the concrete gap: "message this company, wait for their replies,
  report the timeline to me, then stop" is now expressible without a
  `BUILDER` round-trip, while `BUILDER` remains the only place a *permanent*
  trigger can be created or edited.
- `delete_own_trigger` is a new, narrow, auditable surface - no broader than
  `update_own_triggers` already was (same config-mode-only gate, same
  agent-scoped write).
- `_merge_triggers` (`crud.py`) is unchanged - `expires_at`/`max_fires` ride
  inside the same per-chat/per-flag dicts it already merges one level deep;
  no new merge-path code needed.
- Test fallout: `tests/modules/agents/` - `trigger_engine.py` expiry/
  fire-count matching and auto-delete, `_tool_update_own_triggers`'s new
  `require_expiry` gate, `_tool_delete_own_trigger`, and
  `ONE_OFF_ACTION`'s/`BUILDER`'s expected handler/schema sets in
  `test_dispatch.py`/`test_builder_flow.py`.
