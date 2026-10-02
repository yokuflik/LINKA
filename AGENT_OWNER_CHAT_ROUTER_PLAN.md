# Owner-Chat jev Router — Delivery Plan

Prereq: **ADR 0093** carries the rationale/decision; this file is the
implementation plan (Core Rule 7). Needs user sign-off before each phase
(Core Rule 10 - backend change) since this touches the agent's core
dispatch path.

Not started. Work phase-by-phase; each phase should land, test green, and
get a green light before the next starts - this is not a one-pass rewrite.

---

## Phase 1 - Tool-table restructuring (no routing-mechanism change yet) — DONE (2026-09-29)

Landed as planned, with two confirmed calls: `save_knowledge_from_text` stays
under `one_off_action` (per the spec text below, not moved to Builder), and
`scripts/init_db.py` got an idempotent backfill (`UPDATE agents SET
builder_state = 'one_off_action' WHERE builder_state = 'supervisor'`, plus
the column-default repin) so an already-seeded dev-DB agent doesn't hit
`BuilderState('supervisor') -> ValueError` on its next owner-chat turn.

`modules/agents/builder_flow.py`: `BuilderState.SUPERVISOR` -> `ONE_OFF_ACTION`
(`"one_off_action"`), added `CLARIFY` (`"clarify"`); `SUPERVISOR_PROMPT` ->
`ONE_OFF_ACTION_PROMPT` (same body, transfer targets renamed), added a
minimal `CLARIFY_PROMPT`; `TRANSFER_TO_SUPERVISOR_SCHEMA` ->
`TRANSFER_TO_ONE_OFF_ACTION_SCHEMA` (`transfer_to_one_off_action`, temporary
stopgap per this phase's own instructions - deleted in Phase 3).
`modules/agents/tools/schemas.py` / `tools/builder_handoff.py`:
`BUILDER_STATE_TOOL_SCHEMAS`/`BUILDER_STATE_HANDLERS` reshaped exactly per
the Tool reassignment table in ADR 0093 - `one_off_action` keeps the ADR
0062 execution-tool union + `resolve_user`/`find_chat_by_name`/
`resume_paused_chat`/`spawn_ephemeral_task`/`save_knowledge_from_text`/
`schedule_one_off_task`/`no_reply_needed`; `clarify` = `{no_reply_needed}`
only; `builder_agent` dropped the execution-tool union entirely, keeping
only the 6 ADR 0047 config tools + `set_agent_identity` +
`update_own_triggers`/`resolve_user`/`find_chat_by_name`/`no_reply_needed`/
handoffs/`finish_building_agent`. `_tool_finish_building_agent` now sets
only `is_enabled=True` (+ `sync_agent_cache`) but still stopgap-self-
transitions to `ONE_OFF_ACTION` (removed again in Phase 3, per plan).
`Agent.builder_state` default -> `"one_off_action"` (`models.py`,
`scripts/init_db.py`).
Test fallout: `tests/modules/agents/tools/test_dispatch.py` +
`test_builder_flow.py` rewritten for the new state/tool shapes incl. new
`clarify` coverage; `tests/modules/agents/_factories.py`'s `make_agent`
default builder_state fixed (was hardcoded `"supervisor"`); 3 tests in
`test_run_turn_config_mode.py` renamed/updated (Supervisor -> one_off_action
naming, `BuilderState.SUPERVISOR` -> `ONE_OFF_ACTION` in an assertion). Also
fixed one stale docstring in `modules/agents/schemas.py` (`AgentOut.builder_state`
comment listed the old string values).

**Also landed in Phase 1** (per the "Decisions confirmed by user" section
below, added mid-phase): the `clarify` free-text/Gemini-authored decision is
fully implemented now, not deferred to Phase 3, even though nothing can
reach `BuilderState.CLARIFY` yet. New `config/agent_settings.py::AGENT_CLARIFY_MODEL`
(same minimal-call pattern as `AGENT_JUDGE_REDIRECT_MODEL`). New
`modules/agents/clarify.py::generate_clarify_question` (own module per
user's file-size call - `builder_flow.py` was already at 471 lines) -
mirrors `judge.py::_generate_redirect_message`: a tool-free, history-free
`generate_structured` call with a fixed English fallback
(`LOCAL_CLARIFY_QUESTION`) on any failure. `invoke_worker.py::_run_turn`
gets a new special-cased branch (before the normal contents-building/
round-trip loop): `config_mode_turn and message_id is not None and
BuilderState(agent.builder_state) == BuilderState.CLARIFY` fetches the
triggering message, calls `generate_clarify_question`, posts the result via
`_post_config_reply`, and returns - completely bypassing `get_tool_schemas_for_chat`/
`get_builder_state_prompt`/the tool-calling loop for this one state (wired
in now, per user's explicit go-ahead, rather than deferred to Phase 3
alongside `route_owner_turn` - dead code today since nothing sets
`builder_state="clarify"` yet). `BUILDER_STATE_PROMPTS[CLARIFY]` (a static
prompt, from the original Phase 1 spec bullet below) is now unused dead
code along this path but kept as the dict-completeness fallback
`get_builder_state_prompt`/tests still expect.
New `tests/modules/agents/test_clarify.py` (6 tests, isolated - mocks
`modules.agents.clarify.generate_structured`) + 2 new tests in
`test_run_turn_config_mode.py` (real-DB, mocks
`modules.agents.invoke_worker.generate_clarify_question`, asserts
`mock_gemini_turn` is never needed/hit).

**308 tests pass** (`tests/modules/agents/`). No `.claude_docs`/CLAUDE.md
updates (Phase 4), no merge/push.

Original Phase 1 spec, for reference:

Goal: land the new toolsets under the new state names, while dispatch is
still driven by handoff tools (so the blast radius is "which tools exist
where", not "how transitions happen").

- `modules/agents/builder_flow.py`: rename `BuilderState.SUPERVISOR` ->
  `BuilderState.ONE_OFF_ACTION` (value `"one_off_action"`), add
  `BuilderState.CLARIFY` (value `"clarify"`). Update
  `BUILDER_STATE_PROMPTS` accordingly - retire the Supervisor prompt,
  write a `ONE_OFF_ACTION_PROMPT` (default/idle framing + direct-action
  framing) and a minimal `CLARIFY_PROMPT`. **Decided:** the clarify
  question is Gemini-authored free text (not a fixed template), but via
  the cheapest available model (Flash-Lite tier, mirroring
  `AGENT_JUDGE_REDIRECT_MODEL`'s pattern - new `AGENT_CLARIFY_MODEL`
  config knob) with the smallest viable context: just the ambiguous
  message + the two candidate destinations, no real system-prompt
  instructions needed beyond "phrase one short disambiguating question
  asking whether this is a one-time thing or something to set up going
  forward."
- `modules/agents/tools/schemas.py`:
  - `BUILDER_STATE_TOOL_SCHEMAS[BuilderState.ONE_OFF_ACTION]` = the 15
    `TOOL_SCHEMAS` + `resolve_user`, `find_chat_by_name`,
    `resume_paused_chat`, `spawn_ephemeral_task`,
    `save_knowledge_from_text`, `schedule_one_off_task`, `no_reply_needed`.
  - `BUILDER_STATE_TOOL_SCHEMAS[BuilderState.BUILDER]` drops the `*TOOL_SCHEMAS`
    union entirely; keeps `set_agent_persona`, `update_agent_rules`,
    `set_agent_identity`, `set_trigger`, `get_agent_status`,
    `estimate_api_usage`, `update_own_triggers`, `resolve_user`,
    `find_chat_by_name`, `finish_building_agent`, `no_reply_needed`.
  - `BUILDER_STATE_TOOL_SCHEMAS[BuilderState.CLARIFY]` = `no_reply_needed`
    only.
  - Keep the `transfer_to_*` schemas **for now** (temporary
    `transfer_to_one_off_action` replacing `transfer_to_supervisor`) -
    Phase 1 does not remove the handoff mechanism, only reshapes what each
    state can do once it's active.
- `modules/agents/tools/builder_handoff.py`: mirror the same reassignment
  on the handler-dict side (`BUILDER_STATE_HANDLERS`); `finish_building_agent`
  handler changes now (this part is not deferrable - it currently writes
  `builder_state=SUPERVISOR` which no longer exists): make it set only
  `is_enabled=True` + `sync_agent_cache`, leaving `builder_state` as
  whatever it already was (will be `builder_agent` until Phase 3 lands the
  router, so add a **temporary** explicit `transfer_to_one_off_action`-style
  self-transition inside the handler as a stopgap, removed again in Phase 3).
- `Agent` model / ADR 0050 reset path: default `builder_state` ->
  `"one_off_action"`.
- Test fallout: `tests/modules/agents/tools/test_dispatch.py`,
  `tests/modules/agents/tools/test_builder_flow.py` - rename
  Supervisor-keyed expectations, update Builder's expected tool set (now
  ~11 tools instead of ~28), add `clarify` state coverage.

## Phase 2 - The jev router itself (standalone, not wired in yet) — DONE (2026-09-29)

Landed as planned. One resolved open call: the "exact jev question
wording/schema" item was drafted as **four independent Noul (0-1 confidence)
questions in one call** (one per destination: `one_off_action`/`builder`/
`help_building`/`help_general`), reusing the exact proven pattern
`judge.py::_build_jev_questions` already uses for its four flags - not a
single `choice`/`score`-type question, since that response shape has no
precedent anywhere in this codebase to build confidently against (`choice`/
`score` are only mentioned as valid types in `typesafe_client.py`'s own
docstring, never actually used). The four Noul scores are ranked in Python
(`_resolve_margin`) to pick the top destination and compute the ADR 0093
margin.

`modules/agents/owner_chat_router.py` (new): `route_owner_turn(session, agent,
chat_id, recent_turns, new_message) -> RouterDecision` (`state`,
`probabilities`, `margin`, `failed_open`). Clarify only applies to the
`{one_off_action, builder}` pair specifically (`_CLARIFY_PAIR`) - a close
margin between any other pair (e.g. `builder` vs `help_building`) just
resolves to the top scorer, per the ADR's own reasoning that no other pair is
a useful question to put to the owner. Fail-frozen (not fail-open-to-action)
on both a `TypeSafeError`/malformed-response classification failure and an
exhausted rate-limit budget - `route_owner_turn` returns the agent's current
`builder_state` unchanged (`probabilities=None`, `failed_open=True`) rather
than guessing; an unexpected exception type outside
`(TypeSafeError, KeyError, TypeError, ValueError)` still propagates, same
discipline as `judge.py`.

New config knobs (`config/agent_settings.py`, ADR 0019 pattern):
`AGENT_ROUTER_CALLS_PER_MINUTE`/`AGENT_ROUTER_CALLS_WINDOW_SECONDS` (own rate
bucket `agent_router_calls`, mirrors `agent_judge_calls`, never shares the
Gemini-calls-per-minute budget), `AGENT_ROUTER_CONTEXT_TURNS` (default 3, not
yet consumed by anything until Phase 3 wires in the actual recent-turns
extraction), `AGENT_ROUTER_CLARIFY_MARGIN` (default `0.25`, conservative
starting point per the plan - tune in Phase 5 against real `AgentRouterLog`
data).

New `AgentRouterLog` model (`modules/agents/models.py`, mirrors
`AgentJudgeLog`): `agent_id`, `chat_id`, `previous_state`, `resolved_state`,
`probabilities` (JSONB, null on fail-frozen), `margin` (nullable float),
`failed_open`, `created_at`. Picked up automatically by `scripts/init_db.py`'s
existing `Base.metadata.create_all` - no manual `CREATE TABLE`/`ALTER TABLE`
needed (this table has no need for the judge tables' later-added-column
backstop pattern).

Unit tests only, real ephemeral DB + real Redis (`db_session`/`redis_db`
fixtures) with `classify` mocked - same isolation posture as `test_judge.py`:
new `tests/modules/agents/test_owner_chat_router.py` (11 tests) covers
top-scorer-wins for each of the 4 destinations, the clarify-margin trigger
and its wide-margin/wrong-pair negatives, both fail-frozen paths (TypeSafeError
+ malformed response), the unexpected-exception-type passthrough, the
rate-limit-exhausted fail-frozen path, and that every call path writes exactly
one `AgentRouterLog` row. No `invoke_worker.py` wiring/tests yet (Phase 3).

**319 tests pass** (`tests/modules/agents/`, 308 carried over from Phase 1 +
11 new). No `.claude_docs`/CLAUDE.md updates (Phase 4), no merge/push.

Original Phase 2 spec, for reference:

Goal: build and unit-test the classification call in isolation.

- New `modules/agents/owner_chat_router.py`: `route_owner_turn(agent,
  recent_turns, new_message) -> RouterDecision` (destination + per-class
  probabilities + margin flag), calling `typesafe_client.py`'s jev
  contract (reuse the existing client, new question set: is this a
  request to do something now/once vs. set up how the agent should behave
  going forward vs. a question about building vs. a general help
  question).
- New config knobs (`config/` sub-module, ADR 0019 pattern):
  `AGENT_ROUTER_CLARIFY_MARGIN` (default TBD from real margin
  distribution - start conservative, e.g. wide margin, and narrow once
  data exists), `AGENT_ROUTER_CONTEXT_TURNS` (default 3).
  New rate bucket mirroring `agent_judge_calls` (own ceiling, never shares
  the Gemini-calls-per-minute budget - same principle as ADR 0053).
- New `AgentRouterLog` table (mirrors `AgentJudgeLog`) for offline review
  of routing decisions - needed to tune the clarify margin in Phase 5.
- Unit tests only in this phase - no `invoke_worker.py` wiring yet.

## Phase 3 - Wire the router in, delete the handoff mechanism — DONE (2026-09-29)

Landed as planned. `modules/agents/invoke_worker.py::_run_turn` now calls
`route_owner_turn(session, agent, chat_id, recent_turns, new_message)` for
every config-mode turn with a real `message_id` (a schedule/knowledge-notice
-fired turn has no owner utterance to classify and skips the router
entirely, keeping whatever `builder_state` is already set) - placed right
before the existing `BuilderState.CLARIFY` special-case branch, so a router
decision of `CLARIFY` takes effect within the same turn rather than needing
a second message. The router call is unconditional and always persists
whatever `RouterDecision.state` comes back (`update_agent_config` only if it
actually differs from the current value) - `route_owner_turn` itself is
fail-frozen by construction (returns the agent's current `builder_state`
unchanged on any internal failure, per Phase 2), so no separate
success/failure branch was needed at the call site. `recent_turns` is built
from the owner-agent chat's own history via a new
`_format_router_recent_turns` helper (reuses
`invoke_turn_helpers._format_history_transcript`'s per-line
`Agent:`/`Customer:` rendering, trimmed to `AGENT_ROUTER_CONTEXT_TURNS` via
`get_message_history(..., limit=...)`, excluding the triggering message
itself since that's passed separately as `new_message`).

Deleted entirely: `transfer_to_builder`, `transfer_to_help_building`,
`transfer_to_help_general`, `transfer_to_one_off_action`
(ex-`transfer_to_supervisor`) - schemas (`builder_flow.py`/`schemas.py`),
handlers (`builder_handoff.py`), and every `BUILDER_STATE_TOOL_SCHEMAS`/
`BUILDER_STATE_HANDLERS` dict entry. `builder_agent`/`help_agent_building`/
`help_general` are now purely zero-action beyond their own domain tools (no
transfer tool left anywhere) - the router alone decides where the owner's
next message goes. `_tool_finish_building_agent` reached its final form:
`is_enabled=True` + `sync_agent_cache` only, the Phase 1 stopgap
self-transition to `ONE_OFF_ACTION` is gone.

Prompt fallout (`builder_flow.py`): every prompt's handoff instructions were
rewritten to stop telling the model to call a transfer tool - `one_off_action`
lost its 4 routing bullets (now framed as "a router already decided this is
a DO-something message"), `builder_agent` lost its "Acting directly, without
leaving the interview"/"Leaving the interview early" sections (it no longer
has the execution-tool union either, per Phase 1, so those sections were
doubly stale) and its finishing-checklist help-handoff instruction, and both
Help prompts lost their 3 transfer bullets each - all replaced with a short
note that a router decides the next message's destination on its own.

Test fallout: `test_builder_flow.py` rewritten (removed all
`test_transfer_to_*`/`finish_building_agent`-with-`builder_state`-write
tests, added `test_no_transfer_tool_exists_anywhere` +
`test_finish_building_agent_sets_only_is_enabled` +
`test_resume_paused_chat_is_one_off_action_only`, updated every
`BUILDER_STATE_HANDLERS` set-equality test for the trimmed tool sets);
`test_dispatch.py` rewritten (removed transfer-tool-specific
allow/reject tests, added `test_no_transfer_tool_is_ever_allowed_anywhere`
parametrized across every `BuilderState`); `test_run_turn_config_mode.py`
gained 3 new integration tests (`route_owner_turn` called with the right
args and its decision persisted before the tool-calling loop runs; a
`CLARIFY` decision bypasses the loop in the same turn; a schedule-fired turn
never calls the router at all) and one rename (the old
`test_builder_to_one_off_action_handoff_mid_turn_via_finish_building_agent`
-> `test_finish_building_agent_enables_without_touching_builder_state`, since
there is no handoff to assert on anymore). Also fixed stale comments/
docstrings referencing the deleted tools in `models.py`, `schemas.py`,
`invoke_turn_helpers.py` (the `_TOOL_THINKING_LABELS` dict), and
`scripts/init_db.py`.

**795 tests pass** (full suite). No `.claude_docs`/CLAUDE.md updates (Phase
4), no merge/push.

Original Phase 3 spec, for reference:

Goal: the router becomes the sole way `builder_state` changes.

- `modules/agents/invoke_worker.py`: before building `tool_schemas`/
  `persona_prompt` for a config-mode turn, call `route_owner_turn(...)`,
  persist the returned `builder_state` (fail-frozen: keep the existing
  value on router error/timeout instead of guessing), then proceed as
  today with `get_tool_schemas_for_chat`/`get_builder_state_prompt` off
  the now-current state.
- Delete `transfer_to_builder`, `transfer_to_help_building`,
  `transfer_to_help_general`, `transfer_to_one_off_action`
  (ex-`transfer_to_supervisor`) - schemas, handlers, dict entries, the
  stopgap self-transition added in Phase 1's `finish_building_agent`.
- `finish_building_agent` reaches its final form: `is_enabled=True` +
  `sync_agent_cache` only, no `builder_state` write.
- Test fallout: every test that currently exercises a `transfer_to_*` tool
  call gets rewritten to instead assert on `route_owner_turn` output;
  `test_dispatch.py`/`test_builder_flow.py` updated again for the final
  state machine shape.

## Phase 4 - Docs — DONE (2026-09-29)

Landed as planned, docs-only (no `modules/agents/` code touched).

`.claude_docs/ai_agent.md`: the "Config mode" registry table's `supervisor`
row replaced with `one_off_action`/`clarify`/`builder_agent` rows reflecting
the final ADR 0093 tool reassignment (cross-checked directly against
`tools/schemas.py::BUILDER_STATE_TOOL_SCHEMAS` and `builder_flow.py::
BuilderState`, not just the ADR text); the old `transfer_to_supervisor`/
"every handoff tool must be invisible" ADR 0062 narrative paragraphs
condensed into one short "ADR 0093 retires the handoff mechanism" note
pointing at the new file below (kept the pre-0093 detail in
`ai_agent_changelog_early.md`/`ai_agent_changelog_mid.md` rather than
duplicating it); the "Selection is purely chat_id-then-builder_state-driven"
line's ADR 0049 citation corrected to ADR 0093; the trailing "ADR 0093 (not
yet implemented, PENDING)" note removed/folded in; title-line ADR list and
the file-index table both updated. Landed at 315 lines (over the nominal
~300 budget by 15 - the file already notes it "kept re-crossing" that
threshold; not worth a further split for 15 lines).

New `.claude_docs/ai_agent_owner_chat_router.md`: router contract
(`route_owner_turn` inputs/outputs, the four-Noul-question jev call, the
`_CLARIFY_PAIR` margin rule, fail-frozen policy), the final `BuilderState`
table, `clarify`'s Gemini-authored-question mechanism, all 4 config knobs
(cross-checked against `config/agent_settings.py`), and the full
`AgentRouterLog` column list with Phase-5 tuning guidance.

CLAUDE.md: ADR 0093 index row's description changed from "Not yet
implemented" to "Implemented ... (Phase 5 tuning still open)" (status column
was already `Accepted`); new index row added for
`.claude_docs/ai_agent_owner_chat_router.md`.

**305 tests pass** (`tests/modules/agents/`, re-run before starting this
phase to confirm Phases 1-3 are still green - no code changed in this
phase). No merge/push.

Original Phase 4 spec, for reference:

- `.claude_docs/ai_agent.md`: replace the Supervisor references in the
  execution-mode-vs-config-mode registry table with the new state set;
  point to this file + ADR 0093 for detail (keep under the ~300-line
  budget - split further if needed, this file already re-crossed the
  threshold once).
- `.claude_docs/ai_agent_judge_and_escalation.md` or a new
  `.claude_docs/ai_agent_owner_chat_router.md`: router contract, margin
  tuning notes, `AgentRouterLog` shape.
- CLAUDE.md ADR index: mark 0093 done once Phase 3 lands.

## Phase 5 - Tuning (after real usage)

- Pull `AgentRouterLog` data; check how often `schedule_one_off_task`
  requests land in `clarify` (flagged in the ADR as the likely hot spot
  for the one-off/persistent boundary) and adjust
  `AGENT_ROUTER_CLARIFY_MARGIN` accordingly.
- Confirm `clarify`'s single-question framing is actually resolving
  ambiguity rather than annoying the owner on cases the router should
  have been confident about.

## Phase 6 - Router refinements: stickiness + single-clarify cap — DONE (2026-09-29)

Prompted by a Gemini second-opinion review of this plan (2026-09-29), which
raised four blind spots relative to router designs in more mature agent
products. Two were adopted as concrete, scoped additions (6a/6b, both landed
below, same day, per explicit user go-ahead); one (6c, latency) was
evaluated and is explicitly **not** being pursued, for reasons specific to
this codebase's process topology - documented but not built; the fourth
(short context window/anaphora) was judged to already be covered by Phase
5's tuning pass and needs no separate design here.

**Landed:** `config/agent_settings.py::AGENT_ROUTER_STICKINESS_MARGIN`
(default `0.15`, same starting-conservative reasoning as
`AGENT_ROUTER_CLARIFY_MARGIN`). `AgentRouterLog.sticky` (new `Boolean`
column, `models.py` + `scripts/init_db.py` ALTER-table backstop for an
already-initialised dev DB). `owner_chat_router.py::route_owner_turn`'s
resolution logic rewritten per 6a/6b's design below exactly as specced - new
`_STATE_TO_DESTINATION` reverse map, `RouterDecision.sticky`, the
CLARIFY-cap check running first, then the stickiness check, then the
existing clarify-pair/top-scorer logic unchanged. `tests/modules/agents/test_owner_chat_router.py`:
7 new tests (4 for 6a incl. a margin-boundary case, 3 for 6b) - **all 11
pre-existing router tests still pass unmodified**, confirming the new checks
are additive and don't change any previously-specified behavior. **312
tests pass** (`tests/modules/agents/`).

**Explicitly deferred to Phase 5 per user instruction** (2026-09-29): both
new margins (`AGENT_ROUTER_STICKINESS_MARGIN`/`AGENT_ROUTER_CLARIFY_MARGIN`)
ship with their conservative starting defaults only - no tuning against real
usage happens now, since Phase 5 hasn't started (no real `AgentRouterLog`
data exists yet to tune against). Phase 5's own section below already covers
this; it is unchanged by Phase 6 landing.

