# AI Agent - changelog, newest entries (no-ADR fixes, prompt tuning, in-scope UX changes)

Split out of `.claude_docs/ai_agent.md` on 2026-09-26; split again twice on
2026-09-28 once the changelog kept re-crossing the ~300-line CLAUDE.md
threshold. This file holds the newest third of the chronological log
(ADR 0079, 2026-09-28, onward - starting with the Gemini-budget-retry
entry). Older entries: 2026-09-23 to 2026-09-25 in
`.claude_docs/ai_agent_changelog_early.md`; 2026-09-25 to 2026-09-26 in
`.claude_docs/ai_agent_changelog_mid.md`. For ADR-backed features (schema/
tool-registry/architecture changes), see `ai_agent.md`'s own per-ADR
sections, `ai_agent_schema.md`, `ai_agent_judge_and_escalation.md`, and
`ai_agent_capacity_and_budgets.md`. Ordered oldest to newest as originally
written.

**ADR 0079 (2026-09-28), user-reported: 3 messages sent back-to-back in the
owner's own agent chat got zero response, zero notice, zero UI change.**
Found two genuine silent dead-ends in `invoke_worker.py`, distinct from the
supersede-chain's by-design silence (ADR 00732/0075/0077 - a stale turn
correctly yielding to a newer one is *supposed* to say nothing):

1. `_run_turn`'s per-minute Gemini call budget check
   (`AGENT_GEMINI_CALLS_PER_MINUTE`) ended the turn on a bare `return` with
   no retry and no notice - the one exhaustion path in this file that had
   neither, unlike `GeminiChatError`/`MAX_TOKENS`/the round-trip cap/the
   outer timeout, which all post an owner-facing notice. Since this is a
   fixed window that resets within a minute on its own, giving up
   immediately was also the wrong call, not just an unreported one. Now
   retries in place: `AGENT_GEMINI_BUDGET_RETRY_SECONDS` (5) x
   `AGENT_GEMINI_BUDGET_MAX_RETRIES` (6), re-checking `is_superseded` on
   every wake-up (falls through to the normal top-of-loop supersede branch
   if a newer message landed while asleep), never consuming a `round_trip`
   slot (a `while`-style sub-loop, not a `continue` on the outer
   `for round_trip in range(...)`). Exhausting all retries now falls
   through to the same `_post_config_reply` pattern every other exhaustion
   path uses.
2. That retry's supersede-while-waiting path re-enters the top-of-loop
   ADR 0075 case-A/case-B branch, which previously used `round_trip == 0`
   as a proxy for "no Gemini call made yet" - a proxy the retry's `continue`
   silently broke (it can advance `round_trip` before any call happens).
   Replaced with an explicit `gemini_call_made` flag, set immediately before
   the one place `_generate_turn_or_supersede` is actually invoked.
