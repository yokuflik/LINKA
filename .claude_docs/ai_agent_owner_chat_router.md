# Owner-chat jev router (ADR 0093)

Full rationale: `docs/adr/0093-owner-chat-jev-router-replaces-supervisor.md`.
Delivery history/phase log: `AGENT_OWNER_CHAT_ROUTER_PLAN.md` (all 4 code/doc
phases DONE 2026-09-29; Phase 5 - tuning against real usage - still open).

## What it replaces

Pre-ADR-0093, `Agent.builder_state` (ADR 0049) transitioned only when the
model itself called a `transfer_to_*` tool - a real bug class (the "mid-turn
handoff bug", `ai_agent_changelog_mid.md`) since it depended on the model
remembering to call the right tool mid-conversation. ADR 0093 removes every
`transfer_to_*` tool entirely and replaces model-self-directed transitions
with a router that decides `builder_state` deterministically before the
model ever sees the turn.

## The state set

`modules/agents/builder_flow.py::BuilderState`:

| State | Value | Role |
|---|---|---|
| `ONE_OFF_ACTION` | `"one_off_action"` | Default/idle state. Direct execution for the owner (send a message, look something up, schedule a one-time future action) - inherits the old ADR 0062 execution-tool union, now exclusively here. Also gets disposable-only trigger write access + deletion (ADR 0095: `update_own_triggers` with every entry required to carry `expires_at`/`max_fires`, plus `delete_own_trigger`) - a permanent trigger still requires `BUILDER`. |
| `CLARIFY` | `"clarify"` | Zero-action. Fires only when the router's top two candidates are `one_off_action`/`builder` and too close to call. Asks one disambiguating question, then the router decides again on the owner's next message. |
| `BUILDER` | `"builder_agent"` | Interviews the owner to configure persistent behavior (persona, rules, triggers, identity, knowledge base). Execution-tool union removed (ADR 0093) - config tools only. |
| `HELP_BUILDING` | `"help_agent_building"` | Unchanged from ADR 0064/0092 - static inline docs, zero tools but `no_reply_needed`. |
| `HELP_GENERAL` | `"help_general"` | Same as above, for general Linka platform questions. |

Default on agent creation and on `POST /agents/me/reset` (ADR 0050):
`one_off_action` (was `supervisor`).

Full per-state tool lists: the registry table in `.claude_docs/ai_agent.md`
("Config mode" section) - kept there since it lines up directly against the
execution-mode tool list.

## Router contract

`modules/agents/owner_chat_router.py::route_owner_turn(session, agent,
chat_id, recent_turns, new_message) -> RouterDecision`

- Called from `invoke_worker.py::_run_turn` for every config-mode turn that
  has a real triggering `message_id` (a schedule-fired or knowledge-notice
  turn has no owner utterance to classify and skips the router, keeping
  whatever `builder_state` is already set).
- `recent_turns`: the owner-agent chat's own last `AGENT_ROUTER_CONTEXT_TURNS`
  (default 3) lines, built by `invoke_worker.py::_format_router_recent_turns`
  (reuses `invoke_turn_helpers._format_history_transcript`'s
  `Agent:`/`Customer:` rendering), excluding the triggering message itself.
  Deliberately NOT the full chat history - `builder_state` already encodes
  long-running context (e.g. "mid Builder interview"), so a full transcript
  would just burn tokens without improving routing accuracy.
- Classification: one call to the TypeSafe `jev` backend
  (`modules/agents/typesafe_client.py::classify`, same backend as the LLM
  Judge, ADR 0076) with **four independent Noul (0-1 confidence) questions**,
  one per destination (`one_off_action`/`builder`/`help_building`/
  `help_general`) - mirrors `judge.py::_build_jev_questions`'s proven
  four-flag pattern rather than a single `choice`/`score` question (no
  precedent for that response shape anywhere in this codebase).
- `_resolve_margin` ranks the four scores in Python; if the top two are
  exactly `{one_off_action, builder}` and their margin is under
  `AGENT_ROUTER_CLARIFY_MARGIN` (default `0.25`), the decision is `CLARIFY`
  instead of the top scorer. No other destination pair ever triggers
  `clarify` - the ADR's own reasoning is that no other pair is a useful
  disambiguating question to put to the owner.
- **Fail-frozen, not fail-open-to-action**: on a rate-limit exhaustion or a
  `(TypeSafeError, KeyError, TypeError, ValueError)` classification failure,
  `route_owner_turn` returns the agent's *current* `builder_state` unchanged
  (`RouterDecision.failed_open=True`, `probabilities=None`, `margin=None`)
  rather than guessing - a routing failure must never silently grant
  `one_off_action`'s execution tools by default. Any other exception type
  still propagates (same discipline as `judge.py`).
- Every call - resolved, clarified, or fail-frozen - writes exactly one
  `AgentRouterLog` row.
- `invoke_worker.py` persists `RouterDecision.state` unconditionally (only
  writes `Agent.builder_state` via `update_agent_config` if it actually
  differs) - no separate success/failure branch needed at the call site,
  since `route_owner_turn` is fail-frozen by construction. A `CLARIFY`
  decision takes effect within the same turn (special-cased branch in
  `_run_turn` before the normal tool-calling loop): fetches the triggering
  message, calls `clarify.py::generate_clarify_question`, posts the result,
  returns - bypassing `get_tool_schemas_for_chat`/the round-trip loop
  entirely for this one state.

## `clarify`'s question text

Free-text, Gemini-authored (not a fixed template) - `modules/agents/clarify.py::
generate_clarify_question`, via the cheapest available tier
(`AGENT_CLARIFY_MODEL`, mirrors `AGENT_JUDGE_REDIRECT_MODEL`'s pattern).
Tool-free, history-free call: just the ambiguous message + the two candidate
destinations. Fixed English fallback (`LOCAL_CLARIFY_QUESTION`) on any
failure - mirrors `judge.py::_generate_redirect_message`'s own fallback
discipline.

## Config knobs (`config/agent_settings.py`, ADR 0019 pattern)

| Knob | Default | Purpose |
|---|---|---|
| `AGENT_ROUTER_CALLS_PER_MINUTE` / `AGENT_ROUTER_CALLS_WINDOW_SECONDS` | mirrors `agent_judge_calls` | Own rate bucket (`ratelimit:agent_router_calls:{agent_id}`) - never shares the Gemini-calls-per-minute budget. |
| `AGENT_ROUTER_CONTEXT_TURNS` | 3 | How many recent owner-chat turns feed the classification call. |
| `AGENT_ROUTER_CLARIFY_MARGIN` | 0.25 | Conservative starting point (per the ADR) - tune once real `AgentRouterLog` data exists (Phase 5). |
| `AGENT_CLARIFY_MODEL` | mirrors `AGENT_JUDGE_REDIRECT_MODEL` | Cheapest-tier model used only to phrase the `clarify` question. |

## `AgentRouterLog` (mirrors `AgentJudgeLog`)

`modules/agents/models.py` - picked up automatically by `scripts/init_db.py`'s
`Base.metadata.create_all` (no manual migration):

- `agent_id`, `chat_id`
- `previous_state` / `resolved_state` (both `BuilderState.value` strings)
- `probabilities` (JSONB, one float 0-1 per destination, `null` on
  fail-frozen)
- `margin` (nullable float)
- `failed_open` (bool)
- `created_at`

Use this table to answer, once real traffic exists: how often
`schedule_one_off_task`-shaped requests land in `clarify` (flagged in the ADR
as the likely hot spot for the one-off/persistent boundary, since its name
straddles the two), and whether the `clarify` question is actually resolving
ambiguity rather than annoying the owner on cases the router should have been
confident about.
