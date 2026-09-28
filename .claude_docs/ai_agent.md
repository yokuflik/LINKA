# AI Agent (service account, Gemini tool calling) - ADR 0045 / ADR 0046 / ADR 0047 / ADR 0049 / ADR 0051 / ADR 0053 / ADR 0057 / ADR 0059 / ADR 0063 / ADR 0064 / ADR 0065 / ADR 0066 / ADR 0067 / ADR 0071 / ADR 0072 / ADR 0073 / ADR 00732 / ADR 0075 / ADR 0077 / ADR 0078

Full design rationale: `docs/adr/0045-ai-agent-service-account-tool-calling.md`
(base design), `docs/adr/0046-agent-schedules-knowledge-base-and-byok.md`
(schedules, knowledge base, BYOK, pre-filter cache - extends 0045, does not
replace it), `docs/adr/0047-agent-skills-tool-mode-gate-and-escalation.md`
(paid-tier rate limits, skills/personas, hard tool-mode gate, Agentic RAG,
escalation - extends 0045/0046; all 7 decisions implemented),
`docs/adr/0049-agent-builder-supervisor-help-substates.md` (Supervisor/
Builder/Help sub-states inside the config chat - extends 0047 decision 4,
does not replace it; the single Help state it introduced was later split
into two by ADR 0064 below), `docs/adr/0051-unknown-sender-auto-registration.md`
(auto-registers a chat into `on_specific_chats` after `on_unknown_sender`
fires, so the agent keeps replying to that same person - extends 0046
decision 2), and `docs/adr/0053-llm-judge-message-gate.md` (LLM Judge
pre-filter gate in front of execution-mode message-fired turns - extends
0045/0046/0047/0049/0051, does not replace any of them), and
`docs/adr/0064-help-agent-general-and-agent-building-split.md` (splits ADR
0049's single Help sub-state into `help_general`/`help_agent_building`, each
with its own prompt and a set of transfer tools reaching every other
sub-state - no execution/config tool of its own in either), and
`docs/adr/0065-config-mode-no-reply-needed-tool.md` (no-op `no_reply_needed`
config tool in every `builder_state`, so a config-mode turn can end without
posting anything - extends 0047/0049/0063, does not replace them). ADR 0047's
own implementation log for decisions 5-7, and ADR 0053's full implementation
log, both live in their respective ADR files, per the user's explicit request
to keep implementation detail alongside the ADR once it's substantial.

**This file is the current-state index/reference** (schema, defaults, tool
registry, rate limits). Detailed build history and per-change narrative
moved out into topic files as this file kept re-crossing the ~300-line
split threshold:

