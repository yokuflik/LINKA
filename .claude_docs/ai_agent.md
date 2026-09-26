# AI Agent (service account, Gemini tool calling) - ADR 0045 / ADR 0046 / ADR 0047 / ADR 0049 / ADR 0051 / ADR 0053 / ADR 0057 / ADR 0059

Full design rationale: `docs/adr/0045-ai-agent-service-account-tool-calling.md`
(base design), `docs/adr/0046-agent-schedules-knowledge-base-and-byok.md`
(schedules, knowledge base, BYOK, pre-filter cache - extends 0045, does not
replace it), `docs/adr/0047-agent-skills-tool-mode-gate-and-escalation.md`
(paid-tier rate limits, skills/personas, hard tool-mode gate, Agentic RAG,
escalation - extends 0045/0046; all 7 decisions implemented),
`docs/adr/0049-agent-builder-supervisor-help-substates.md` (Supervisor/
Builder/Help sub-states inside the config chat - extends 0047 decision 4,
does not replace it), `docs/adr/0051-unknown-sender-auto-registration.md`
(auto-registers a chat into `on_specific_chats` after `on_unknown_sender`
fires, so the agent keeps replying to that same person - extends 0046
decision 2), and `docs/adr/0053-llm-judge-message-gate.md` (LLM Judge
pre-filter gate in front of execution-mode message-fired turns - extends
0045/0046/0047/0049/0051, does not replace any of them). ADR 0047's own
implementation log for decisions 5-7, and ADR 0053's full implementation log,
both live in their respective ADR files, per the user's explicit request to
keep implementation detail alongside the ADR once it's substantial.

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

**Standing obligation (2026-09-25, process note, not code)**: the Help Agent
(`modules/agents/builder_flow.py::HELP_PROMPT`) explains "how the system
works" purely from its static system prompt - it has no tool that reads
live code or docs. Whenever a change touches the agent system's user-facing
behavior (new/changed tools, triggers, skills, restrictions, rate limits, or
flow), the same task must also check whether `HELP_PROMPT` needs a matching
update so its explanations don't go stale - a discipline for this assistant
to apply every time the agents module changes, alongside the existing
`.claude_docs/` auto-maintenance rule in the root `CLAUDE.md`. Full detail
of past checks lives in `ai_agent_changelog.md`.

## Current tool registry

**Execution mode** (any chat except `owner_agent_chat_id`, or a
schedule-fired turn): `send_message`, `reply_message`, `create_chat`,
`leave_group`, `read_history`, `update_own_triggers`, `search_messages`,
`get_knowledge_index`, `fetch_chunk`, `pause_and_escalate` - 10 tools.

**Config mode** (`chat_id == owner_agent_chat_id` only) further branches on
`Agent.builder_state` (ADR 0049) into three disjoint sub-sets:

| `builder_state` | Persona prompt | Tools |
|---|---|---|
| `supervisor` (default) | routes only | `transfer_to_builder`, `transfer_to_help`, `resume_paused_chat` (ADR 0055) |
| `builder_agent` | interviews the owner | the 6 ADR 0047 config tools (`set_agent_persona`, `update_agent_rules`, `set_trigger`, `get_agent_status`, `estimate_api_usage`, `schedule_one_off_task`) + `resolve_user` + `resume_paused_chat` (ADR 0055) + `transfer_to_help` + `finish_building_agent` |
| `help_agent` | explains the system | `transfer_to_builder` |

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
