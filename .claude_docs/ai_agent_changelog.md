# AI Agent - changelog (no-ADR fixes, prompt tuning, in-scope UX changes)

Split out of `.claude_docs/ai_agent.md` on 2026-09-26 (file exceeded the
~300-line CLAUDE.md threshold again). This file holds the chronological log
of implementation-detail entries that are NOT their own ADR - bug fixes,
prompt-only tone changes, small in-scope UX additions. For ADR-backed
features (schema/tool-registry/architecture changes), see `ai_agent.md`'s
own per-ADR sections, `ai_agent_judge_and_escalation.md`, and
`ai_agent_capacity_and_budgets.md`. Ordered oldest to newest as originally
written.

**`modules/agents/tools.py` split into a package (ADR 0056, 2026-09-25):**
`modules/agents/tools/` now holds `common.py` (identity masking, quota
check, call logging), `execution.py` (execution-mode tool handlers),
`config_mode.py` (config-mode tool handlers), `builder_handoff.py` (ADR 0049
handoff tools), `schemas.py` (`TOOL_SCHEMAS`/`CONFIG_TOOL_SCHEMAS`/
`BUILDER_STATE_TOOL_SCHEMAS`), and `dispatch.py` (`is_config_mode`/
`get_tool_schemas_for_chat`/`execute_tool_call` - the hard tool-mode gate).
`__init__.py` is a thin facade; `invoke_worker.py`'s `from modules.agents.tools
import execute_tool_call, get_tool_schemas_for_chat, is_config_mode` is
unchanged. Pure structural split, no behaviour change.

**Real peer-visible typing indicator DONE 2026-09-25** (no ADR - in-scope UX
fix, not a new architectural decision): `invoke_worker.py::_run_turn` now
starts a `_publish_peer_typing_loop` background task whenever a turn is
execution-mode against a real chat (`chat_id is not None and not
is_config_mode(agent, chat_id)`) - it calls the same `realtime_service.
publish_event(chat_id, {event:"typing", user_id, kind:"typing"})` a genuine
user's WS `typing` frame goes through, re-published every 3s
(`_PEER_TYPING_REFRESH_SECONDS`, matching the client's own
`TYPING_SEND_THROTTLE_MS`/`TYPING_EXPIRY_MS` in `useTyping.js`) for the
lifetime of the turn, cancelled in the `finally` right before the reply is
posted. This is distinct from the pre-existing `agent_thinking` event below:
`agent_thinking` is a private, owner-only signal for the agent drawer;
the chat's other participants now see an ordinary "X is typing…" indicator
like they would from a human, with no other UI surfaced to them. Never fired
for config-mode turns (the owner's own drawer chat, which already has
`agent_thinking`) or schedule-fired turns with `chat_id=None`.

**`agent_thinking` no longer leaks into the drawer during execution-mode
turns (2026-09-25, no ADR - in-scope bug fix)**: all three
`_publish_agent_thinking` call sites in `_run_turn` (`"started"`, each
`"tool_call"`, and the final `done`/`error` in the `finally`) are now gated
behind `config_mode_turn = chat_id is None or is_config_mode(agent,
chat_id)`. Previously they fired unconditionally, so the owner's drawer would
show a live "thinking…" status even while the agent was mid-turn replying to
an unrelated third-party chat - reported by the user as confusing (looks like
the owner's own conversation with the agent is doing something, when it's
actually a customer's chat being served). `agent_thinking` is scoped to
config-mode turns (the owner's own drawer chat) only, matching its doc'd
"private, owner-only signal for the agent drawer" contract - the peer-visible
`typing` event above (already chat-scoped, not owner-drawer-scoped) is
unaffected and remains the correct signal for execution-mode turns. No
schema/API change, import-smoke-tested only (same gap as every prior
agents-module step).

**Peer-visible typing indicator's `user_id` was an unserialized int, breaking
every strict identity check downstream (2026-09-25, real bug, no ADR)**:
`_publish_peer_typing_loop` sent `"user_id": sender_id` as a raw Python
`int`. Every other id on the wire in this codebase is a string (Snowflake-
id-as-string convention, `.claude_docs/database_schema.md`) - e.g. a genuine
user's `typing` frame is stringified in Rust
(`crates/ws_gateway/src/handlers.rs`: `user_id.to_string()`), and
`new_message`'s `sender_id` is `str(...)`'d in `modules/messaging/send.py`.
The Rust gateway forwards a Python-originated `instance_inbox` payload
byte-for-byte with no reserialization (`crates/ws_gateway/src/fanin.rs`), so
this one event type alone reached the browser with a JSON *number* instead
of a string. The PoC frontend compares ids with strict `===` throughout
(`poc/composables/useWsRouter.js`), so this single-field type mismatch
silently broke both of the below - fixed with one `str(sender_id)` in
`_publish_peer_typing_loop`:
- The customer-visible indicator never resolved to the owner's real name
  (fell through to `userLabelById`'s no-user-cached fallback every time) -
  this is the actual root cause of what looked like a frontend name-
  resolution bug; that earlier `useWsRouter.js`/`userLabelById` hardening
  was real defense-in-depth but not what was causing the reported symptom.
- The indicator could outlive the reply and get stuck: `new_message`'s
  `clearUserTyping(chat_id, sender_id)` (string) never matched the
  number-keyed entry `noteUserTyping` had stored, so the entry only ever
  cleared via its own 5s client-side expiry instead of immediately.

Also hardened the stop timing itself: the peer-typing loop is now cancelled
right after `execute_tool_call` returns for `send_message`/`reply_message`
(new `_MESSAGE_SENDING_TOOL_NAMES` check), not only in the turn's outer
`finally` - previously a 3s-interval tick still in flight could re-publish
`typing` after the reply had already landed (the turn may run more
round-trips afterward), re-arming a phantom indicator with nothing left to
clear it early.

Full detail in `.claude_docs/ai_agent_frontend.md`.

**ADR 0049 DONE 2026-09-24**: config-mode turns (`chat_id ==
owner_agent_chat_id`) no longer always run the single `agent_builder`
persona - they now branch three ways on new `Agent.builder_state`
(`"supervisor"` default | `"builder_agent"` | `"help_agent"`), each with its
own system prompt (`modules/agents/builder_flow.py::BUILDER_STATE_PROMPTS`)
and its own disjoint tool schema set. Handoffs are three new tools -
`transfer_to_builder`, `transfer_to_help`, `finish_building_agent` - dispatched
through the exact same `update_agent_config` write path every other config
tool uses (no new mutation mechanism). Supervisor and Help states expose only
`transfer_to_builder`; the Builder state exposes all 6 pre-existing ADR 0047
config tools (unchanged, saves incrementally during the interview - no
bulk-config-apply path was added) plus `transfer_to_help` and
`finish_building_agent`. `finish_building_agent` resets `builder_state` back
to `"supervisor"` AND auto-enables the agent (`is_enabled = True`, confirmed
with the user) - the one handoff tool that calls `sync_agent_cache` (the
other two don't touch anything the trigger pre-filter cache tracks).
ADR 0047 decision 4's outer gate (`is_config_mode`, execution vs. config by
`chat_id`) is completely untouched - execution-mode chats never see
`builder_state` or any of the three new tools. `get_tool_schemas_for_chat`
and `execute_tool_call` both extend their existing config-mode branch into a
3-way dispatch on `BuilderState(agent.builder_state)`; the defense-in-depth
allowlist recheck in `execute_tool_call` is preserved exactly, just per-state
instead of one flat config allowlist. `AgentOut.builder_state` exposed
read-only (same convention as `active_skill` - written only by the agent's
own tools, never `PATCH /agents/me`). No frontend changes - confirmed
`AgentChatView.js`/`useAgentConfig.js` are already mode-agnostic (send via
`send_message`, render whatever comes back). No Redis cache changes beyond
the one `sync_agent_cache` call noted above - `builder_state` itself was
deliberately kept out of the `agent:trigger_cfg` cache payload (that cache
exists for cross-owner trigger pre-filtering; `builder_state` is only ever
read inside the owner's own config-chat turn, which already re-fetches the
row fresh). No new tests (same gap as every prior agents-module step) -
import-smoke-tested + manually traced all three states' tool-schema
selection.

**ADR 0047, ALL DECISIONS (1-7) DONE 2026-09-23** - see the ADR's own
Implementation log for full detail:
- Decision 1: `AGENT_GEMINI_CALLS_PER_MINUTE` 5→30, `AGENT_TURN_MAX_TOOL_
  ROUNDTRIPS` 4→8 (`config/agent_settings.py`) - cost/abuse ceiling now that
  Linka is on the paid Gemini tier, not shared-quota protection. Turn
  timeout (20s) and worker concurrency semaphore unchanged.
- Decision 2: `Agent.is_enabled` default `True→False` - new agents start
  dormant until the owner explicitly enables them. New rows only (no
  backfill); `scripts/init_db.py` ALTER safety net for deployed DBs.
- Decision 3: new `Agent.active_skill` column (default `one_off_executor`)
  + `modules/agents/personas.py::PERSONA_SYSTEM_PROMPTS` (5-skill catalog:
  `agent_builder`/`sales_agent`/`support_agent`/`summarizer`/
  `one_off_executor`). `agent_builder` is never stored as `active_skill` -
  it's structurally in force whenever the triggering chat is
  `owner_agent_chat_id` (decision 4). Writable via the `set_agent_persona`
  config tool (decision 6) - no direct PATCH support (deliberate: the model
  validates against the fixed catalog before storing).
- Decision 4 (hard tool-mode gate): `modules/agents/tools/dispatch.py::
  is_config_mode(agent, chat_id)` is the sole gate decision point
  (`chat_id == agent.owner_agent_chat_id`; `chat_id=None` i.e. a
  schedule-fired turn is never config mode). `get_tool_schemas_for_chat`
  picks `TOOL_SCHEMAS` (execution) vs `CONFIG_TOOL_SCHEMAS` (config, now
  populated by decision 6 - see below).
  `execute_tool_call(..., chat_id=)` independently re-derives the
  mode-appropriate allowlist before dispatch - defense in depth, same
  pattern as the `is_enabled` dequeue recheck. `invoke_worker.py::_run_turn`
  threads `chat_id` through to both schema selection and tool execution,
  and picks the system-prompt skill the same way (config-mode always
  `agent_builder`; otherwise the owner's `active_skill`), prepended to
  `Agent.system_prompt`.
- Decision 5 (Agentic RAG + escalation): `modules/agents/tools/`
  execution registry has `get_knowledge_index()` (every chunk as
  `{document_id, filename, chunk_id, excerpt}`, capped by
  `AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT`), `fetch_chunk(chunk_id)` (full
  content, agent_id-scoped), and `pause_and_escalate(reason?)`. The old
  `search_knowledge` tool is gone from the registry/schemas (superseded -
  `search_knowledge_chunks` stays in `crud.py`, just unused by any tool
  now). New `Agent.paused_chat_ids` (JSONB list) + `modules/agents/
  crud.py::pause_agent_chat`/`resume_agent_chat`. `pause_and_escalate`
  pauses the turn's own triggering `chat_id` (never a model-supplied
  argument - threaded through `execute_tool_call` via a small
  `_CHAT_SCOPED_TOOL_NAMES` special-case, since this is the one tool whose
  handler needs more than `(session, agent, arguments)`) and notifies the
  owner via the existing `realtime.notification_service.send_push`.
  `agent:trigger_cfg:{owner_user_id}` (the ADR 0046 cache) now also carries
  `active_skill`/`paused_chat_ids` - "cached runtime config," same key,
  same invalidation discipline. `trigger_engine.py` skips a paused chat at
  both the cheap cached-config check and the full-row recheck right before
  enqueue (same defense-in-depth spot as `blocked_read_chat_ids`). New
  `POST /agents/me/resume-chat/{chat_id}` - human-only, no tool lets the
  agent un-pause itself. `get_agent_status` (decision 6) and `AgentOut`
  both expose `active_skill`/`paused_chat_ids`. Full detail on the current
  pause shape/expiry in `ai_agent.md`'s Schema section (ADR 0054/0055).
- Decision 6 (config-mode tools): `_CONFIG_TOOL_HANDLERS` in `tools/`
  holds all six - `set_agent_persona` (validates against
  `personas.STORABLE_SKILLS`), `update_agent_rules`, `set_trigger` (same
  shape/merge as the execution-mode `update_own_triggers`, gated to
  config-mode chats only), `get_agent_status`, `estimate_api_usage`
  (static lookup table, informational only, no DB write, no rate-limit
  tie-in), `schedule_one_off_task` (builds a `kind: "once"` entry via the
  existing `update_agent_triggers` + `sync_schedule_zset` path - reuses ADR
  0046's schedule mechanism, not a new one). All six route through
  `modules/agents/crud.py::update_agent_config`/`update_agent_triggers`,
  the same write path `PATCH /agents/me` uses (`update_agent_config` gained
  `active_skill` handling for `set_agent_persona`). `CONFIG_TOOL_SCHEMAS`
  now holds these six Gemini function declarations - a config-mode turn
  (`chat_id == owner_agent_chat_id`) has real tools for the first time.
- Decision 7 (standing-context correction): confirmed moot - neither
  `_build_initial_contents` nor `_build_schedule_contents` in
  `invoke_worker.py` ever injected `owner_agent_chat_id` history
  unconditionally (that idea was floated during ADR 0046's design but never
  built), so there was nothing to correct in code. Recorded so a future
  reader doesn't reintroduce the leak the ADR warned about.
- No new tests (same gap as every prior agents-module step). Full suite:
  427/430 (3 pre-existing flaky timeout failures, confirmed unrelated).

**`one_off_executor` persona now defaults to natural conversation (2026-09-24,
no ADR - prompt-only change)**: `modules/agents/personas.py::
PERSONA_SYSTEM_PROMPTS[ONE_OFF_EXECUTOR]` (the default `active_skill` for new
agents) now instructs the model to answer casual chat/general-knowledge
messages the way any conversational assistant would, staying in character as
"Linka Agent" - not opening with "I'm your personal Linka agent" every turn.
It only explains its role as the owner's personal agent when directly asked
what it is/does. Applies only to `one_off_executor`; the other four personas
(`sales_agent`/`support_agent`/`summarizer`/`agent_builder`) are unchanged.

**Self-triggering loop fixed (2026-09-24, no ADR - real bug)**:
`trigger_engine.py::_evaluate_triggers` now also skips `AGENT_REPLY_MESSAGE_TYPE`
messages, not just `SYSTEM_MESSAGE_TYPE`. An agent's own reply carries
`sender_id=owner_user_id` (no user_id of its own, ADR 0045), so it was
indistinguishable by sender from a real owner-authored message - the
owner-chat direct-wake branch (`owner_agent.owner_user_id ==
message.sender_id`) treated every agent reply as a fresh deliberate wake and
re-invoked the agent on itself, one Gemini turn per prior turn's own reply.
Caught via a real symptom: one user message produced two full Gemini turns
(only stopped because the second happened to return empty text, not because
anything broke the chain) - a case that could have continued turn after turn
with no upper bound beyond the hourly activation quota. This check lives
above the owner-chat branch, so it protects execution mode the same way
(an agent's `send_message`/`reply_message` into any chat never wakes another
participant's agent off its own send, `AGENT_REPLY_MESSAGE_TYPE` there too).

**Config-mode plain-text replies now actually reach the chat (2026-09-24, no
ADR - in-scope bug fix in ADR 0049's implementation)**: Supervisor/Builder/
Help (`modules/agents/builder_flow.py`) have no `send_message`-shaped tool -
their prompts just instruct the model to reply in plain text, and
`invoke_worker.py::_run_turn`'s `call is None` branch (turn ends with text,
no function call) used to only log that text and return - never posting it
anywhere. Since config mode is the *only* mode that can end a turn this way
by design (execution-mode personas are expected to call `send_message`/
`reply_message` themselves), this meant every Supervisor/Builder/Help
response was silently generated and discarded - from the drawer it looked
exactly like "the agent thought for a moment and then said nothing," with no
error anywhere. Fixed with a new `_post_config_reply` helper that posts the
turn's final text via the same `process_outgoing`/`AGENT_REPLY_MESSAGE_TYPE`
path `_tool_send_message` uses, called only when `chat_id is not None and
is_config_mode(agent, chat_id)` - execution-mode and schedule-fired
(`chat_id=None`) turns are unaffected.

**Gemini model bumped `gemini-2.0-flash` → `gemini-flash-latest` (2026-09-24,
no ADR - vendor deprecation)**: `modules/agents/gemini_client.py::
GEMINI_CHAT_MODEL`. Google retired `gemini-2.0-flash` (every call started
404'ing with "This model ... is no longer available"), which is what made
every agent turn silently fail locally with no user-visible error - the
worker logs it and abandons the turn (`_run_turn` returns early on
`GeminiChatError`), so from the chat UI it just looks like the agent never
responded. Verified both `?key=` query param and the model name against the
live API by hand before landing this. `modules/vector_search/gemini_client.py`
(ADR 0042, embeddings) is a separate model/endpoint, unaffected.

**Reset to default (ADR 0050, 2026-09-24)**: new `POST /agents/me/reset`
(`modules/agents/router.py`) - owner-only, no body, executes unconditionally
(confirmation is frontend-only, `window.confirm` in `useAgentConfig.js::
resetAgentToDefault`, gated behind a new reset button in `AgentDrawer.js`'s
header, visible only in the settings view). Orchestrated by
`modules/agents/reset.py::reset_agent_to_default`: hard-purges every message
in `owner_agent_chat_id` (new `_purge_all_chat_messages` - not sender-scoped
and doesn't require a prior soft-delete, unlike ADR 0021's `purge_message`;
soft-deletes then purges each row, deref'ing/deleting S3 media on last ref
via the same `modules.media.crud.deref_blob`/`media_service.delete_object`
calls the single-message path uses), deletes the whole knowledge base
(`AgentKnowledgeDocument`/`Chunk`, cascaded), and resets `system_prompt`→`""`,
`triggers`→`DEFAULT_AGENT_TRIGGERS`, `active_skill`→`DEFAULT_AGENT_
ACTIVE_SKILL`, `builder_state`→`DEFAULT_AGENT_BUILDER_STATE`,
`paused_chat_ids`→`[]`, `encrypted_gemini_api_key`→`NULL`, `restrictions`→
`DEFAULT_AGENT_RESTRICTIONS`. `is_enabled` and `owner_agent_chat_id` itself
are deliberately left untouched (see the ADR). Re-sends the opening greeting
into the now-empty chat afterward, same `process_outgoing` path as agent
creation. Same cache/event side effects as `PATCH /agents/me`
(`sync_agent_cache` + `agent_config_changed` + `sync_schedule_zset`).

**Unknown-sender chats now auto-register into `on_specific_chats` (ADR 0051,
2026-09-25)**: previously `on_unknown_sender` fired exactly once per private
chat and then went silent for every later message from that same person -
surprising for a `sales_agent`-style deployment (one reply to a new lead,
then nothing). `trigger_engine.py::_evaluate_triggers` now calls the new
`modules/agents/crud.py::auto_register_unknown_sender_chat` right after the
per-sender daily quota check passes (i.e. only once the turn is actually
enqueued) - merges `chat_id` into `Agent.triggers.on_specific_chats` with
empty keywords (wake on any message) and an `_auto_added_at` UTC ISO
timestamp, then calls `sync_agent_cache` so the Redis pre-filter cache picks
it up immediately (same pattern as every other trigger-affecting write).
Registration is **permanent** (no expiry) until the owner removes it
manually. Capped at `AGENT_MAX_AUTO_CHATS` (200, `config/agent_settings.py`)
- only entries carrying `_auto_added_at` count against the cap or are
eligible for eviction; on overflow the oldest `_auto_added_at` entries are
evicted first (FIFO). Manually-added `on_specific_chats` entries (owner-set,
or via `update_own_triggers`/`set_trigger` without going through this path)
never carry `_auto_added_at` and are immune to both the cap and eviction.
Idempotent - re-firing for an already-registered chat is a no-op (keeps the
original timestamp, does not bump it to the back of the FIFO queue). No
frontend change needed - the auto-added chat just appears as a normal
`on_specific_chats` entry, subject to the existing "no chat picker" known
gap. No new tests (same gap as every prior agents-module step) -
import-smoke-tested only.

**Supervisor can hand off to Help directly (2026-09-25, no ADR - prompt +
tool-set fix)**: `modules/agents/builder_flow.py::SUPERVISOR_PROMPT` +
`modules/agents/tools/` now give the `supervisor` builder-state its own
`transfer_to_help` tool (alongside the existing `transfer_to_builder`),
instead of forcing every "what is this / how does this work" question
through the Builder first. Requested by the user so a brand-new owner who
opens the drawer (lazy `Agent` create-if-missing, lands on `supervisor` per
ADR 0045/0047) and just wants an explanation isn't detoured into the
Builder's interview flow before reaching Help. Scope confirmed with the
user: Help Agent stays config-chat-only (`owner_agent_chat_id`) - an
execution-mode end-user talking to someone else's deployed agent never gets
routed to Help; only the agent's own owner does, in their own config chat.
No schema/architecture change (ADR 0049's `BuilderState`/dispatch-table
shape is unchanged, just `SUPERVISOR`'s handler dict and schema list grew
by one entry each) - same class of change as the other no-ADR prompt-only
fixes already logged in this file. Import-smoke-tested only, same gap as
every prior agents-module step.

**Standing obligation: keep the Help Agent's answers accurate as the agent
system evolves (2026-09-25, process note, not code)**: the Help Agent
(`modules/agents/builder_flow.py::HELP_PROMPT`) explains "how the system
works" purely from its static system prompt - it has no tool that reads
live code or docs. Per the user's explicit request, whenever a change in
this session touches the agent system's user-facing behavior (new/changed
tools, triggers, skills, restrictions, rate limits, or flow), the same task
must also check whether `HELP_PROMPT` needs a matching update so its
explanations don't go stale, the same way this file and the ADR index are
kept current. Not automated - a discipline for this assistant to apply
every time the agents module changes, alongside the existing
`.claude_docs/` auto-maintenance rule in the root `CLAUDE.md`.

**History transcript now uses role labels + a char cap (2026-09-25, no ADR -
prompt quality + cost guardrail)**: `invoke_worker.py::_format_history_
transcript` (new, shared by `_build_initial_contents` and
`_build_schedule_contents`) replaces the old `[timestamp] sender=<id>:
<content>` lines with `Agent: ...` / `Customer: ...` (role decided by
`m.type == AGENT_REPLY_MESSAGE_TYPE`, not sender_id) - reads far better to
Gemini than a raw numeric id, matches how a human would paste a chat log.
Also truncates the joined transcript to `AGENT_HISTORY_TRANSCRIPT_MAX_CHARS`
(`config/agent_settings.py`, default 4000) by keeping only the tail (most
recent context wins, oldest lines dropped first) - the 20-message window
itself has no size bound, so a burst of long messages could otherwise blow
up prompt size/cost. Timestamps dropped from the transcript (were unused by
the model in practice); the window size (`limit=20`, still a hardcoded
literal, not in `config/agent_settings.py`) is unchanged.

**Config-mode handoff now takes effect within the same turn (2026-09-25, no
ADR - in-scope bug fix in ADR 0049's implementation)**: `invoke_worker.py`'s
round-trip loop used to compute `system_prompt`/`tool_schemas` once, before
the loop, from the `builder_state` at turn start. A handoff tool
(`transfer_to_builder`/`transfer_to_help`/`finish_building_agent`) flips
`agent.builder_state` mid-turn, but the *next* Gemini call in that same turn
kept running under the OLD state's prompt and (critically) its OLD
tool_schemas - it could see the tool result confirming the transfer but had
no way to actually act as the new state, so it just emitted a generic
"you've been handed off to the builder" line and stopped; the user's actual
request (e.g. "I want an agent that sells iPhones") sat unanswered until
their next message, and the Supervisor's canned handoff response looked like
it had ignored what they'd just said. Fixed by moving the
`get_tool_schemas_for_chat`/`get_builder_state_prompt`/`get_persona_system_
prompt` derivation inside the round-trip loop (re-read fresh every
iteration) - a handoff now takes effect on the very next Gemini call within
the same turn. Also strengthened `_tool_transfer_to_builder`/
`_tool_transfer_to_help`'s (`modules/agents/tools/`) return payload with an
explicit `instruction` field telling the model to address the user's
preceding message directly instead of just acknowledging the transfer - the
conversation history (including that message) was already present in
`contents`, the model just wasn't being told to use it. No schema/API change,
no new tests (same gap as every prior agents-module step) - import-smoke-
tested only.

**BYOK temporarily disabled (2026-09-24, no ADR)**: `poc/components/
AgentSettingsView.js`'s "Your own Gemini key" section is hidden (`v-if=
"false"`) - not available yet in the frontend. `modules/agents/
invoke_worker.py::_run_turn` now always uses the shared `GEMINI_API_KEY`,
never `agent.encrypted_gemini_api_key`, even for agents that already have one
stored from before this change (found via a real bug: a stray/invalid stored
BYOK key silently 404'd every turn for that agent with no user-visible
error). Schema column, `PATCH /agents/me`'s `gemini_api_key` write path, and
`crypto.py` are untouched - re-enabling is just restoring the decrypt call in
`invoke_worker.py` and un-hiding the settings section.

**All 7 agent prompts rewritten for conversational tone + language matching
(2026-09-25, no ADR - prompt-only UX change, requested by the user)**: every
system prompt the agents can run under - `builder_flow.py`'s `SUPERVISOR_PROMPT`/
`BUILDER_PROMPT`/`HELP_PROMPT` and `personas.py`'s `PERSONA_SYSTEM_PROMPTS`
(`sales_agent`/`support_agent`/`summarizer`/`one_off_executor`; `agent_builder`
untouched - it's dead code, never actually selected since ADR 0049 moved
config-mode fully onto `builder_flow.py`) - now carries an explicit style
block instructing the model to write like a person texting (short sentences,
natural line breaks, no markdown headers/bullet dumps, at most one emoji per
message, one focused question at a time, acknowledge briefly instead of
echoing back what the user said at length) plus a language-matching rule
(reply in whichever language the other party is writing in; default to
English if ambiguous). `builder_flow.py` defines a shared `STYLE_RULES`
string appended (via `.format`) to all three config-chat prompts;
`personas.py` defines a parallel `CHAT_STYLE_RULES` string appended to the
four storable execution personas (`summarizer` inlines its own short
formatting note instead, since its format is a recap not a chat, but carries
the same language-matching sentence). Two pre-existing spots in
`BUILDER_PROMPT` that contradicted the new no-echo/no-bullets rule were fixed
in the same pass: the "narrate every save" example changed from `"Saved: the
agent will now reply automatically to messages containing 'refund'..."` to a
natural `"Got it, saved 📝 - it'll jump in automatically on refund
questions..."`, and the final finish-summary instruction changed from
"structure this as ... a bullet list per trigger" to natural line breaks: the
checklist's own internal `##`/numbered-list structure is explicitly flagged
as for the model's own tracking only, never to be reproduced verbatim in
chat. Scope confirmed with the user: applies to all 7 prompts (both
config-mode and execution-mode), not just the config chat. No schema/tool/
architecture change - text-only, no ADR. No new tests (same gap as every
prior agents-module step) - import-smoke-tested only. Per the standing
obligation logged above, `HELP_PROMPT`'s own content did not need a factual
update from this change (it doesn't describe prompt tone anywhere), only the
style-block append.

**Internal ids hard-masked out of every tool result handed to Gemini
(2026-09-25, no ADR - real bug/security fix, requested by the user)**:
`read_history` and `search_messages` used to return raw `sender_id` (both)
and `chat_id` (search only) to the model as plain strings - the same class
of leak the CLAUDE.md frontend rule (`user_id` is strictly for backend
logic, never shown to an end user) already forbids in the PoC UI, just
reached here through the agent's own text output instead of a Vue
component. New `modules/agents/tools/common.py::_resolve_sender_labels` (batch
`modules.users.crud.get_users_by_ids`, new) resolves a list of sender ids to
`{name, phone_number}` in one query (name = `display_name || username ||
phone_number`, ADR 0024's convention) - `read_history` now returns
`sender_name`/`sender_phone_number` instead of `sender_id`;
`search_messages` returns the same plus keeps `chat_id` (the one exception -
it's the sole handle the model has to target a follow-up
`read_history(chat_id=...)` call on a specific hit, an argument the model
passes back into another tool call, never text it would reproduce to a
person) but drops `message_id`/`sender_id`. This is enforced unconditionally
server-side, not behind any `Agent.restrictions` toggle or the owner's
`system_prompt` - the user explicitly asked for hard enforcement over a
setting, same reasoning as every other restriction in this file being
server-side-only. Defense-in-depth: `CHAT_STYLE_RULES` in `personas.py` also
now tells the model never to mention or invent any internal id to anyone.
`pause_and_escalate`'s push notification was checked and left as-is -
`chat_id` only appears in the push `data` payload (client-side deep-link
target, never rendered as text) and `title`/`body` never contained a raw id.
No schema/architecture change. No new tests (same gap as every prior
agents-module step) - import-smoke-tested only.

**Trigger Rule Engine: missing commits + ADR 0054 auto-resume removed
(2026-09-26, found via new test coverage):** the first real test suite for
`trigger_engine.py` (`tests/modules/agents/test_trigger_engine.py`, 29
tests, real Postgres/Redis, no LLM mocking needed since this engine never
calls Gemini) surfaced that `evaluate_triggers`'s `session_scope()` block
never called `session.commit()` on the happy path - unlike every other
`session_scope()`/`get_db()` call site in the codebase. Two writes were
silently rolled back in production: (1) ADR 0051's
`auto_register_unknown_sender_chat` (the chat never actually got folded
into `on_specific_chats` after `on_unknown_sender` fired), and (2) the old
ADR 0054 `resume_most_recent_pause` call. Fixed by adding explicit
`await session.commit()` after each of those writes and after the
activation-quota-exceeded notice path in `_evaluate_triggers` (the
quota-notice message itself happened to survive before the fix only
because `send_system_message`'s `create_message` commits internally - that
was incidental, not a real fix). Separately, testing this surfaced that the
ADR 0054 auto-resume was itself the wrong behavior: it resumed the
most-recently-escalated paused chat on **any** owner message in their own
agent chat, regardless of content - so an unrelated message to the agent
silently un-paused an escalation the owner never meant to touch. Removed
that call entirely from `trigger_engine.py` and deleted
`crud.resume_most_recent_pause` (dead code once its only caller was gone).
Resuming a paused chat is now exclusively explicit via the `resume_paused_chat`
config tool (ADR 0055). See `ai_agent.md`'s `paused_chat_ids` section for
the corrected behavior description. All 466 tests pass after the change.
