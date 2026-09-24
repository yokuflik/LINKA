# 0049 - Supervisor / Builder / Help sub-states inside the agent-builder config chat

Status: Accepted

## Context

ADR 0047 gave the owner's config chat (`chat_id == owner_agent_chat_id`) exactly
one persona (`agent_builder`) and one flat tool set (`CONFIG_TOOL_SCHEMAS`, 6
tools). In practice the builder persona has to do two very different jobs in
the same breath - interview the owner about *what to build* AND explain *how
the system works* when the owner gets confused - and today both live in one
system prompt with one tool list, so there is no clean way to route "how does
this work?" to different, better-tailored behavior without it bleeding into
the interview flow.

This ADR adds three **sub-states inside config mode** (Supervisor -> Builder
<-> Help), each with its own system prompt and its own disjoint tool schema
list, selected the same structural way ADR 0047 selects config-vs-execution
mode: never by what the model claims about itself, only by server-held state.
Confirmed with the user (2026-09-24):

- The existing 6 config tools stay exactly as built (incremental saves during
  the interview) - no new bulk-config-apply path. `finish_building_agent` is a
  lightweight wrap-up signal, not a JSON blob.
- Calling `finish_building_agent` auto-enables the agent (`is_enabled = True`).

**What does NOT change:** the outer boundary from ADR 0047 decision 4
(`is_config_mode(agent, chat_id)`, execution tools vs. config tools, the whole
hard gate keyed on `chat_id`). Execution-mode chats are completely unaffected
by this ADR - they never see any of the new tools or states. This ADR only
subdivides what happens *inside* the config chat.

## Decision

### 1. New column: `Agent.builder_state`

Dynamic runtime state, same category as `paused_chat_ids` (not owner-authored
config like `restrictions`/`triggers`) - meaningful *only* when
`chat_id == owner_agent_chat_id`; execution-mode turns never read or write it.
One of `"supervisor" | "builder_agent" | "help_agent"`, default `"supervisor"`.

**Not added to the Redis `agent:trigger_cfg` cache**
(`modules/agents/cache.py::sync_agent_cache`) - that cache exists to let
*other* users' incoming messages skip a DB query when pre-filtering whose
agent might wake (ADR 0046 decision 1). `builder_state` is only ever read
inside the owner's own config-chat turn, which already does a fresh
`get_agent_by_id` fetch in `invoke_worker._run_turn` - there is no cross-owner
read path that would benefit from caching it.

### 2. New module: `modules/agents/builder_flow.py`

`BuilderState` enum (`SUPERVISOR`/`BUILDER`/`HELP`), the three system prompts
(`BUILDER_STATE_PROMPTS`), and the three handoff tool schemas
(`transfer_to_builder`, `transfer_to_help`, `finish_building_agent`). Kept
separate from `personas.py` (stays the fixed execution-skill catalog) and from
`tools.py`'s existing config-tool handlers (unchanged, merged in).

Tool set per state:

| State | Tools available |
|---|---|
| `supervisor` | `transfer_to_builder` only |
| `builder_agent` | the existing 6 config tools + `transfer_to_help` + `finish_building_agent` |
| `help_agent` | `transfer_to_builder` only |

### 3. `modules/agents/tools.py`

Three new handlers (`_tool_transfer_to_builder`, `_tool_transfer_to_help`,
`_tool_finish_building_agent`), all routed through the existing
`update_agent_config` write path - no new mutation mechanism.
`_tool_finish_building_agent` also flips `is_enabled=True` and calls
`sync_agent_cache` (the only one of the three handoff tools that touches a
field the trigger pre-filter cache tracks).

`get_tool_schemas_for_chat` and `execute_tool_call` both extend their existing
config-mode branch to key off `agent.builder_state` (3-way dispatch instead of
1-way) - `execute_tool_call` already receives `agent`, so no new parameter.
The defense-in-depth allowlist recheck from ADR 0047 decision 4 is preserved
exactly, just against a per-state allowlist instead of one flat config
allowlist.

### 4. `modules/agents/crud.py`

`update_agent_config` gains a `builder_state` patch key, same shallow-replace
pattern as `active_skill`. No changes to `update_agent_triggers` or
`sync_agent_cache`'s payload shape.

### 5. `modules/agents/invoke_worker.py`

`_run_turn`'s persona selection branches on `builder_state` when
`is_config_mode` is true (via `get_builder_state_prompt`), instead of always
loading the fixed `agent_builder` persona. Execution-mode turns are unchanged.
`_TOOL_THINKING_LABELS` gains entries for the three new tool names.

### 6. `modules/agents/schemas.py`

`AgentOut.builder_state: str`, read-only (same convention as `active_skill` -
writable only via the agent's own tools, never `PATCH /agents/me`).

### 7. Frontend

No changes. `poc/composables/useAgentConfig.js` /
`poc/components/AgentChatView.js` already treat the config chat generically
(send via `send_message`, render whatever comes back, show `agent_thinking`
status) and do not branch on persona/mode client-side.

## Consequences

- Config-mode turns are no longer "always the same `agent_builder` persona" -
  they now branch three ways on `builder_state`, still gated identically by
  `is_config_mode`. Execution-mode turns are provably unaffected.
- One more `Agent` column, one more additive `ALTER TABLE` safety net,
  following the exact convention of every prior ADR 0046/0047 column
  addition.
- No new Redis cache surface, no new REST endpoints, no new tests convention
  introduced (matches every prior agents-module step - no test harness exists
  for this module yet).
- `personas.AGENT_BUILDER` becomes vestigial on the config-mode path
  specifically (superseded by the three sub-state prompts) but is left in
  place - not required to remove it, and ADR 0047's text still references it.

## Schema changes

```python
class Agent(Base):
    # ...existing columns from ADR 0045/0046/0047...
    builder_state: str   # "supervisor" | "builder_agent" | "help_agent", default "supervisor"
```
No new tables.
