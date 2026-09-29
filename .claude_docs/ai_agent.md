# AI Agent (service account, Gemini tool calling) - ADR 0045 / ADR 0046 / ADR 0047 / ADR 0049 / ADR 0051 / ADR 0053 / ADR 0057 / ADR 0059 / ADR 0063 / ADR 0064 / ADR 0065 / ADR 0066 / ADR 0067 / ADR 0071 / ADR 0072 / ADR 0073 / ADR 00732 / ADR 0075 / ADR 0077 / ADR 0078 / ADR 0080 / ADR 0081 / ADR 0082 / ADR 0083 / ADR 0084 / ADR 0085 / ADR 0089 / ADR 0090

**BYOK removed (ADR 0090, 2026-09-29):** ADR 0046 decision 5/6 (owner-supplied
Gemini API key) is gone entirely - no DB column, no schema fields, no
endpoint logic, no frontend UI. Every mention of BYOK below and in the
changelog/history files describes a since-removed feature; every agent turn
always uses the shared `settings.GEMINI_API_KEY`.

`docs/adr/0082-invoke-worker-domain-split.md`: `modules/agents/invoke_worker.py`
(1165 lines) split by responsibility. `_run_turn`/`AgentInvokeConsumer`/
`_fire_schedule_entry`/both poll loops/`run_forever` stayed in
`invoke_worker.py` (patched-by-dotted-path in tests, or structurally central
to the file's own docstring); the Gemini-call/contents-building helpers
(`_pending_confirmation_note`, `_post_config_reply`, `_format_history_
transcript`, `_build_initial_contents`, `_build_schedule_contents`,
`_check_gemini_call_budget`, `_generate_turn_or_supersede`,
`_TurnSuperseded`) moved to `modules/agents/invoke_turn_helpers.py`; the
owner-notification/typing-indicator helpers (`_publish_agent_thinking`,
`_publish_peer_typing_loop`, `_notify_token_budget_exhausted`,
`_notify_daily_budget_exhausted`) moved to `modules/agents/invoke_notify.py`.
No behaviour change - references to these helper names elsewhere in this
file/the changelog files as "in `invoke_worker.py`" predate the split.

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
posting anything - extends 0047/0049/0063, does not replace them), and
`docs/adr/0080-agent-marks-messages-read-on-turn-start.md` (execution-mode,
message-fired turns call `message_service.mark_as_read` for the triggering
message at the same gate as the Judge call in `invoke_worker.py::_run_turn`,
right before the read tick's peer-visible `typing` indicator starts -
unconditional call, ADR 0003 privacy/1:1-scoping already enforced downstream
by `modules/receipts/apply.py`, no new logic in the agent). ADR 0047's
own implementation log for decisions 5-7, and ADR 0053's full implementation
log, both live in their respective ADR files, per the user's explicit request
to keep implementation detail alongside the ADR once it's substantial.

**This file is the current-state index/reference** (schema, defaults, tool
registry, rate limits). Detailed build history and per-change narrative
moved out into topic files as this file kept re-crossing the ~300-line
split threshold:

