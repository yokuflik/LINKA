# AI Agent (service account, Gemini tool calling) - ADR 0045 / ADR 0046 / ADR 0047 / ADR 0049 (superseded by 0093) / ADR 0051 / ADR 0053 / ADR 0057 / ADR 0059 / ADR 0063 / ADR 0064 / ADR 0065 / ADR 0066 / ADR 0067 / ADR 0071 / ADR 0072 / ADR 0073 / ADR 00732 / ADR 0075 / ADR 0077 / ADR 0078 / ADR 0080 / ADR 0081 / ADR 0082 / ADR 0083 / ADR 0084 (superseded by 0092) / ADR 0085 / ADR 0089 / ADR 0090 / ADR 0092 / ADR 0093 / ADR 0096

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
| `.claude_docs/ai_agent_owner_chat_router.md` | ADR 0093 owner-chat jev router: contract (`route_owner_turn`, fail-frozen policy), the `one_off_action`/`clarify`/`builder_agent`/`help_*` state set and tool reassignment, `AgentRouterLog` shape, margin-tuning notes for Phase 5. |
| `.claude_docs/ai_agent_outcome_judge.md` | ADR 0096 tool-outcome-mismatch judge: the two turn-ending hook points in `_run_turn`, `modules/agents/outcome_judge.py` contract (fail-open-to-silence, own rate bucket, `AgentOutcomeJudgeLog`), the `❗` notice glyph, explanation-generation fallback. |

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
`leave_group`, `read_history`, `count_messages_in_range`,
`bulk_fetch_messages` (ADR 0072), `search_messages`,
`search_semantic` (ADR 0069), `search_knowledge_semantic` (ADR 0078),
`get_knowledge_index`, `fetch_chunk`, `list_attached_files`,
`send_attached_file` (ADR 0083), `pause_and_escalate` - 15 tools.
`update_own_triggers`/`delete_own_trigger` are config-mode-only as of ADR
0091/0095 (`update_own_triggers` was reachable here before ADR 0091 -
removed as a prompt-injection surface: an execution persona talking to a
third party could otherwise be steered into rewriting its own wake-up
triggers).
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
`Agent.builder_state` (ADR 0093, replacing ADR 0049's model-self-directed
handoff) into five sub-states, chosen deterministically by a jev router
(`modules/agents/owner_chat_router.py::route_owner_turn`) that runs once
before every config-mode turn with a real owner message - the model inside a
state never transitions `builder_state` itself anymore (all `transfer_to_*`
tools are gone). Full router contract, fail-frozen policy, and
`AgentRouterLog` shape: `.claude_docs/ai_agent_owner_chat_router.md`.