3. `process_entry`'s daily active-time budget check
   (`time_budget.has_budget_remaining`) runs *before* `_run_turn` even
   starts, so it wasn't just missing a notice - the drawer's
   `agent_thinking` never fires either, nothing anywhere. New
   `time_budget.seconds_until_reset` reads the counter key's own Redis TTL
   (this window is rolling-from-first-use, TTL set once on the first
   increment of the day - not a calendar-day reset, so there's no fixed
   reset time to compute from) and a new cooldown-gated
   `_notify_daily_budget_exhausted` (same `SET NX` pattern as ADR 0059's
   token-budget notice) posts one "pause responding for up to N hours"
   notice per exhaustion event into the owner's agent chat. No retry here
   (unlike the per-minute budget, this window won't clear again soon).

**Message-batch debounce + per-chat turn mutex (ADR 0063, moved here from
`ai_agent.md` 2026-09-28):** a matched trigger no longer calls
`enqueue_invocation` directly - `trigger_engine.py` calls
`invoke_debounce.arm_debounce(agent_id, chat_id, message_id)`, which `ZADD`s
`{agent_id}:{chat_id}` onto `agent_invoke_debounce_due` scored
`now + AGENT_INVOKE_DEBOUNCE_SECONDS` (default 2s) and stashes `message_id`
in a matching `agent_invoke_debounce_msg:{agent_id}:{chat_id}` STRING. A
second match for the same pair before it fires just overwrites both (plain
`ZADD`/`SET`) - a fast burst or a self-correction ("I want blue" / "wait,
purple") coalesces into a single turn seeded from the *latest* message,
instead of racing one Gemini turn per message. All quota/permission checks
still run per-message at match time, unchanged.
`invoke_worker.py::_invoke_debounce_poll_loop` (1s tick, alongside the
existing 30s schedule-poll loop) pops due pairs and is what actually calls
`enqueue_invocation`.

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

**Superseding an in-flight turn (ADR 00732, 2026-09-27):** ADR 0063's
turn-mutex fallback (a debounced fire landing while a previous turn is
still running) only re-armed the debounce - the in-flight turn itself was
left untouched and could still call `send_message`/`reply_message` before
the mutex released, producing a stale reply followed by a second, real one.
`process_entry`'s same re-arm call site now also calls
`invoke_debounce.mark_superseded(agent_id, chat_id)`
(`agent_turn_superseded:{agent_id}:{chat_id}`, TTL =
`AGENT_TURN_TIMEOUT_SECONDS`, same self-healing bound as the turn lock).
`_run_turn` checks `is_superseded` (atomic `GETDEL`) at two points: the top
of every round-trip loop iteration, and again immediately before dispatching
`send_message`/`reply_message` specifically (closes the race where the flag
was set while that round-trip's Gemini call was already in flight). Either
hit ends the turn with no reply delivered and no re-arm (the message that
set the flag already re-armed); every other tool call already made by the
superseded turn (e.g. `read_history`) is simply wasted, not merged into the
replacement turn. **Does not attempt real Gemini request cancellation** -
confirmed via `ai.google.dev` docs that no cancellation/billing-on-early-stop
contract is documented; the underlying HTTP call runs to completion or
timeout exactly as before, this only gates whether its result ever reaches a
chat. No new owner-facing notice; `agent_thinking` stays in whatever state
it was in when superseded.

**History/search pagination + truncation notice (ADR 0067, 2026-09-26)**:
`read_history` (still 20 messages/call) and `search_messages` (still 10
results/call) now accept `before_id`/`cursor` respectively and return
`has_more` (+ `next_before_id`/`next_cursor`) instead of silently
truncating. `CHAT_STYLE_RULES` (execution-mode, `personas.py`) and
`STYLE_RULES` (config-mode, `builder_flow.py` - reachable there too since
Supervisor/Builder get the full execution toolset per ADR 0062) both
instruct the model to say plainly that there's more than it pulled in one
call whenever `has_more: true`, and offer to continue in parts, rather than
answering as if the page were the whole history/result set. Caps themselves
are unchanged; this is pagination + disclosure, not a bigger single-call
limit.

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
is clamped to `[1, AGENT_BULK_FETCH_MAX_MESSAGES]`, independent of the
shared 50 cap, since it's already gated behind `count_messages_in_range` +
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
re-execution of the tool call that already handled it. Only the trailing
run of unanswered lines (no Agent line after them yet) stays unmarked. A
matching `STYLE_RULES` rule in `builder_flow.py` tells the model to treat
marked lines as past context only, never to re-act on them.

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
`AGENT_PENDING_CONFIRMATION_TTL_MINUTES` (60). The *next* turn (seeded by
the owner's actual reply) gets a reminder appended to its system prompt by
`_pending_confirmation_note()` (`invoke_worker.py`, re-derived every
round-trip exactly like `builder_state`'s prompt already is). Hard,
server-side enforcement (ADR 0045 posture, not prompt-only):
`bulk_fetch_messages`'s handler (`modules/agents/tools/execution.py`)
independently re-checks `pending_confirmation` matches the call's exact
chat_id/date range and hasn't expired, and re-counts at call time in case
messages arrived in the gap - a `ToolDeniedError` either way, regardless of
what the model claims. `reset_agent_to_default` (ADR 0050) clears
`pending_confirmation` too. `docs/adr/0072-bulk-message-fetch-with-confirmation-gate.md`.

**Activation quota exceeded -> owner notice (2026-09-25, no ADR)**: unlike
the daily time budget above, exceeding the hourly activation quota does
post a generic system message ("Your agent hit its hourly activation limit
and won't respond to new messages until it resets...") into the owner's own
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

**Attach menu in the owner-agent chat + attachment awareness in the
transcript (2026-09-28, no ADR - UI wiring + a transcript-formatting tweak,
not a new architectural decision).** The chat `+` button in `AgentChatView.js`
was a PDF-only stub (`pick-pdf` -> `useAgentConfig.js::onAgentPdfPicked`,
which just toasted "not sent yet"). Replaced with a two-option attach menu
(mirrors `MessageInput.js`'s existing attach-menu pattern):

- **"Content file" (txt/pdf)** - shortcut into the knowledge-base pipeline
  that already existed for `AgentSettingsView.js` (`useKnowledgeUpload.js::
  uploadKnowledgeFile` - client-side PDF.js chunking, server chunking for
  txt/md, quota check, embedding). No new upload logic; `onAgentKnowledgeFilePicked`
  just calls the existing function.
- **"Attached file" (any file)** - new: sends the file as a normal media
  message into the owner-agent chat, via `useMediaUpload.js`'s existing
  `prepareMediaBlock` (upload-ticket + PUT, same bucket/pipeline as any chat
  attachment - confirmed `modules/messaging/` has zero `owner_agent_chat_id`
  special-casing). `onAgentAttachmentPicked` (`useAgentConfig.js`) builds the
  optimistic bubble into `agentMessages` and sends via the normal
  `send_message` WS action (`message_type: 5`, forced `kind='file'` so any
  file type is accepted). `AgentChatView.js`'s bubble template gained minimal
  media rendering (inline `<img>` for images, a filename+size chip
  otherwise) - it was text-only before.
- **Model awareness, no analysis**: per explicit user requirement, the agent
  must know an attachment exists (filename only) without ever fetching/
  analyzing its bytes, as future groundwork for a not-yet-built tool that
  could reference/resend it to a customer. `invoke_turn_helpers.py::
  _format_history_transcript` used to filter on `if m.content` - a
  captionless media message (the common case for a plain attachment) was
  silently dropped from the transcript entirely; a captioned one showed only
  the caption, with no attachment marker at all. Now includes any message
  with `content` **or** `media_name`, appending `[attached file: <name>]` to
  the line (filename only). Covered by new `tests/modules/agents/
  test_invoke_turn_helpers.py` (pure-function tests, no DB/Redis).

**2026-09-28, user-reported: `POST /agents/me/reset` sometimes reset the
agent but the fresh opening greeting never showed up.** Root cause in
`modules/agents/router.py::reset_my_agent` (and the same pre-existing risk
in `create_my_agent`): the reset itself (`reset_agent_to_default` +
`session.commit()`) had already landed before the greeting's
`message_service.process_outgoing(...)` call, but that call had no
try/except around it - any exception there (transient send-path error,
`NotAParticipantError`, etc.) propagated out as a 500, which the frontend
(`useAgentConfig.js::resetAgentToDefault`) surfaces as "We couldn't reset
your agent" even though the reset had actually succeeded, and the greeting
was simply lost. Both call sites now wrap `process_outgoing` in
`try/except Exception: logger.exception(...)` - best-effort, logs instead of
failing a response for state that already committed. Also translated
`_GREETING_TEXT` from Hebrew to English per user request (same string used
on both agent creation and reset).

**ADR 0089 (2026-09-28), user-reported: Hebrew owner-agent chat, uploaded an
English PDF - the knowledge-ingestion summary came back in English, then a
follow-up message got `agent_thinking` "started" and never replied at all.**
Two separate fixes.

*Language bug*: `_build_knowledge_contents` (`invoke_turn_helpers.py`) used
to seed the whole turn from a fixed English framing sentence plus the raw
document excerpt (`router.py`'s `AGENT_KNOWLEDGE_NOTICE_PREVIEW_*`-capped
preview) - **no chat history at all**, unlike `_build_initial_contents`
(message-fired) or `_build_schedule_contents`'s optional history join. With
zero signal of the owner's actual chat language, `STYLE_RULES`'s "reply in
the same language the user is writing in, default to English if ambiguous"
correctly resolved to English (the document's own language) - not a
prompt-compliance failure, a missing-context bug. Fixed by having
`_build_knowledge_contents` take `session`/`agent` and join the owner-agent
chat's own recent history (same `get_message_history`/
`_format_history_transcript` call `_build_initial_contents` already makes),
plus an explicit sentence in both `router.py` instruction strings
(`commit_knowledge_document`'s success notice and
`report_knowledge_failure`'s failure notice): the document excerpt/failure
reason may be in any language, the reply to the owner must be in the
owner's own chat language, never the document's.

*Silent-turn bug*: found and closed three independent ways a config-mode
turn (owner's own chat only) could end with `agent_thinking` clearing to a
terminal status while posting nothing:
1. `_post_config_reply` (`invoke_turn_helpers.py`) silently no-ops on an
   empty `text` argument - reachable whenever Gemini's final response has no
   extractable text (e.g. a thought-signature-only part). `invoke_worker.py`'s
   text-response branch now checks for this and posts a fixed short fallback
   line instead of calling through with an empty string, logged at
   `WARNING` so a recurrence is traceable.
2. `no_reply_needed` (ADR 0065) had no guard against being called on a
   knowledge/schedule-fired turn, which by construction has an outcome the
   owner has not been told yet - calling it there silently drops that
   report. `STYLE_RULES`'s "when to stay silent" section now explicitly
   excludes this case; `invoke_worker.py` also logs a `WARNING` if the model
   calls it anyway (prompt-level guard only, same enforcement posture as
   every other conversational instruction in this module - not a hard
   block).
3. Any unhandled exception inside `_run_turn` used to propagate out of
   `AgentInvokeConsumer.process_entry` to `BaseStreamConsumer._drain_shard`'s
   generic `except Exception: logger.exception(...)` - fully silent from the
   owner's side (the `agent_thinking "error"` event `_run_turn`'s own
   `finally` publishes is fire-and-forget pub/sub with no replay, and the
   frontend treats `"error"` identically to `"done"`). `process_entry` now
   has its own `except Exception` around the `asyncio.wait_for(coro, ...)`
   call, alongside the existing `except asyncio.TimeoutError`: posts the
   same fixed non-technical notice the timeout path already sends via
   `_post_config_reply`, then **re-raises** so the entry is still left
   unacked for reclaim/retry exactly as before - this only adds an
   owner-facing trace, it does not change delivery/retry semantics.

**Explicit owner requirement, honored across all of the above**: no
dedicated "error" UI - no badge, no distinct-colored bubble, nothing beyond
an ordinary agent chat message. A first draft added a rose-colored "⚠️
Something went wrong" bubble keyed off `thinkingStatus.status === 'error'`
in `AgentChatView.js`; reverted immediately per explicit feedback (twice) -
the user does not want any visual "error" signal shown separately from a
normal message. Every notice above is a plain `_post_config_reply` /
`process_outgoing(..., type=AGENT_REPLY_MESSAGE_TYPE)` message in the
owner's own agent chat, indistinguishable from any other agent reply -
which is also structurally why none of this can ever reach a real customer
chat (config-mode notices only ever target `agent.owner_agent_chat_id`).

Supersede-and-say-nothing itself (ADR 0063/00732/0075/0077) is unchanged -
still correct by design for the common in-place case. `.claude_docs/
ai_agent.md`'s stale claim that the daily active-time budget sends no
announcement was also corrected in place (ADR 0079 already made it send
one). Full design writeup: `docs/adr/
0089-config-mode-language-fix-and-silent-turn-failures.md`. Full agents
suite (260/260) green after landing; no new tests (same gap as most
prior agents-module prompt/notice-path fixes).

## History transcript: per-message caps + whole-message budget (2026-10-02, no ADR)

`_format_history_transcript` used to truncate the joined string with
`transcript[-4000:]`, so one long message could crowd out the rest of the 20-message
window and the cut could land mid-line (losing the `Customer:`/`Agent:` label and
`[already handled]`). Now: each message is clipped to
`AGENT_HISTORY_MESSAGE_MAX_CHARS` (1000, head+tail kept, marker points to
`read_history`); the newest customer message gets
`AGENT_HISTORY_LATEST_MESSAGE_MAX_CHARS` (3000); the total
`AGENT_HISTORY_TRANSCRIPT_MAX_CHARS` is 6000 (was 4000) and is enforced by dropping
whole oldest lines only. Tests: `tests/modules/agents/test_invoke_turn_helpers.py`.

## Message formatting knowledge for agents (2026-10-02, no ADR)
New `modules/agents/message_formatting.py::MESSAGE_FORMATTING_RULES` — the formatting the PoC renders (`poc/composables/messageFormat.js`: `*bold*`, `_italic_`, `~strike~`, ```` ```mono``` ````, `- ` bullets, `1. ` numbered; anything else shows literally) plus a "plain text by default, format only when a person would" rule. Injected into `personas.CHAT_STYLE_RULES` (all execution personas) and `builder_flow.STYLE_RULES` (all config/help states). Keep in sync with `messageFormat.js`.