| File | Contents |
|---|---|
| `.claude_docs/ai_agent_schema.md` | Full `Agent`/`AgentToolCallLog`/`AgentKnowledgeDocument`/`AgentKnowledgeChunk` schema; `restrictions` default + DB-level backstop triggers (ADR 0066); `triggers` default + every field's behavior; `paused_chat_ids` shape/resume flow; no-code-execution rule; ADR 0078 knowledge-base ingestion/semantic-retrieval detail. |
| `.claude_docs/ai_agent_history.md` | ADR 0045 steps 1-5 + ADR 0046 decisions 1-6 build log (schema, Trigger Rule Engine, worker, tool registry/Gemini client, config API, pre-filter cache, `on_unknown_sender`, `on_schedule`, knowledge base/RAG, BYOK), plus the AGENT_DRAWER_UI_PLAN.md Wave 2 backend pieces. |
| `.claude_docs/ai_agent_changelog_early.md` | Chronological changelog, part 1/3 (2026-09-23 to 2026-09-25): `tools.py` package split (ADR 0056), peer-visible typing indicator + its int/string bug, `agent_thinking` drawer leak fix, ADR 0049 (Supervisor/Builder/Help) build detail, ADR 0047 all-decisions summary, persona tone fixes, self-triggering-loop bug, config-mode reply-not-posted bug, Gemini model bump, ADR 0050 reset-to-default, ADR 0051 auto-registration, Supervisor→Help handoff. |
| `.claude_docs/ai_agent_changelog_mid.md` | Chronological changelog, part 2/3 (2026-09-25 to 2026-09-26): history-transcript formatting, mid-turn handoff bug, BYOK disable, all-7-prompts tone rewrite, internal-id masking, `no_reply_needed` (ADR 0065), Trigger Rule Engine commit bug. |
| `.claude_docs/ai_agent_changelog.md` | Chronological changelog, part 3/3, newest (ADR 0079 / 2026-09-28 onward): eager supersede + token penalty (ADR 0077), ADR 0079 budget-retry/notice, message-batch debounce/turn-mutex (ADR 0063), in-flight-turn supersede (ADR 00732), history/search pagination (ADR 0067), per-call `limit` arg, history `[already handled]` marker (ADR 0071), bulk-fetch confirmation gate (ADR 0072), activation-quota-exceeded notice. |
| `.claude_docs/ai_agent_judge_and_escalation.md` | ADR 0053 LLM Judge gate (model, fail-open, redirect-message, `AgentJudgeLog`); `pause_and_escalate` behavior/notices (talk-to-a-human trigger fix, formatted handoff notice, customer-facing transfer reply); identity-masking detail; `resolve_user` config tool. |
| `.claude_docs/ai_agent_capacity_and_budgets.md` | ADR 0057 (`get_capacity_status` tool + `peek_fixed_window`), ADR 0058 (agent shares owner's WS send-message sliding-window budget + `peek_sliding_window`), ADR 0059 (5h/500k + 7d/3M rolling token-usage windows, output-token cap, `GET /agents/me/usage`, `UsageProgressBar.js`). |
| `.claude_docs/ai_agent_frontend.md` | AI agent PoC frontend: `AgentDrawer.js`/`AgentChatView.js`/`AgentSettingsView.js`, `useAgentConfig.js`, BYOK frontend, peer-visible typing indicator frontend detail, the chat attach menu (content file / attached file). |
| `.claude_docs/ai_agent_frontend_usage_and_search.md` | AI agent PoC frontend, split out of `ai_agent_frontend.md`: the token-usage ring+popover widget (`UsageProgressBar.js`, ADR 0059), in-chat search wiring, known frontend follow-ups. |

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
`get_knowledge_index`, `fetch_chunk`, `list_attached_files`,
`send_attached_file` (ADR 0083), `pause_and_escalate` - 14 tools.
`search_messages`/`search_semantic` both take optional `start_date`/`end_date`
(ADR 0068), which now also accept a specific time of day, not just a calendar
date (ADR 0070).

**ADR 0083 (2026-09-28): resend an owner-attached file.** `list_attached_files`
lists media messages the owner personally sent into their own owner-agent
chat (`{file_id, filename, caption, kind, mime, size_bytes, uploaded_at}`,
newest first) - the groundwork the 2026-09-28 "Attached file" attach-menu
entry (`ai_agent_frontend.md`) was explicitly built for. `caption` is the
`Message.content` the owner typed alongside the attachment (nullable) - the
signal the model actually needs to match a vague ask like "a picture of the
computer" against an opaque filename. `send_attached_file(chat_id, file_id,
caption?)` resends one into a real target chat, reusing the same
`can_send_messages`/`blocked_read_chat_ids`/group-private/quota gate as
`send_message`, then `process_outgoing(..., media={key, name,
duration_seconds, blur_hash}, sender_agent_id=agent.id)` - `_validate_media`
re-HEADs the object and re-bumps `media_blob.ref_count`, the same dedup-safe
mechanics a client-side forward (ADR 0020) already uses. Hard security
boundary: `file_id` is looked up via `get_message_by_id(session,
agent.owner_agent_chat_id, file_id)` with `chat_id` fixed from the `Agent`
row (never model-supplied) and `sender_id == agent.owner_user_id` re-verified
at call time - so the tool can never resend a knowledge-base document or a
media message from any *other* chat the agent can merely read. New crud
query `modules/messaging/crud.py::list_attached_files`.

Both tools sit in the plain `TOOL_SCHEMAS` list, so they were always reachable
by every real (customer-facing) persona with no extra wiring - execution mode
is "any chat except `owner_agent_chat_id`" regardless of `active_skill`
(`dispatch.py::get_tool_schemas_for_chat`). `CHAT_STYLE_RULES`
(`modules/agents/personas.py`, shared by every execution persona) now
explicitly instructs the model to call `list_attached_files` whenever the
other person asks for a photo/file/document, send immediately on one clear
match, ask which one on multiple matches, and never claim to send something
that isn't there.

**Config mode** (`chat_id == owner_agent_chat_id` only) further branches on
`Agent.builder_state` (ADR 0049, ADR 0064) into four sub-sets (supervisor/
builder_agent deliberately overlap with execution mode, per ADR 0062 below;
neither Help state does):

| `builder_state` | Persona prompt | Tools |
|---|---|---|
| `supervisor` (default) | routes, AND acts directly for the owner (ADR 0062) | `transfer_to_builder`, `transfer_to_help_building`, `transfer_to_help_general` (ADR 0064), `resume_paused_chat` (ADR 0055), `resolve_user`, `find_chat_by_name` (ADR 0073), `spawn_ephemeral_task` (ADR 0061), `no_reply_needed` (ADR 0065), `save_knowledge_from_text` (ADR 0078), **plus the full execution-mode toolset** (`send_message`, `reply_message`, `create_chat`, `leave_group`, `read_history`, `update_own_triggers`, `search_messages`, `search_semantic`, `search_knowledge_semantic`, `get_knowledge_index`, `fetch_chunk`, `list_attached_files`, `send_attached_file` (ADR 0083), `pause_and_escalate`) |
| `builder_agent` | interviews the owner, AND acts directly for the owner too (2026-09-26) | the 6 ADR 0047 config tools (`set_agent_persona`, `update_agent_rules`, `set_trigger`, `get_agent_status`, `estimate_api_usage`, `schedule_one_off_task`) + `set_agent_identity` (ADR 0081) + `resolve_user` + `find_chat_by_name` (ADR 0073) + `resume_paused_chat` (ADR 0055) + `no_reply_needed` (ADR 0065) + `save_knowledge_from_text` (ADR 0078) + `transfer_to_help_building` + `transfer_to_help_general` (ADR 0064) + `transfer_to_supervisor` + `finish_building_agent`, **plus the full execution-mode toolset** (same 14 tools as supervisor) |
| `help_agent_building` (ADR 0064) | explains building/configuring an agent | `transfer_to_builder`, `transfer_to_help_general`, `transfer_to_supervisor`, `no_reply_needed` (ADR 0065), `search_knowledge_semantic`, `get_knowledge_index`, `fetch_chunk` (ADR 0084, read-only) |
| `help_general` (ADR 0064) | explains using the Linka platform | `transfer_to_help_building`, `transfer_to_supervisor`, `no_reply_needed` (ADR 0065), `search_knowledge_semantic`, `get_knowledge_index`, `fetch_chunk` (ADR 0084, read-only) |

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

**ADR 0084 (2026-09-28):** both Help states stayed pure prompt-knowledge
under ADR 0064 - anything not hand-written into `HELP_GENERAL_PROMPT`/
`HELP_BUILDING_PROMPT` had to be answered "I don't know." Gave both **read-only**
access to the agent's own knowledge base (`search_knowledge_semantic`,
`get_knowledge_index`, `fetch_chunk` - the same execution-mode handlers,
`modules/agents/tools/builder_handoff.py`/`schemas.py`), so an owner can seed
their own agent's knowledge base (`save_knowledge_from_text` or a file
upload, same mechanism as any other reference doc) with real product
documentation and have their Help personas answer from it. Both prompts now
instruct the model to call `search_knowledge_semantic` first (fallback to
`get_knowledge_index`+`fetch_chunk`) before answering, and to never mention
the lookup or quote saved content verbatim. `save_knowledge_from_text` stays
off both Help states' tool sets (read-only, no write) - ADR 0064's
zero-action posture is otherwise unchanged (no execution/config tool
reachable from either Help state), and knowledge stays hard-scoped per
`agent_id` as everywhere else - no shared/global knowledge store across
different owners' agents.

**ADR 0085 (2026-09-28): knowledge-ingestion notice to the owner.** A
successful `POST /agents/me/knowledge` commit, and a client-side PDF parse
failure (new `POST /agents/me/knowledge/report-failure`), each enqueue a
third `agent_invoke_stream` `kind` (`"knowledge"`, alongside
`"message"`/`"schedule"`) that runs a real config-mode agent turn (always
`chat_id == owner_agent_chat_id`) reporting the outcome in the agent's own
voice - not a toast, not a fixed string. Full detail:
`docs/adr/0085-agent-knowledge-ingestion-notice.md`.

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

Full `Agent`/`AgentToolCallLog`/`AgentKnowledgeDocument`/`AgentKnowledgeChunk`
models, `restrictions`/`triggers` default shapes and field behavior, the
DB-level restriction backstop (ADR 0066), and the ADR 0078 knowledge-base
ingestion/retrieval detail all moved to `.claude_docs/ai_agent_schema.md`
(2026-09-28 split). Quick field reference kept here:

```python
class Agent(Base):
    __tablename__ = "agents"
    # id, owner_user_id (FK users, UNIQUE), owner_agent_chat_id (FK chats),
    # system_prompt, restrictions (JSONB), triggers (JSONB), is_enabled,
    # active_skill, paused_chat_ids (JSONB),
    # builder_state, agent_name, disclose_as_agent, created_at, updated_at
    # (encrypted_gemini_api_key removed by ADR 0090 - BYOK is gone)