| `builder_state` | Persona prompt | Tools |
|---|---|---|
| `one_off_action` (default) | acts directly for the owner - immediate or time-delayed single actions (ADR 0093, absorbs ADR 0062's action-union role) | `resume_paused_chat` (ADR 0055), `resolve_user`, `find_chat_by_name` (ADR 0073), `spawn_ephemeral_task` (ADR 0061), `start_goal_task`/`cancel_goal_task` (ADR 0099, see `ai_agent_goal_tasks.md`), `no_reply_needed` (ADR 0065), `save_knowledge_from_text` (ADR 0078), `schedule_one_off_task`, `update_own_triggers` (ADR 0095, **disposable-only** - every entry must set `expires_at`/`max_fires`, server-enforced), `delete_own_trigger` (ADR 0095), **plus the full execution-mode toolset** (`send_message`, `reply_message`, `create_chat`, `leave_group`, `read_history`, `count_messages_in_range`, `bulk_fetch_messages`, `search_messages`, `search_semantic`, `search_knowledge_semantic`, `get_knowledge_index`, `fetch_chunk`, `list_attached_files`, `send_attached_file` (ADR 0083), `pause_and_escalate`) |
| `clarify` | zero-action; asks one disambiguating question between `one_off_action` and `builder_agent` when the router's margin is too close to call (ADR 0093) | `no_reply_needed` only |
| `builder_agent` | interviews the owner to configure persistent behavior only - the ADR 0062 execution-tool union was removed (ADR 0093) | the 6 ADR 0047 config tools minus `schedule_one_off_task` (`set_agent_persona`, `update_agent_rules`, `set_trigger`, `get_agent_status`, `estimate_api_usage`) + `set_agent_identity` (ADR 0081) + `update_own_triggers` (config-mode-only, ADR 0091; **permanent triggers only reachable here**, ADR 0095) + `delete_own_trigger` (ADR 0095) + `resolve_user` + `find_chat_by_name` (ADR 0073) + `no_reply_needed` (ADR 0065) + `finish_building_agent` |
| `help_agent_building` (ADR 0064) | explains building/configuring an agent | `no_reply_needed` (ADR 0065) only - zero knowledge-base tools (ADR 0092), zero transfer tools (ADR 0093) |
| `help_general` (ADR 0064) | explains using the Linka platform | `no_reply_needed` (ADR 0065) only - zero knowledge-base tools (ADR 0092), zero transfer tools (ADR 0093) |

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

**ADR 0084 (2026-09-28), superseded by ADR 0092 (2026-09-29):** ADR 0084 gave
both Help states read-only access to the agent's *own* per-`agent_id`
knowledge base (`search_knowledge_semantic`/`get_knowledge_index`/
`fetch_chunk`), on the assumption an owner would seed it with real product
documentation. In practice this never worked for a fresh agent: the
per-agent knowledge base is populated only by the owner manually calling
`save_knowledge_from_text`/uploading a file, so a brand-new agent (or the dev
agent, whose only two seeded documents turned out to be unrelated
computer-catalog demo PDFs for a `sales_agent` persona) had nothing to find -
`search_knowledge_semantic` returned empty and Help had to say "I don't
know" about Linka itself. It was also wiped by `POST /agents/me/reset`
(ADR 0050 deletes the knowledge base), even though Help's reference material
has nothing to do with any individual owner's config.

**ADR 0092 (2026-09-29) fix:** two static markdown files -
`docs/agent_knowledge/linka_general_help.md` and
`docs/agent_knowledge/linka_agent_building_help.md` - are read once at import
time by `modules/agents/help_docs.py` and inlined directly into
`HELP_GENERAL_PROMPT`/`HELP_BUILDING_PROMPT` (`builder_flow.py`) as a
"Reference material" section, replacing the tool-call instruction entirely.
The three knowledge-base tools were removed from both Help states'
`BUILDER_STATE_TOOL_SCHEMAS`/`BUILDER_STATE_HANDLERS` entries
(`modules/agents/tools/schemas.py`/`builder_handoff.py`), returning both to
ADR 0064's original zero-tool-call posture. No embedding, no per-`agent_id`
storage, no DB row - editing either file and restarting the app updates every
agent's Help persona at once, and `POST /agents/me/reset` can never touch it
since it isn't agent-scoped state. `SUPERVISOR`/`BUILDER` keep the three
knowledge tools unchanged (ADR 0062's execution-toolset union, untouched).
Owner-seeded per-agent knowledge bases (e.g. the dev agent's catalog docs)
remain reachable by execution-mode personas exactly as before - Help simply
no longer looks at them. Full rationale:
`docs/adr/0092-help-personas-static-inline-docs.md`.

**ADR 0085 (2026-09-28): knowledge-ingestion notice to the owner.** A
successful `POST /agents/me/knowledge` commit, and a client-side PDF parse
failure (new `POST /agents/me/knowledge/report-failure`), each enqueue a
third `agent_invoke_stream` `kind` (`"knowledge"`, alongside
`"message"`/`"schedule"`) that runs a real config-mode agent turn (always
`chat_id == owner_agent_chat_id`) reporting the outcome in the agent's own
voice - not a toast, not a fixed string. Full detail:
`docs/adr/0085-agent-knowledge-ingestion-notice.md`.

**ADR 0093 (2026-09-29): the handoff mechanism above is retired.** Every
`transfer_to_*` tool is deleted - no model-self-directed `builder_state`
transition exists anywhere anymore; a jev router decides instead. Supersedes
the Supervisor persona and relocates ADR 0062's execution-tool union onto
`one_off_action` only. Detail: `.claude_docs/ai_agent_owner_chat_router.md`;
pre-0093 handoff history: `ai_agent_changelog_early.md`/`ai_agent_changelog_mid.md`.

`get_capacity_status` (ADR 0057) was removed entirely (ADR 0091,
2026-09-29): it turned out to have never actually been wired into any
`builder_state`'s schema list (dead since introduction), and the user
confirmed it's unwanted - deleted from `config_mode.py`/`schemas.py`
rather than re-wired in.

Selection is purely `chat_id`-then-`builder_state`-driven
(`modules/agents/tools/dispatch.py::is_config_mode` + `BuilderState(agent.builder_state)`)
- never `active_skill`, `system_prompt`, or anything the model says about
itself. See ADR 0047 decision 4 (outer gate) and ADR 0093 (inner sub-states,
router-driven - supersedes ADR 0049's model-self-directed version).
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
| Tool-outcome-mismatch judge calls | 60/min per-agent (ADR 0096, own bucket) | `ratelimit:agent_outcome_judge_calls:{agent_id}` - detail: `ai_agent_outcome_judge.md` |

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

**Owner-chat jev router calls** (ADR 0093, own bucket, never shares the
Gemini-calls-per-minute budget) - rate/config detail, `AgentRouterLog` shape,
and margin-tuning notes: `.claude_docs/ai_agent_owner_chat_router.md`.
