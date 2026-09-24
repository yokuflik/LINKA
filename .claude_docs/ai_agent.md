# AI Agent (service account, Gemini tool calling) - ADR 0045 / ADR 0046 / ADR 0047 / ADR 0049 / ADR 0051

Full design rationale: `docs/adr/0045-ai-agent-service-account-tool-calling.md`
(base design), `docs/adr/0046-agent-schedules-knowledge-base-and-byok.md`
(schedules, knowledge base, BYOK, pre-filter cache - extends 0045, does not
replace it), `docs/adr/0047-agent-skills-tool-mode-gate-and-escalation.md`
(paid-tier rate limits, skills/personas, hard tool-mode gate, Agentic RAG,
escalation - extends 0045/0046; all 7 decisions implemented),
`docs/adr/0049-agent-builder-supervisor-help-substates.md` (Supervisor/
Builder/Help sub-states inside the config chat - extends 0047 decision 4,
does not replace it), and `docs/adr/0051-unknown-sender-auto-registration.md`
(auto-registers a chat into `on_specific_chats` after `on_unknown_sender`
fires, so the agent keeps replying to that same person - extends 0046
decision 2). This file tracks current implementation state, schema,
and rate limits/budgets - the detailed step-by-step build log (ADR 0045
steps 1-5, ADR 0046 decisions 1-6) moved to `.claude_docs/ai_agent_history.md`
on 2026-09-23 once this file passed the ~300-line split threshold again;
frontend detail (drawer UI, knowledge upload, BYOK UI) lives in
`.claude_docs/ai_agent_frontend.md`. ADR 0047's own implementation log for
decisions 5-7 lives in the ADR file itself, per the user's explicit request
to keep it alongside the ADR.

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

**Peer-visible typing indicator could momentarily show an unresolved
identity instead of the owner's name (2026-09-25, frontend-only fix, no
ADR)**: `_publish_peer_typing_loop` already sent the correct identity
(`user_id: owner_user_id`, never an agent id - the agent has no `user_id` of
its own, ADR 0045) - the bug was purely client-side resolution timing. Fixed
so the indicator never renders until the sender's identity is fully
resolved (deferred instead of racing), with a raw-id-safe fallback for the
remaining rare-error case. Full detail in
`.claude_docs/ai_agent_frontend.md`.

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
- Decision 4 (hard tool-mode gate): `modules/agents/tools.py::
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
- Decision 5 (Agentic RAG + escalation): `modules/agents/tools.py`
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
  both expose `active_skill`/`paused_chat_ids`.
- Decision 6 (config-mode tools): `_CONFIG_TOOL_HANDLERS` in `tools.py`
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
`modules/agents/tools.py` now give the `supervisor` builder-state its own
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
`_tool_transfer_to_help`'s (`modules/agents/tools.py`) return payload with an
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

## Current tool registry

**Execution mode** (any chat except `owner_agent_chat_id`, or a
schedule-fired turn): `send_message`, `reply_message`, `create_chat`,
`leave_group`, `read_history`, `update_own_triggers`, `search_messages`,
`get_knowledge_index`, `fetch_chunk`, `pause_and_escalate` - 10 tools.

**Config mode** (`chat_id == owner_agent_chat_id` only) further branches on
`Agent.builder_state` (ADR 0049) into three disjoint sub-sets:

| `builder_state` | Persona prompt | Tools |
|---|---|---|
| `supervisor` (default) | routes only | `transfer_to_builder`, `transfer_to_help` |
| `builder_agent` | interviews the owner | the 6 ADR 0047 config tools (`set_agent_persona`, `update_agent_rules`, `set_trigger`, `get_agent_status`, `estimate_api_usage`, `schedule_one_off_task`) + `transfer_to_help` + `finish_building_agent` |
| `help_agent` | explains the system | `transfer_to_builder` |

Selection is purely `chat_id`-then-`builder_state`-driven
(`modules/agents/tools.py::is_config_mode` + `BuilderState(agent.builder_state)`)
- never `active_skill`, `system_prompt`, or anything the model says about
itself. See ADR 0047 decision 4 (outer gate) and ADR 0049 (inner sub-states).

## Schema

```python
class Agent(Base):
    __tablename__ = "agents"
    id: BigInteger
    owner_user_id: BigInteger   # FK -> users.id, UNIQUE (one agent per user)
    owner_agent_chat_id: BigInteger  # FK -> chats.id, permanent 1:1 owner<->agent chat, created eagerly
    system_prompt: str          # soft constraint (Gemini system_instruction), NOT a security boundary
    restrictions: dict          # JSONB, hard/enforced - see below
    triggers: dict              # JSONB, Gatekeeper wake config - see below
    is_enabled: bool            # kill switch, default False (ADR 0047 decision 2), checked at trigger-eval AND worker-dequeue time
    encrypted_gemini_api_key: bytes | None  # BYOK (ADR 0046 decision 5), Fernet ciphertext, NULL = shared key
    active_skill: str           # persona catalog key (ADR 0047 decision 3), default "one_off_executor"
    paused_chat_ids: dict       # JSONB list, default [] (ADR 0047 decision 5) - escalated chats, human-resume only
    builder_state: str          # "supervisor"|"builder_agent"|"help_agent" (ADR 0049), default "supervisor" - only meaningful in the config chat
    created_at, updated_at