| File | Contents |
|---|---|
| `.claude_docs/ai_agent_history.md` | ADR 0045 steps 1-5 + ADR 0046 decisions 1-6 build log (schema, Trigger Rule Engine, worker, tool registry/Gemini client, config API, pre-filter cache, `on_unknown_sender`, `on_schedule`, knowledge base/RAG, BYOK), plus the AGENT_DRAWER_UI_PLAN.md Wave 2 backend pieces. |
| `.claude_docs/ai_agent_changelog.md` | Chronological log of no-ADR fixes/prompt tuning/in-scope UX changes: `tools.py` package split (ADR 0056), peer-visible typing indicator + its int/string bug, `agent_thinking` drawer leak fix, ADR 0049 (Supervisor/Builder/Help) build detail, ADR 0047 all-decisions summary, persona tone fixes, self-triggering-loop bug, config-mode reply-not-posted bug, Gemini model bump, ADR 0050 reset-to-default, ADR 0051 auto-registration, Supervisor→Help handoff, history-transcript formatting, mid-turn handoff bug, BYOK disable, all-7-prompts tone rewrite, internal-id masking. |
| `.claude_docs/ai_agent_judge_and_escalation.md` | ADR 0053 LLM Judge gate (model, fail-open, redirect-message, `AgentJudgeLog`); `pause_and_escalate` behavior/notices (talk-to-a-human trigger fix, formatted handoff notice, customer-facing transfer reply); identity-masking detail; `resolve_user` config tool. |
| `.claude_docs/ai_agent_capacity_and_budgets.md` | ADR 0057 (`get_capacity_status` tool + `peek_fixed_window`), ADR 0058 (agent shares owner's WS send-message sliding-window budget + `peek_sliding_window`), ADR 0059 (5h/500k + 7d/3M rolling token-usage windows, output-token cap, `GET /agents/me/usage`, `UsageProgressBar.js`). |
| `.claude_docs/ai_agent_frontend.md` | AI agent PoC frontend: `AgentDrawer.js`/`AgentChatView.js`/`AgentSettingsView.js`, `useAgentConfig.js`, BYOK frontend, peer-visible typing indicator frontend detail. |

**Standing obligation (2026-09-25, updated 2026-09-26 by ADR 0064, process
note, not code)**: the Help Agent used to be one state
(`modules/agents/builder_flow.py::HELP_PROMPT`); ADR 0064 split it into two
- `HELP_BUILDING_PROMPT` (agent-building/config questions) and
`HELP_GENERAL_PROMPT` (everything else about using Linka). Both explain
purely from their static system prompt text - neither has a tool that reads
live code or docs. Whenever a change touches the agent system's user-facing
behavior (new/changed tools, triggers, skills, restrictions, rate limits, or
flow), check `HELP_BUILDING_PROMPT`; whenever a change touches a general
platform feature (chats, search, groups, media, receipts, profile, etc.),
check `HELP_GENERAL_PROMPT` - a discipline for this assistant to apply every
time either surface changes, alongside the existing `.claude_docs/`
auto-maintenance rule in the root `CLAUDE.md`. Both prompts must also stay
interface-only (never mention backend/infra/model/mechanism terms - see ADR
0064). Full detail of past checks lives in `ai_agent_changelog.md`.

## Current tool registry

**Execution mode** (any chat except `owner_agent_chat_id`, or a
schedule-fired turn): `send_message`, `reply_message`, `create_chat`,
`leave_group`, `read_history`, `update_own_triggers`, `search_messages`,
`search_semantic` (ADR 0069), `search_knowledge_semantic` (ADR 0078),
`get_knowledge_index`, `fetch_chunk`, `pause_and_escalate` - 12 tools.
`search_messages`/`search_semantic` both take optional `start_date`/`end_date`
(ADR 0068), which now also accept a specific time of day, not just a calendar
date (ADR 0070).

**Config mode** (`chat_id == owner_agent_chat_id` only) further branches on
`Agent.builder_state` (ADR 0049, ADR 0064) into four sub-sets (supervisor/
builder_agent deliberately overlap with execution mode, per ADR 0062 below;
neither Help state does):

| `builder_state` | Persona prompt | Tools |
|---|---|---|
| `supervisor` (default) | routes, AND acts directly for the owner (ADR 0062) | `transfer_to_builder`, `transfer_to_help_building`, `transfer_to_help_general` (ADR 0064), `resume_paused_chat` (ADR 0055), `resolve_user`, `find_chat_by_name` (ADR 0073), `spawn_ephemeral_task` (ADR 0061), `no_reply_needed` (ADR 0065), `save_knowledge_from_text` (ADR 0078), **plus the full execution-mode toolset** (`send_message`, `reply_message`, `create_chat`, `leave_group`, `read_history`, `update_own_triggers`, `search_messages`, `search_semantic`, `search_knowledge_semantic`, `get_knowledge_index`, `fetch_chunk`, `pause_and_escalate`) |
| `builder_agent` | interviews the owner, AND acts directly for the owner too (2026-09-26) | the 6 ADR 0047 config tools (`set_agent_persona`, `update_agent_rules`, `set_trigger`, `get_agent_status`, `estimate_api_usage`, `schedule_one_off_task`) + `resolve_user` + `find_chat_by_name` (ADR 0073) + `resume_paused_chat` (ADR 0055) + `no_reply_needed` (ADR 0065) + `save_knowledge_from_text` (ADR 0078) + `transfer_to_help_building` + `transfer_to_help_general` (ADR 0064) + `transfer_to_supervisor` + `finish_building_agent`, **plus the full execution-mode toolset** (same 12 tools as supervisor) |
| `help_agent_building` (ADR 0064) | explains building/configuring an agent | `transfer_to_builder`, `transfer_to_help_general`, `transfer_to_supervisor`, `no_reply_needed` (ADR 0065) |
| `help_general` (ADR 0064) | explains using the Linka platform | `transfer_to_help_building`, `transfer_to_supervisor`, `no_reply_needed` (ADR 0065) |

**ADR 0065 (2026-09-26):** `no_reply_needed` is a no-op config-mode tool
available in all four `builder_state`s - lets the model end a config-mode
turn without `invoke_worker.py::_run_turn` calling `_post_config_reply`
(previously unconditional whenever a turn ended with plain text and no
function call). Added because ADR 0063's debounce/coalescing could still
leave a coalesced or otherwise re-fired turn with nothing new to say -
without this the model had no way to avoid re-asking a question it already
asked (and is waiting on), or sending filler acknowledging a message that
needed no reply. Each config-chat prompt (`builder_flow.py`'s shared
`STYLE_RULES`) now instructs the model to call it in exactly that situation.
Execution-mode turns are unaffected - they already end silently on a
text-only reply (no `send_message`/`reply_message` call), this only closes
the equivalent gap on the config-mode side.

**ADR 0064 (2026-09-26):** the original single `help_agent` state covered
both "how do I build an agent" and "how does Linka work" questions in one
prompt with no material for the latter. Split into two disjoint personas -
`HELP_BUILDING_PROMPT` (agent-building/config, `builder_flow.py`) and
`HELP_GENERAL_PROMPT` (general platform features: chats, search, groups,
media, receipts, forwarding, scheduled messages, profile, storage). Both
are interface-only by design - forbidden from naming any backend/infra/
model/mechanism term, same zero-leakage discipline as every other
customer/owner-facing persona (see the identity-masking entry in
`ai_agent_judge_and_escalation.md`). Supervisor/Builder route to whichever
fits the question, defaulting to `help_general` when ambiguous; either Help
state can transfer directly to its sibling or fall back to
`transfer_to_supervisor` when unsure - the Supervisor is the one state that
always knows where to route next, so neither Help state needs to reason
about anything beyond "answer, hand to my sibling, or give up to
Supervisor." No schema/migration - `Agent.builder_state` is a plain string
column, no DB-level enum constraint.

**`transfer_to_supervisor` (2026-09-26):** Builder-only escape hatch back to
Supervisor, distinct from `finish_building_agent` - does not require the
checklist to be complete and does not set `is_enabled`/touch
`sync_agent_cache` (whatever was already saved via the incremental config
tools just stays saved). Lets the owner drop out of the interview mid-way to
pause setup for later. `builder_flow.py::BUILDER_PROMPT` instructs the
Builder to call it when the user explicitly wants to stop/pause, or their
intent has clearly shifted away from configuration for the rest of the
conversation - not for a one-off direct action mid-interview, since the
Builder can now just do those itself (see below).

**Every handoff tool's transfer must be invisible to the user (2026-09-26):**
`transfer_to_builder`/`transfer_to_help_building`/`transfer_to_help_general`/
`transfer_to_supervisor` each return
an `instruction` string that explicitly forbids announcing the switch (no
"switching you to the builder" / "transferring you back to the regular
agent") - the model must just act on the user's message as if it had been
handling it all along. This was a real observed failure mode (the model
narrated the handoff instead of silently continuing) before the instruction
text was strengthened.

**ADR 0062 (2026-09-26), extended 2026-09-26 to cover Builder too:**
Supervisor and Builder are both deliberate exceptions to ADR 0047's
"config-mode and execution-mode schemas never overlap" invariant
(`schemas.py::BUILDER_STATE_TOOL_SCHEMAS[SUPERVISOR]` and `[BUILDER]` both
union in the full `TOOL_SCHEMAS`; `builder_handoff.py::BUILDER_STATE_HANDLERS`
unions in `EXECUTION_TOOL_HANDLERS` for both the same way). This lets the
owner issue a direct "act as me" command - "send a message to +972-5-xxx and
tell me what they say" - from either their idle chat (Supervisor) or
mid-interview (Builder), without needing `transfer_to_supervisor` first: the
owner is the agent's own supervised user in both states, so there's no
prompt-injection concern in giving Builder the same reach as Supervisor. Same
handler bodies, same `Agent.restrictions`/quota enforcement as any other
execution-mode call (ADR 0045) - only which tools are *reachable* changed,
not what any of them do once called. Both Help states (ADR 0064) keep the
original zero-overlap invariant unchanged (no supervised-owner-only
exception applies there - Help never acts, only explains). Fixed a real bug:
before ADR 0062, Supervisor had
no tool that could message a third party at all (only `spawn_ephemeral_task`,
and even that was missing from its schema list), so this exact request
always failed with "I can't do that."

`get_capacity_status` (ADR 0057) is deliberately excluded from the Builder's
tool set (`schemas.py::_BUILDER_TOOL_SCHEMAS` filters it out of
`CONFIG_TOOL_SCHEMAS`) and no longer called during `finish_building_agent` -
removed by user request 2026-09-26; it remains defined/dispatchable for other
config-mode contexts, just not offered to the interview flow.

Selection is purely `chat_id`-then-`builder_state`-driven
(`modules/agents/tools/dispatch.py::is_config_mode` + `BuilderState(agent.builder_state)`)
- never `active_skill`, `system_prompt`, or anything the model says about
itself. See ADR 0047 decision 4 (outer gate) and ADR 0049 (inner sub-states).
Full per-tool build detail: `ai_agent_history.md` (original registry),
`ai_agent_judge_and_escalation.md` (`resolve_user`, `pause_and_escalate`),
`ai_agent_capacity_and_budgets.md` (`get_capacity_status`).

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
    paused_chat_ids: dict       # JSONB list, default [] (ADR 0047 decision 5) - escalated chats; ADR 0054: list of {chat_id, paused_at, expires_at}, auto-expires after AGENT_ESCALATION_PAUSE_HOURS (default 24) or on human resume/owner-chat reply
    builder_state: str          # "supervisor"|"builder_agent"|"help_general"|"help_agent_building" (ADR 0049/0064), default "supervisor" - only meaningful in the config chat
    created_at, updated_at

class AgentToolCallLog(Base):
    __tablename__ = "agent_tool_call_log"  # unpartitioned to start (ADR 0005 pattern if it grows)
    id, agent_id, tool_name, arguments (JSONB), allowed, denial_reason, created_at

class AgentKnowledgeDocument(Base):
    __tablename__ = "agent_knowledge_documents"  # ADR 0046 decision 4
    id, agent_id, filename, s3_key, mime_type, status ("processing"|"ready"|"failed"), created_at
    # s3_key is nullable (ADR 0078) - a document created via save_knowledge_from_text
    # has nothing uploaded to S3; delete_knowledge_document skips the S3 call for it.

class AgentKnowledgeChunk(Base):
    __tablename__ = "agent_knowledge_chunks"  # ADR 0046 decision 4
    id, agent_id, document_id, chunk_index, content, content_tsv, embedding, created_at
    # embedding vector(768), nullable (ADR 0078) - own IVFFlat index
    # (ix_agent_knowledge_chunks_embedding_ivfflat), separate from messages.embedding's
    # (ADR 0042) - unrelated table, unrelated growth curve/tenant scoping, deferred-build
    # the same way (see modules/agents/knowledge_ddl.py::ensure_knowledge_ivfflat_index).
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

**DB-level backstop (ADR 0066, 2026-09-26):** the checks above, in
`modules/agents/tools/execution.py`, are real enforcement against the model
but not against a Python bug - a missed check in a new tool handler, or a
future code path calling the messaging/chat services directly for an agent,
would silently bypass them. `modules/agents/restriction_ddl.py` adds three
Postgres `BEFORE` triggers that re-check the *same* `agents.restrictions`
row independently, so the database itself refuses the write no matter what
Python code (or bug) tried to make it:
- `trg_agents_enforce_message_restrictions` (`BEFORE INSERT ON messages`):
  reads the new `messages.sender_agent_id` column (populated by
  `create_message`/`process_outgoing` whenever the caller is an agent -
  never inferred from `type == AGENT_REPLY_MESSAGE_TYPE`, which is
  display-only) and enforces `can_send_messages` /
  `can_message_groups`/`can_message_private` / `blocked_read_chat_ids`.
- `trg_agents_enforce_leave_group` (`BEFORE DELETE ON participants`):
  enforces `can_leave_groups`.
- `trg_agents_enforce_new_private_chat` (`BEFORE INSERT ON participants`):
  enforces `can_message_new_private_contacts`, distinguishing a brand-new
  1:1 chat from adding a member to one that already has participants.

The last two read `current_setting('app.current_agent_id', true)` - a
`SET LOCAL app.current_agent_id = '<id>'` issued by the write path
(`modules/messaging/crud.py::create_message`,
`modules/chats/membership.py::remove_member`,
`modules/chats/creation.py::get_or_create_private_chat`, all via a new
keyword-only `sender_agent_id` param) inside the same transaction as the
mutation - transaction-scoped so it's safe under PgBouncer transaction
pooling, never persisted anywhere, never used for anything except these
triggers. `blocked_read_chat_ids` on the *read* side (`read_history`/
`search_messages`) and `max_messages_per_day` are deliberately NOT given a
DB trigger (no `BEFORE SELECT` in Postgres; a rolling quota isn't a
row-level constraint) - both stay application-only, a known/accepted gap,
not an oversight. DDL applied via `scripts/init_db.py` +
`tests/conftest.py`, same idempotent `CREATE OR REPLACE FUNCTION` / `DROP
TRIGGER IF EXISTS` / `CREATE TRIGGER` convention as `modules/search/ddl.py`
(ADR 0040). Full rationale, including why Postgres roles + RLS were
considered and rejected: `docs/adr/0066-db-level-agent-restriction-enforcement.md`.

`Agent.triggers` default (`DEFAULT_AGENT_TRIGGERS`):
```json
{
  "on_time_window": {"enabled": false, "start": "09:00", "end": "22:00"},
  "on_specific_chats": {},
  "on_unknown_sender": {"enabled": false},
  "on_any_message": {"enabled": false},
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
- `on_any_message.enabled` (ADR 0052, 2026-09-25): fires on **every**
  message in **every** private (non-group) chat - a broader, stateless
  catch-all (unlike `on_unknown_sender`, never mutates `on_specific_chats`).
  Groups excluded, same scope as `on_unknown_sender`. Still gated by
  `on_time_window` + `blocked_read_chat_ids` + `paused_chat_ids`; reuses the
  plain hourly `agent_activation` quota, no separate budget. Matched in
  `trigger_engine._matches_any_message` (own DB round-trip for
  `chat.is_group`, parallel to `_matches_unknown_sender`). Settings UI:
  one checkbox, "Reply to every new private message", commits immediately
  (`AgentSettingsView.js` / `useAgentConfig.js::setAnyMessageEnabled`).
- `on_schedule` (ADR 0046 decision 3): list of `{id, kind: "recurring"|
  "once", time|at, instruction, chat_id?, enabled}` entries, driven by the
  `agent_schedule_due` Redis ZSET + a poll loop in `agent_worker`. Capped
  at `AGENT_MAX_SCHEDULE_ENTRIES` (10).
- `update_own_triggers` (execution-mode tool) and `set_trigger`
  (config-mode tool) both write here via `modules/agents/crud.py::
  update_agent_triggers` / `update_agent_config` - hard-scoped to the
  caller's own `agent_id`, never touches `restrictions`. Both merge
  `on_time_window`/`on_unknown_sender`/`on_any_message` one level deep
  (`crud.py::_merge_triggers`) rather than replacing the sub-object
  outright - a 2026-09-24 incident (`AgentOut` 500 on `GET /agents/me`)
  found a partial `{"on_time_window": {"enabled": false}}` patch dropping
  `start`/`end` under the old top-level-only shallow merge; `on_specific_chats`
  is merged per-`chat_id` for the same reason, but the key-set itself still
  follows the patch (a key absent from the patch is dropped from the
  result), so the frontend's "always send the full current map" convention
  still deletes entries correctly.

`Agent.paused_chat_ids` (ADR 0047 decision 5, shape updated by ADR 0054):
list of `{chat_id, paused_at, expires_at}` objects for chats the agent
escalated via `pause_and_escalate` - skipped entirely at trigger-evaluation
time (`trigger_engine._active_paused_chat_ids`, lazy expiry checked on
read) until one of two things happens: `expires_at` lapses
(`AGENT_ESCALATION_PAUSE_HOURS`, default 24, env-overridable), or a human
calls `POST /agents/me/resume-chat/{chat_id}`. **2026-09-26 (no ADR,
behavior fix):** the original ADR 0054 "any owner message in their own
agent chat resumes the most-recently-escalated pause" shortcut
(`crud.resume_most_recent_pause`) was removed from `trigger_engine.py` - it
fired on message content indiscriminately, so a plain unrelated message to
the agent silently un-paused an escalation the owner had no intention of
resuming. `resume_most_recent_pause` itself was deleted from `crud.py` as
dead code once its only caller was removed. Resuming a paused chat is now
only ever explicit: (ADR 0055) the owner names a person via the
`resume_paused_chat` config tool (Supervisor + Builder states), which
resolves phone_number/username through the `resolve_user` contract and
un-pauses just that chat via `crud.resume_agent_chat`. Full escalation/
notice detail: `ai_agent_judge_and_escalation.md`.

**No-code-execution rule (2026-09-26, no ADR, prompt-only)**: added a fixed
"never run code" clause to the shared style blocks both config-mode
(`builder_flow.py::STYLE_RULES`, inherited by Supervisor/Builder/Help) and
execution-mode (`personas.py::CHAT_STYLE_RULES`, inherited by every
storable skill) prompts inherit - the agent must refuse to run/execute/
evaluate any code, script, shell command, or similar sent by anyone in a
message, including its own owner, regardless of framing. This is a soft,
system-prompt-level instruction only (like `system_prompt` itself, not a
security boundary enforced in `execute_tool_call`) - the agent has no
code-execution tool in the registry to begin with, so this is defense in
depth against the model being talked into simulating/roleplaying execution,
not a fix for an actual capability.

**Agent-decided knowledge-base ingestion + semantic retrieval (ADR 0078,
2026-09-28)**: new config-mode tool `save_knowledge_from_text` (Supervisor +
Builder, `modules/agents/tools/config_mode.py::_tool_save_knowledge_from_text`)
lets the model itself decide, within a normal tool-calling turn, that the
owner's latest message is reference/lookup data (inventory, price lists,
policies, FAQs) that should never re-enter the prompt verbatim - an
owner-invoked command was deliberately rejected (the owner isn't expected to
know when the mechanism applies). Known, accepted risk: this is a
classification judgment, not a size threshold, and can misfire either way;
mitigated by prompt wording + a hard **transparency requirement** (the tool's
schema description requires the agent to tell the owner what it saved and why,
in the same turn - never silently). New text-only ingestion path,
`modules/agents/knowledge_service.py::commit_knowledge_text` - same
chunk -> quota-check -> document -> chunk-rows sequence as
`commit_knowledge_document`, skipping the S3 fetch since the text arrives
directly as a tool-call argument (`AgentKnowledgeDocument.s3_key=None`,
`mime_type="text/plain"`, now nullable on the model).

Both commit paths (`commit_knowledge_document` too) now embed their chunks
**synchronously** right after the rows are written
(`knowledge_service.py::_embed_chunks_best_effort`, one `gemini_client.
embed_batch` call per document) - deliberately not the flush-on-demand Redis
queue ADR 0042 uses for messages, since knowledge documents are created rarely
(an occasional action) rather than on every send, so there's no hot-path
latency to defer around, and immediate searchability (the owner's very next
message could already retrieve it) outweighs the batching win. A Gemini
failure mirrors ADR 0042's posture - logs a warning, leaves `embedding` NULL,
never blocks/rolls back the document commit.

New execution-mode tool `search_knowledge_semantic`
(`modules/agents/tools/execution.py::_tool_search_knowledge_semantic` ->
`knowledge_service.py::search_knowledge_semantic` ->
`modules/agents/knowledge_crud.py::semantic_search_knowledge_chunks`): embeds
the query via the same live query-embedding LRU (`vector_search.query_cache`,
ADR 0044, agent-agnostic cache key) and cosine-searches
`agent_knowledge_chunks` scoped hard to `agent_id`, returning top matches'
content directly - no index-then-fetch two-hop. `get_knowledge_index`/
`fetch_chunk` are **not removed** - they stay the fallback for a small
knowledge base and for chunks with no embedding (an embed failure mid-ingest).
Both prompts (`SUPERVISOR_PROMPT`/execution `TOOL_SCHEMAS` description) steer
the model to prefer `search_knowledge_semantic` once a KB exists.

New `AgentKnowledgeChunk.embedding vector(768)` column + its own deferred-build
IVFFlat index (`modules/agents/knowledge_ddl.py::ensure_knowledge_ivfflat_index`,
NOT auto-run by `apply_knowledge_ddl`/`init_db.py` - same "empty-table
centroids are useless" reasoning as ADR 0042 for messages) - a second,
independent IVFFlat index alongside `ix_messages_embedding_ivfflat`, never
sharing the messages table's index or ADR 0042's Redis queue. Reuses 100% of
existing chunking/quota/Gemini-embedding infra, no new external dependency.

## Rate limits / budgets (all via `infra/ratelimit`, ADR 0012)

| Limit | Scope | Mechanism |
|---|---|---|
| Gemini API calls | 30/min per-agent (ADR 0047 decision 1; skipped when BYOK key present) | `agent_gemini_calls:{agent_id}` |
| LLM Judge calls | 60/min per-agent (ADR 0053, own bucket, never shares the Gemini API calls budget above) | `ratelimit:agent_judge_calls:{agent_id}` |
| Function-call recursion | 8 round-trips/turn (ADR 0047 decision 1) | in-process cap |
| Trigger activation quota | 100/hour per-agent | `ratelimit:agent_activation:{agent_id}`, Redis fixed-window |
| Unknown-sender daily quota | 20/day per-sender (ADR 0046 decision 2) | `ratelimit:agent_unknown_sender:{agent_id}:{sender_user_id}` |
| Daily active-time budget | 1 hour/day per-agent, counts actual processing wall-clock (Gemini + tool exec) | `ratelimit:agent_active_seconds:{agent_id}`, Redis fixed-window, seconds-based |
| Knowledge base | 20 documents / 2000 chunks per-agent (ADR 0046 decision 4) | Postgres count check at upload |
| Schedule entries | 10 per-agent (ADR 0046 decision 3) | Postgres count check at write |
| Tool-call side effects | same as a human user | reuses existing per-user limits (agent acts as owner's `user_id`) |
| Send-message throughput | shared with the owner's own WS budget: 3/1s + 40/60s (ADR 0058) | `rlsw:send_message(_burst):{owner_user_id}` |
| Token usage - session | 500,000 tokens / 5h per-agent, combined input+output (ADR 0059) | `ratelimit:agent_tokens_5h:{agent_id}`, Redis fixed-window, token-weighted |
| Token usage - weekly | 3,000,000 tokens / 7d per-agent, combined input+output (ADR 0059) | `ratelimit:agent_tokens_7d:{agent_id}`, Redis fixed-window, token-weighted |

When the daily time budget is exhausted: finish the in-flight turn, then go
dormant until the daily window resets (no announcement message is
currently sent - a known gap, not yet built).

## Message-batch debounce + per-chat turn mutex (ADR 0063)

A matched trigger no longer calls `enqueue_invocation` directly -
`trigger_engine.py` calls `invoke_debounce.arm_debounce(agent_id, chat_id,
message_id)`, which `ZADD`s `{agent_id}:{chat_id}` onto
`agent_invoke_debounce_due` scored `now + AGENT_INVOKE_DEBOUNCE_SECONDS`
(default 2s) and stashes `message_id` in a matching `agent_invoke_debounce_
msg:{agent_id}:{chat_id}` STRING. A second match for the same pair before it
fires just overwrites both (plain `ZADD`/`SET`) - a fast burst or a
self-correction ("I want blue" / "wait, purple") coalesces into a single
turn seeded from the *latest* message, instead of racing one Gemini turn per
message. All quota/permission checks still run per-message at match time,
unchanged. `invoke_worker.py::_invoke_debounce_poll_loop` (1s tick,
alongside the existing 30s schedule-poll loop) pops due pairs and is what
actually calls `enqueue_invocation`.

Separately, `AgentInvokeConsumer.process_entry` holds a Redis mutex
(`agent_turn_lock:{agent_id}:{chat_id}`, `SET NX EX AGENT_TURN_TIMEOUT_
SECONDS`) around every `_run_turn` call - a debounced fire landing while a
previous turn for the same pair is still running (up to 90s) does not start
a second concurrent turn; it calls `arm_debounce` again (no message_id, so
whatever was last stashed carries over) so the message gets a real turn
right after the in-flight one's lock releases, instead of being dropped.

Complementary prompt-level instruction (not a substitute for the above):
`personas.py::CHAT_STYLE_RULES` tells the model to read an unanswered run of
messages backwards and let the latest one override an earlier, contradicted
one, replying naturally to the final ask without mentioning the correction.

**Superseding an in-flight turn (ADR 00732, 2026-09-27):** ADR 0063's turn-mutex
fallback (a debounced fire landing while a previous turn is still running)
only re-armed the debounce - the in-flight turn itself was left untouched and
could still call `send_message`/`reply_message` before the mutex released,
producing a stale reply followed by a second, real one. `process_entry`'s
same re-arm call site now also calls `invoke_debounce.mark_superseded(agent_id,
chat_id)` (`agent_turn_superseded:{agent_id}:{chat_id}`, TTL =
`AGENT_TURN_TIMEOUT_SECONDS`, same self-healing bound as the turn lock).
`_run_turn` checks `is_superseded` (atomic `GETDEL`) at two points: the top of
every round-trip loop iteration, and again immediately before dispatching
`send_message`/`reply_message` specifically (closes the race where the flag
was set while that round-trip's Gemini call was already in flight). Either
hit ends the turn with no reply delivered and no re-arm (the message that set
the flag already re-armed); every other tool call already made by the
superseded turn (e.g. `read_history`) is simply wasted, not merged into the
replacement turn. **Does not attempt real Gemini request cancellation** -
confirmed via `ai.google.dev` docs that no cancellation/billing-on-early-stop
contract is documented; the underlying HTTP call runs to completion or
timeout exactly as before, this only gates whether its result ever reaches a
chat. No new owner-facing notice; `agent_thinking` stays in whatever state it
was in when superseded.

**History/search pagination + truncation notice (ADR 0067, 2026-09-26)**:
`read_history` (still 20 messages/call) and `search_messages` (still 10
results/call) now accept `before_id`/`cursor` respectively and return
`has_more` (+ `next_before_id`/`next_cursor`) instead of silently truncating.
`CHAT_STYLE_RULES` (execution-mode, `personas.py`) and `STYLE_RULES`
(config-mode, `builder_flow.py` - reachable there too since Supervisor/
Builder get the full execution toolset per ADR 0062) both instruct the model
to say plainly that there's more than it pulled in one call whenever
`has_more: true`, and offer to continue in parts, rather than answering as if
the page were the whole history/result set. Caps themselves are unchanged;
this is pagination + disclosure, not a bigger single-call limit.

**Per-call `limit` argument (2026-09-27, no ADR)**: `read_history`,
`search_messages`, `search_semantic`, and `bulk_fetch_messages` now all
accept an optional `limit` in their tool-call arguments, letting the model
ask for fewer or more results than each tool's own default in a single call
instead of always getting the fixed page size. New shared
`execution.py::_clamp_tool_limit(requested, default, max_limit=None)` always
clamps to `[1, max_limit]` server-side regardless of what the model passes
(never trusted as-is, same posture as every other restriction/quota check in
this module) - a malformed value raises `ToolDeniedError`, not a 500.
`read_history`/`search_messages` default to their existing 20/10 page sizes,
capped at the new shared `AGENT_TOOL_RESULT_MAX_LIMIT` (50,
`config/agent_settings.py`, env-overridable). `search_semantic` defaults to
`DEFAULT_VECTOR_SEARCH_LIMITS.default_limit`, capped at
`min(AGENT_TOOL_RESULT_MAX_LIMIT, DEFAULT_VECTOR_SEARCH_LIMITS.max_limit)` -
never lets an agent call exceed the vector-search service's own ceiling.
`bulk_fetch_messages` keeps its own much larger ceiling
(`AGENT_BULK_FETCH_MAX_MESSAGES`, 1000) untouched - its `limit` (if passed)
is clamped to `[1, AGENT_BULK_FETCH_MAX_MESSAGES]`, independent of the shared
50 cap, since it's already gated behind `count_messages_in_range` +
owner confirmation (ADR 0072). `count_messages_in_range` itself takes no
`limit` (it returns a plain count, not a list). No schema/architecture
change, no new tests (same gap as every prior agents-module step) - full
agents suite re-run green after landing.

**History transcript `[already handled]` marker (ADR 0071, 2026-09-26)**:
`_format_history_transcript` (`invoke_worker.py`) marks any Customer/Owner
line that is followed later in the same flattened transcript by an Agent
line as `[already handled]` - a structural, order-based signal (no new
schema/state) fixing a bug where an old owner instruction (e.g. "summarize
the chat with Yossi") sitting unmarked in the transcript on a later,
unrelated turn read to Gemini as still-pending and could trigger a
re-execution of the tool call that already handled it. Only the trailing run
of unanswered lines (no Agent line after them yet) stays unmarked. A matching
`STYLE_RULES` rule in `builder_flow.py` tells the model to treat marked lines
as past context only, never to re-act on them.

**Bulk chat summarization with a hard confirmation gate (ADR 0072,
2026-09-27)**: two new execution-mode tools (auto-unioned into
Supervisor/Builder per ADR 0062) let the agent summarize a whole chat/date
range in one call instead of paging `read_history` 20 rows at a time -
`count_messages_in_range(chat_id, start_date?, end_date?)` (free
`SELECT COUNT(*)`, `modules/messaging/crud.py`) and
`bulk_fetch_messages(chat_id, start_date?, end_date?)` (up to
`AGENT_BULK_FETCH_MAX_MESSAGES` = 1000 messages, single shot, no pagination
envelope). The model must call the count first: `too_large: true` (count
>1000) -> tell the owner to narrow the range, never attempt the fetch;
`needs_confirmation: true` (count <=1000) -> ask the owner to confirm the
"expensive operation" and end the turn - never call `bulk_fetch_messages` in
that same turn. `count_messages_in_range`'s handler stashes the request on a
new nullable `Agent.pending_confirmation` JSONB column
(`{tool, chat_id, start_at, end_at, count, created_at}`, single dict like
`paused_chat_ids`'s per-entry shape but not a list - only one owner
conversation at a time), lazily expiring after
`AGENT_PENDING_CONFIRMATION_TTL_MINUTES` (60). The *next* turn (seeded by the
owner's actual reply) gets a reminder appended to its system prompt by
`_pending_confirmation_note()` (`invoke_worker.py`, re-derived every
round-trip exactly like `builder_state`'s prompt already is). Hard,
server-side enforcement (ADR 0045 posture, not prompt-only):
`bulk_fetch_messages`'s handler (`modules/agents/tools/execution.py`)
independently re-checks `pending_confirmation` matches the call's exact
chat_id/date range and hasn't expired, and re-counts at call time in case
messages arrived in the gap - a `ToolDeniedError` either way, regardless of
what the model claims. `reset_agent_to_default` (ADR 0050) clears
`pending_confirmation` too. `docs/adr/0072-bulk-message-fetch-with-confirmation-gate.md`.

**Activation quota exceeded -> owner notice (2026-09-25, no ADR)**: unlike the
daily time budget above, exceeding the hourly activation quota does post a
generic system message ("Your agent hit its hourly activation limit and
won't respond to new messages until it resets...") into the owner's own
agent chat (`Agent.owner_agent_chat_id`), from both call sites in
`trigger_engine.py` (owner-chat direct-wake and the normal per-participant
trigger path). Gated by a `SET NX` cooldown key
(`agent_quota_notice_sent:{owner_agent_chat_id}`, TTL = the activation
window) so a burst of dropped triggers within the same hour produces exactly
one notice, not one per message. `send_system_message` is imported lazily
inside the notifier function, not at module top, to avoid a circular import
(`modules.messaging.send` already imports `evaluate_triggers` from this
module). Full detail on the equivalent token-budget-exhaustion notice (ADR
0059): `ai_agent_capacity_and_budgets.md`.
