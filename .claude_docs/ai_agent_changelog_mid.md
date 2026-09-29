# AI Agent - changelog, middle entries (no-ADR fixes, prompt tuning, in-scope UX changes)

Split out of `.claude_docs/ai_agent_changelog.md` on 2026-09-28 (that file
re-crossed the ~300-line threshold right after its own split from
`ai_agent.md` the same day). This file holds the middle third of the
chronological log (2026-09-25 history-transcript-formatting entry through
the 2026-09-26 Trigger Rule Engine commit-bug entry). Older entries
(2026-09-23 to 2026-09-25) live in `.claude_docs/ai_agent_changelog_early.md`;
newer entries (ADR 0077 onward, 2026-09-27/28) continue in
`.claude_docs/ai_agent_changelog.md`. For ADR-backed features (schema/
tool-registry/architecture changes), see `ai_agent.md`'s own per-ADR
sections, `ai_agent_schema.md`, `ai_agent_judge_and_escalation.md`, and
`ai_agent_capacity_and_budgets.md`. Ordered oldest to newest as originally
written.

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

**`no_reply_needed` config-mode tool (ADR 0065, 2026-09-26)**: reported by the
user - in their own agent chat, after the agent already asked a question and
was waiting on an answer, a burst of their own follow-up messages (ADR 0063
debounce/coalescing) could still produce a redundant reply (re-asking the
same question, or filler like "OK") once the coalesced/re-fired turn ran.
Root cause: config-mode turns have no `send_message`-shaped tool, so
`invoke_worker.py::_run_turn`'s `call is None` branch unconditionally posted
whatever text the model returned via `_post_config_reply` - there was no way
for the model to end a turn silently, unlike execution-mode (already silent
on a text-only reply, since those personas are expected to call
`send_message`/`reply_message` themselves). Fixed with a new no-op
`no_reply_needed` tool, added to every `BuilderState`'s schema/handler set
(`modules/agents/tools/schemas.py`, `config_mode.py`, `builder_handoff.py`);
calling it makes `_run_turn` return immediately after the tool dispatch,
skipping the text-response branch entirely. `builder_flow.py`'s shared
`STYLE_RULES` prompt gained a new "When to stay silent" instruction telling
the model when to use it (already-answered pending question, a plain
acknowledgement, or a coalesced batch that changed nothing). No schema/DB
change; `no_reply_needed` calls still count toward the per-turn round-trip
cap like any other tool. 4 existing exact-handler-set tests in
`test_builder_flow.py` updated to include the new tool; 118 tests pass.

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
config tool (ADR 0055). See `ai_agent_schema.md`'s `paused_chat_ids` section
for the corrected behavior description. All 466 tests pass after the change.