6c (speculative router execution during the debounce wait) remains
evaluated-but-not-pursued, as originally written - no code changes for it.

Original Phase 6 design, for reference (as written before implementation -
matches what was actually built):

### 6a. Session stickiness / hysteresis

**The gap:** `route_owner_turn` re-classifies from scratch every turn off
`recent_turns` + `new_message`, with no bias toward the state the
conversation is already in. A short, low-signal owner message mid-`builder_agent`
interview ("wait, what did Dani just say?" / "change it to 10:00") can score
higher for `one_off_action` than for `builder`, yanking the owner out of an
in-progress interview because nothing in the scoring favors the state
already in force.

**Design:** a new post-processing step in `route_owner_turn`, applied
*before* the existing `_CLARIFY_PAIR` margin check (`owner_chat_router.py`,
around `_resolve_margin`'s call site) - not a new call, not new context, no
change to `_build_jev_questions` or the classification itself:

- New config knob `AGENT_ROUTER_STICKINESS_MARGIN` (`config/agent_settings.py`,
  same `float`/env-var pattern as `AGENT_ROUTER_CLARIFY_MARGIN`).
- `previous_state = BuilderState(agent.builder_state)` is already computed at
  the top of `route_owner_turn`. If `previous_state` maps to one of the 4 jev
  destinations (i.e. `previous_state != BuilderState.CLARIFY`) and
  `probabilities[previous_destination] >= top_prob - AGENT_ROUTER_STICKINESS_MARGIN`
  (the top destination doesn't clear the previous state's own score by more
  than the margin), resolve to `previous_state` and stop - the clarify-pair
  check below never runs, since nothing is actually being left.
- Only when the top destination beats the previous state's score by more
  than `AGENT_ROUTER_STICKINESS_MARGIN` does control pass to the existing
  `_CLARIFY_PAIR`/margin logic, now comparing among the states that
  genuinely won.
- Applies uniformly to all 4 real states (not special-cased to
  `builder_agent`) for symmetry, but in practice mostly matters for the 3
  multi-turn stateful ones (`builder_agent`/`help_building`/`help_general`) -
  `one_off_action` is inherently transient (control returns to fresh routing
  every subsequent turn regardless), and `clarify` is excluded by
  construction (see 6b - it is never a "previous state" the stickiness check
  runs against, since it has no corresponding jev destination/probability).
- `AgentRouterLog` gets one new nullable column, `sticky` (bool) - `True`
  when this turn's resolution was the stickiness override rather than the
  classifier's own top pick, so Phase 5 tuning can see how often stickiness
  is actually saving a state vs. how often it's masking a genuine topic
  change the owner wanted.
- Starting value for `AGENT_ROUTER_STICKINESS_MARGIN`: conservative, similar
  reasoning to `AGENT_ROUTER_CLARIFY_MARGIN`'s `0.25` starting point - wide
  enough to stop a marginal score flip from derailing an interview, narrow
  enough that a genuine, clearly-scored topic change still switches
  immediately. Exact value TBD from real `AgentRouterLog` data in Phase 5,
  same as the clarify margin.

### 6b. Single clarify-question cap with a deterministic fallback

**The gap:** nothing today stops `clarify -> clarify -> clarify` if the
owner's follow-up answer is itself ambiguous ("do whatever you think" / "not
sure, what's better?") - the router just re-evaluates the new (still vague)
message and can land back in the `_CLARIFY_PAIR` margin again.

**Design:** in `route_owner_turn`, before the existing `_CLARIFY_PAIR`
check: if `previous_state == BuilderState.CLARIFY`, the clarify branch is
disabled for this turn *unconditionally* (regardless of margin) - resolution
falls through to `_DESTINATION_TO_STATE[top_dest]`, i.e. whichever
destination the classifier scored highest, even if by a hair. This is
deliberately not hardcoded to always land on `one_off_action` - it stays
driven by the classifier's own (possibly weak) signal rather than a fixed
override, while still guaranteeing at most one clarify question per
ambiguous exchange. In the specific case the classifier is still genuinely
torn (near-50/50 on the `_CLARIFY_PAIR`), `one_off_action` and `builder` are
the only two candidates in play by construction, so the fallback lands on
one of exactly those two either way - both are reasonable, low-permanent-
-damage destinations for a single mis-route (one_off_action executes a
single reversible action; builder just starts/continues an interview the
owner can abandon on their very next message, since routing re-runs fresh
every turn).
- No new config knob needed - reuses the existing `_CLARIFY_PAIR`/`_resolve_margin`
  machinery, just gates it on `previous_state`.
- No interaction with 6a's stickiness step: `BuilderState.CLARIFY` is not
  one of the 4 states stickiness checks against (see 6a's last bullet), so
  this check runs independently, right where the `_CLARIFY_PAIR` check is
  today.
- `AgentRouterLog`: no new column needed - `previous_state="clarify"` +
  `resolved_state` != `"clarify"` in the existing log rows already lets
  Phase 5 tuning see every forced single-clarify fallback and confirm the
  cap is actually working.

### 6c. Speculative router execution during the debounce wait (evaluated, not pursued)

**The idea:** since `AGENT_INVOKE_DEBOUNCE_SECONDS` (2s default) is already
dead time the system spends waiting to see if another message coalesces
into the same turn (`invoke_debounce.py`/`_invoke_debounce_poll_loop`), run
`route_owner_turn` speculatively during that window instead of only inside
`_run_turn` after the debounce fires, so its ~200-500ms doesn't add to the
owner-visible latency on top of the real turn.

**Why this isn't being scoped for implementation**, per the user's own
"only if it's not complicated or risky - otherwise skip it" framing:

- **Process-boundary problem.** `arm_debounce` is called from
  `trigger_engine._evaluate_triggers`, which runs synchronously inside the
  message-persistence path in the **main app process**
  (`modules/messaging/send.py`'s docstring is explicit about this - "in
  parallel with the existing fan-out, never blocking it"). `route_owner_turn`
  needs a DB session, a real jev HTTP call, and writes to `AgentRouterLog` -
  firing that speculatively from the main request-handling process (rather
  than the dedicated `agent_worker` process/container `_run_turn` already
  runs in) reintroduces exactly the kind of latency/resource risk to the hot
  message-send path that the existing architecture goes out of its way to
  avoid elsewhere (ADR 0001's whole async fan-out design, ADR 0037's
  fire-and-forget receipts).
- **Staleness / cache-invalidation risk.** A burst of fast owner messages
  re-arms the same debounce member repeatedly (`arm_debounce` overwrites the
  stashed `message_id` and the ZSET score on every match). A speculative
  classification started on an earlier message in the burst would be
  answering the wrong question by the time the debounce actually fires on a
  later one - correctness-sensitive, since the router's decision gates which
  `BuilderState`/tool schema set the real turn runs under (a hard security
  boundary per ADR 0047/0093, not just a UX nicety). Making this safe needs
  a real cache-invalidation contract (only trust a speculative result if it
  was computed from the exact final coalesced `message_id`, else silently
  fall through to the synchronous call) plus care that `AgentRouterLog`
  still gets written exactly once per real turn, not once per speculative
  attempt.
- **Wasted calls on bursts.** Every message in a coalesced burst would burn
  its own `agent_router_calls` budget + a real jev call, even though only
  the last one's result is ever used - a real (if cheap) cost multiplier
  with no guaranteed latency win if the owner keeps typing past the window.

**If router latency is ever measured as a real problem** (Phase 5 data, not
a guess), the lower-risk place to revisit this is a same-process
optimization inside `agent_worker` itself once a pair is already due (e.g.
overlapping the router's jev call with some other already-in-flight,
independent I/O at the very top of `_run_turn`) - not reaching back across
the process boundary into the message-send hot path. No action item here
beyond this note for a future session to start from.

### Test fallout (once 6a/6b are implemented)

- `tests/modules/agents/test_owner_chat_router.py`: new cases for 6a
  (previous state within the stickiness margin of the top scorer is kept;
  previous state losing by more than the margin still switches; stickiness
  is bypassed entirely when `previous_state == CLARIFY`) and 6b (`previous_state
  == CLARIFY` never resolves to `CLARIFY` again regardless of margin; a
  second consecutive ambiguous message still produces exactly one
  `AgentRouterLog` row per turn, not two).
- `tests/modules/agents/test_run_turn_config_mode.py`: an integration case
  confirming a `builder_agent` interview survives a low-signal interrupting
  message (stays in `builder_agent`) and that a real topic change still
  switches states within the same conversation.

---

## Decisions (confirmed by user, 2026-09-29)

- `save_knowledge_from_text` placement: **`one_off_action`** (confirmed) -
  single-document commands are one-shot actions, not a multi-turn build
  interview.
- `clarify`'s question: **free-text, Gemini-authored** (confirmed) - but
  via the cheapest tier (Flash-Lite equivalent, `AGENT_CLARIFY_MODEL`),
  minimal context (ambiguous message + 2 candidate destinations only), no
  real system-prompt beyond a one-line instruction. See Phase 1 above.

## Open calls (remaining)

- (Resolved in Phase 2) Exact jev question wording/schema for the router
  itself: four independent Noul questions (one per destination), mirroring
  `judge.py`'s existing four-flag pattern - see Phase 2 above.
- None remaining as of Phase 2's completion.
