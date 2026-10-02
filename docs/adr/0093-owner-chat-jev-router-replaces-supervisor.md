# 0093 - Owner-chat jev router replaces the Supervisor persona

Status: Accepted (not yet implemented - see `AGENT_OWNER_CHAT_ROUTER_PLAN.md`)

## Context

Today, dispatch inside the owner-agent chat (config-mode, `chat_id ==
agent.owner_agent_chat_id`) is a single `builder_state` field
(`supervisor` / `builder_agent` / `help_agent_building` / `help_general`,
ADR 0049) that the **model itself** transitions via three handoff tools
(`transfer_to_builder`/`transfer_to_help_building`/`transfer_to_help_general`,
plus `transfer_to_supervisor` and `finish_building_agent`,
`modules/agents/tools/builder_handoff.py`). Two problems with this, both
raised by the user directly:

1. **Self-directed handoff has already caused a bug** (the "mid-turn
   handoff bug", `.claude_docs/ai_agent_changelog_mid.md`): the model has to
   remember, mid-conversation, to call the right transfer tool before
   continuing - a state transition that depends on the model's own
   attention rather than on a deterministic check.
2. **Supervisor carries a deliberate toolset-union exception.** ADR 0062
   gave Supervisor the full 15-tool execution-mode set unioned directly
   into its config-mode schema (`schemas.py:495`,
   `builder_handoff.py:112`) specifically so a direct owner request
   ("message X and tell me what they say") does not require a transfer
   round-trip first. This is the one deliberate break of ADR 0047's
   config/execution schema-overlap invariant - acceptable there only
   because the owner-agent chat is a trusted party (no prompt-injection
   surface), but still a wart: Builder inherits the same union
   (`schemas.py:501`) even though it needs almost none of those 15 tools.

Separately, the user wants first-class support for a request pattern that
today falls awkwardly between "one-off action" and "build a persistent
behavior": *"tomorrow at 12:00 send me a summary of what happened in the
group"* - a single, time-delayed action, not a request to recur daily.
`schedule_one_off_task` already exists for exactly this
(`config_mode.py:243-274`, backed by the `on_schedule` due-ZSET poll loop,
ADR 0046 decision 3) but today it only lives in Builder's toolset - so
reaching it requires the model to first decide this is a "building"
request and hand off, when conceptually it is a one-off action that
happens to fire later.

## Decision

Replace `builder_state`-as-model-driven-handoff with **`builder_state`-as-
router-driven dispatch**: a cheap jev classification call runs once before
every owner-chat turn and decides, deterministically, which persona
handles this specific message. The model inside that persona never
transitions state itself again - all four `transfer_to_*` tools and
`finish_building_agent`'s dual `builder_state`+`is_enabled` write are
removed (finish_building_agent keeps only the `is_enabled=True` +
`sync_agent_cache` half).

### New `BuilderState` set (replaces the 4 existing values)

| State | Role |
|---|---|
| `one_off_action` | **New.** Inherits Supervisor's execution role (the ADR 0062 union) and becomes the chat's default/idle destination. Immediate or time-delayed (`schedule_one_off_task`) single actions live here. |
| `clarify` | **New.** Zero-action. Fires only when the router's confidence between `one_off_action` and `builder` is inside a margin (ambiguous: "do this once" vs "set this up to keep happening"). Asks one disambiguating question, then hands back to the router on the owner's next message. |
| `builder_agent` | Kept, **toolset trimmed**: the ADR 0062 execution-tool union is removed. Owns persistent configuration only (persona, rules, identity, triggers, knowledge base). |
| `help_agent_building` / `help_general` | Unchanged (ADR 0092 static inline docs, zero-action). |
| ~~`supervisor`~~ | **Removed.** Its two roles split across `one_off_action` (direct execution) and the router itself (dispatch hub). |

### Router contract

- Input: current `builder_state`, the last 2-3 turns of the owner-agent
  chat (**not** the full history - `builder_state` already encodes
  long-running context, e.g. "mid Builder interview"; a full transcript
  would burn tokens on every owner message without improving accuracy),
  and the new message.
- Output: a probability per destination (`one_off_action` / `builder` /
  `help_building` / `help_general`).
- If the top two candidates are `one_off_action` and `builder` and their
  margin is under `AGENT_ROUTER_CLARIFY_MARGIN` (new config knob) -> route
  to `clarify` instead of guessing.
- **Fail-open policy is "fail-frozen", not "fail-open-to-action"**: if the
  jev call errors, keep the last persisted `builder_state` rather than
  defaulting to `one_off_action` - a routing failure must never silently
  grant action tools by default.

### Tool reassignment

| Tool | Today | After this ADR |
|---|---|---|
| `send_message`, `reply_message`, `create_chat`, `leave_group`, `read_history`, `count_messages_in_range`, `bulk_fetch_messages`, `search_messages`, `search_semantic`, `search_knowledge_semantic`, `get_knowledge_index`, `fetch_chunk`, `list_attached_files`, `send_attached_file`, `pause_and_escalate` | Supervisor + Builder (both via ADR 0062 union) + all 4 execution personas | `one_off_action` (+ still all 4 execution personas, unchanged) - **removed from `builder_agent`** |
| `resolve_user`, `find_chat_by_name` | Supervisor + Builder | Both `one_off_action` and `builder_agent` (Builder still needs them to target `set_trigger`'s `on_specific_chats` at a resolved chat) |
| `resume_paused_chat`, `spawn_ephemeral_task`, `save_knowledge_from_text` | Supervisor + Builder | `one_off_action` only - these are one-shot actions, not persistent config |
| `schedule_one_off_task` | Builder only | **Moved to `one_off_action`** - it schedules a single future action, not a recurring behavior (the motivating case from Context) |
| `set_agent_persona`, `update_agent_rules`, `set_agent_identity`, `set_trigger`, `get_agent_status`, `estimate_api_usage`, `update_own_triggers` | Builder (+ Supervisor for `update_own_triggers`) | `builder_agent` only |
| `finish_building_agent` | Builder; sets `builder_state=supervisor` + `is_enabled=True` | `builder_agent`; sets **only** `is_enabled=True` + `sync_agent_cache` - the next router pass decides state |
| `transfer_to_builder`, `transfer_to_help_building`, `transfer_to_help_general`, `transfer_to_supervisor` | Cross-wired across states | **Deleted entirely** |
| `no_reply_needed` | All 4 states | All 5 states (including new `clarify`, as its only tool besides asking the question) |

## Consequences

- Supervisor's prompt/persona text is retired; `one_off_action` absorbs
  its "default assistant" framing plus the ADR 0062 action union.
- No more model-self-directed state transitions anywhere in config-mode -
  closes the mid-turn-handoff bug class structurally, not by patching the
  specific bug.
- Builder's toolset shrinks from ~28 tools (13 config + 15 unioned
  execution) to ~9, matching what it conceptually needs.
- `schedule_one_off_task` living under `one_off_action` resolves the
  motivating "remind me tomorrow at noon" case directly, but is also the
  most likely tool to keep tripping the `clarify` margin in practice
  (its name literally straddles the one-off/persistent line) - flagged in
  the plan as the first thing to tune once real routing data exists.
- New failure mode: router unavailability now blocks *all* owner-chat
  progress (nothing routes) rather than degrading gracefully within a
  fixed persona - mitigated by fail-frozen (keep last state) rather than
  fail-open, but still a new dependency on every owner-chat turn.
- `Agent.builder_state` default (new-agent creation, ADR 0050 reset) moves
  from `supervisor` to `one_off_action`.
- This is a large, multi-file change (tool tables, dispatch, prompts,
  schema default, tests) - delivered in phases per
  `AGENT_OWNER_CHAT_ROUTER_PLAN.md`, not in one pass.