class AgentToolCallLog(Base):
    __tablename__ = "agent_tool_call_log"  # unpartitioned to start (ADR 0005 pattern if it grows)
    id, agent_id, tool_name, arguments (JSONB), allowed, denial_reason, created_at

class AgentKnowledgeDocument(Base):
    __tablename__ = "agent_knowledge_documents"  # ADR 0046 decision 4
    id, agent_id, filename, s3_key, mime_type, status ("processing"|"ready"|"failed"), created_at

class AgentKnowledgeChunk(Base):
    __tablename__ = "agent_knowledge_chunks"  # ADR 0046 decision 4
    id, agent_id, document_id, chunk_index, content, content_tsv, created_at
```

`Agent.restrictions` default (`DEFAULT_AGENT_RESTRICTIONS` in
`modules/agents/models.py`):
```json
{
  "can_send_messages": true,
  "can_message_groups": false,
  "can_message_private": true,
  "can_message_new_private_contacts": true,
  "can_leave_groups": true,
  "blocked_read_chat_ids": [],
  "max_messages_per_day": null
}
```
- `can_message_groups` defaults `false` for *newly created* agents only
  (AGENT_DRAWER_UI_PLAN.md Wave 2 / ADR 0047 era, 2026-09-23) - existing
  agents keep whatever value they already have (no backfill).
- `can_message_new_private_contacts=false` blocks only `create_chat`, not
  replying in an existing 1:1.
- `blocked_read_chat_ids` is enforced by the Trigger Rule Engine skipping
  the chat entirely (agent never invoked for it), not by a tool-level
  refusal. All keys enforced server-side in `execute_tool_call`, never
  relying on `system_prompt` for a security boundary.

`Agent.triggers` default (`DEFAULT_AGENT_TRIGGERS`):
```json
{
  "on_time_window": {"enabled": false, "start": "09:00", "end": "22:00"},
  "on_specific_chats": {},
  "on_unknown_sender": {"enabled": false},
  "on_schedule": []
}
```
- `on_specific_chats` maps `chat_id -> {"keywords": [...]}`. Empty keywords
  = wake on any message in that chat; non-empty = case-insensitive
  substring match (deliberately not regex - avoids ReDoS from
  user-supplied keywords).
- `on_unknown_sender.enabled` (ADR 0046 decision 2): fires once, on the
  first-ever message in a private (non-group) chat; own per-sender daily
  quota (20/day) on top of the hourly activation quota.
- `on_schedule` (ADR 0046 decision 3): list of `{id, kind: "recurring"|
  "once", time|at, instruction, chat_id?, enabled}` entries, driven by the
  `agent_schedule_due` Redis ZSET + a poll loop in `agent_worker`. Capped
  at `AGENT_MAX_SCHEDULE_ENTRIES` (10).
- `update_own_triggers` (execution-mode tool) and `set_trigger`
  (config-mode tool) both write here via `modules/agents/crud.py::
  update_agent_triggers` - hard-scoped to the caller's own `agent_id`,
  never touches `restrictions`.

`Agent.paused_chat_ids` (ADR 0047 decision 5): list of chat ids the agent
escalated via `pause_and_escalate` - skipped entirely at trigger-evaluation
time until a human calls `POST /agents/me/resume-chat/{chat_id}`.

## Rate limits / budgets (all via `infra/ratelimit`, ADR 0012)

| Limit | Scope | Mechanism |
|---|---|---|
| Gemini API calls | 30/min per-agent (ADR 0047 decision 1; skipped when BYOK key present) | `agent_gemini_calls:{agent_id}` |
| Function-call recursion | 8 round-trips/turn (ADR 0047 decision 1) | in-process cap |
| Trigger activation quota | 20/hour per-agent | `ratelimit:agent_activation:{agent_id}`, Redis fixed-window |
| Unknown-sender daily quota | 20/day per-sender (ADR 0046 decision 2) | `ratelimit:agent_unknown_sender:{agent_id}:{sender_user_id}` |
| Daily active-time budget | 1 hour/day per-agent, counts actual processing wall-clock (Gemini + tool exec) | `ratelimit:agent_active_seconds:{agent_id}`, Redis fixed-window, seconds-based |
| Knowledge base | 20 documents / 2000 chunks per-agent (ADR 0046 decision 4) | Postgres count check at upload |
| Schedule entries | 10 per-agent (ADR 0046 decision 3) | Postgres count check at write |
| Tool-call side effects | same as a human user | reuses existing per-user limits (agent acts as owner's `user_id`) |

When the daily time budget is exhausted: finish the in-flight turn, then go
dormant until the daily window resets (no announcement message is
currently sent - a known gap, not yet built).