class AgentToolCallLog(Base):
    __tablename__ = "agent_tool_call_log"

class AgentKnowledgeDocument(Base):
    __tablename__ = "agent_knowledge_documents"

class AgentKnowledgeChunk(Base):
    __tablename__ = "agent_knowledge_chunks"
```

Full column list, JSONB default shapes (`restrictions`, `triggers`,
`paused_chat_ids`), the DB-level restriction backstop (ADR 0066), the
no-code-execution rule, and ADR 0078's knowledge-base ingestion/semantic
retrieval: **`.claude_docs/ai_agent_schema.md`**.

## Rate limits / budgets (all via `infra/ratelimit`, ADR 0012)

| Limit | Scope | Mechanism |
|---|---|---|
| Gemini API calls | 30/min per-agent (ADR 0047 decision 1) | `agent_gemini_calls:{agent_id}` |
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
dormant until the daily window resets - `process_entry` does call
`_notify_daily_budget_exhausted` (real, SET-NX-cooldown-gated owner notice)
before skipping a due entry; see `ai_agent_changelog.md`.

**Message-batch debounce/turn-mutex (ADR 0063), in-flight-turn supersede
(ADR 00732), history/search pagination (ADR 0067), per-call `limit` arg,
history `[already handled]` marker (ADR 0071), bulk-fetch confirmation gate
(ADR 0072), the activation-quota-exceeded owner notice, and ADR 0089
(config-mode language fix + silent-turn-failure gaps)** all moved to
`.claude_docs/ai_agent_changelog.md` (2026-09-28 split, this file kept
re-crossing the ~300-line threshold).
