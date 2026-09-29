# AI Agent - Schema & Restrictions/Triggers Detail

Split out of `ai_agent.md` 2026-09-28 (file kept re-crossing the ~300-line
split threshold). This file holds the full `Agent`/`AgentToolCallLog`/
`AgentKnowledgeDocument`/`AgentKnowledgeChunk` schema, the `restrictions` and
`triggers` JSONB shapes and every field's behavior, the DB-level restriction
backstop (ADR 0066), and the ADR 0078 knowledge-base ingestion/retrieval
detail. See `ai_agent.md` for the current-state index, tool registry, and
rate limits; `ai_agent_history.md`/`ai_agent_changelog.md` for build history.

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
    # encrypted_gemini_api_key removed (ADR 0090) - BYOK (ADR 0046 decision 5) is gone entirely, always uses the shared key
    active_skill: str           # persona catalog key (ADR 0047 decision 3), default "one_off_executor"
    paused_chat_ids: dict       # JSONB list, default [] (ADR 0047 decision 5) - escalated chats; ADR 0054: list of {chat_id, paused_at, expires_at}, auto-expires after AGENT_ESCALATION_PAUSE_HOURS (default 24) or on human resume/owner-chat reply
    builder_state: str          # "supervisor"|"builder_agent"|"help_general"|"help_agent_building" (ADR 0049/0064), default "supervisor" - only meaningful in the config chat
    agent_name: str | None      # ADR 0081, owner-set display name, prompt-only (never surfaced in UI/typing indicator) - None = agent stays generic ("Linka Agent")
    disclose_as_agent: bool     # ADR 0081, default False (matches today's implicit full-impersonation) - True lets the agent truthfully admit being AI/bot if directly asked
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
  "blocked_read_chat_ids": []
}
```
- **`max_messages_per_day` removed 2026-09-28** (no ADR - not an
  architectural/schema decision, just dropping an optional never-set-by-
  default field): the cumulative daily send cap was a soft, opt-in
  restriction (default `null`/unlimited) enforced by
  `_check_daily_send_quota` in `modules/agents/tools/common.py`, called from
  every send-capable tool in `execution.py`. Removed at explicit user
  request: helper + call sites deleted, field dropped from
  `DEFAULT_AGENT_RESTRICTIONS`/`AgentRestrictionsOut`/`AgentRestrictionsIn`,
  the `get_capacity_status` tool no longer reports it, and the settings-UI
  "Max messages / day" field + its whole Save/Cancel `hardTextDirty` flow in
  `useAgentConfig.js`/`AgentSettingsView.js`/`AgentDrawer.js` are gone.
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
`search_messages`) is deliberately NOT given a DB trigger (no
`BEFORE SELECT` in Postgres) - stays application-only, a known/accepted gap,
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
