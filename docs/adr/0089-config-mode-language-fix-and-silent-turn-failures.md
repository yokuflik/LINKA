# 0089 - Config-mode language fix for knowledge notices, and closing silent-turn-failure gaps

Status: Accepted

## Context

Two related config-mode (owner-agent chat, Supervisor/Builder/Help) bugs were
reported together in the same real session: the owner chats with their agent
in Hebrew, uploads an English-language PDF into the knowledge base, and (1)
the resulting ingestion-notice turn (ADR 0085) replies in English instead of
Hebrew, then (2) a follow-up message from the owner produces an
`agent_thinking` "started"/"tool_call" event and then nothing - no reply, no
error, ever.

### Bug 1 root cause

`_build_knowledge_contents` (`modules/agents/invoke_turn_helpers.py`) seeds
the entire turn from one string: a fixed English framing sentence plus up to
`AGENT_KNOWLEDGE_NOTICE_PREVIEW_MAX_CHARS` (4000) characters of the raw
document text (`modules/agents/router.py`), with **no chat history** -
unlike `_build_schedule_contents`, which optionally joins the target chat's
recent history. `STYLE_RULES`'s language rule (`modules/agents/
builder_flow.py`) is "reply in the same language the user is writing in; if
ambiguous, default to English" - correct in general, but this turn gives the
model no signal of the owner's actual language at all. The document's own
language is the only text present, so the model (correctly, given its input)
resolves "the language the user is writing in" to English. This is a gap in
what ADR 0085 seeds the turn with, not a prompt-compliance failure.

### Bug 2 root causes (several independent silent-failure paths, all real)

Config-mode turns (`chat_id == owner_agent_chat_id`) can currently end with
`ended_status="done"` and `agent_thinking` cleared to a terminal state while
posting **nothing** to the owner, through more than one path:

1. **Supersede-with-no-notice** (ADR 0063/00732, by design): when a new
   message arrives while a turn for the same `(agent_id, chat_id)` is
   in-flight, the running turn is marked superseded and ends silently
   (`invoke_worker.py`, three return sites) so the replacement turn can
   answer with fresh context. Working as designed for the common case, but a
   knowledge-notice turn (`message_id=None`) can never take the in-place
   merge branch - it always falls to the silent-exit branch - and the
   *replacement* turn can itself be superseded again with the same silent
   exit, chaining into what looks to the owner like the agent simply gave up.
2. **`no_reply_needed` (ADR 0065) is unguarded against genuinely new,
   undelivered content.** The "when to stay silent" condition in
   `STYLE_RULES` ("several messages arrived close together and the later
   ones didn't change anything") is broad enough to match a state where a
   knowledge summary was *supposed* to be delivered but the turn carrying it
   was superseded first - the model has no way to know a previous turn's
   output never reached the owner.
3. **`_post_config_reply` no-ops on empty text**
   (`invoke_turn_helpers.py`): if Gemini's final response yields no
   extractable text (e.g. a thought-signature-only part, or an unusual
   finish reason), the turn ends via the normal `ended_status="done"` path
   with nothing posted and no error - the same failure class the
   2026-09-24 fix (referenced in that function's own docstring) already
   addressed once, reintroduced through the empty-string guard.
4. **A broad `except Exception` in the stream consumer**
   (`realtime/fanout/base_worker.py`) logs and continues on any unhandled
   error inside `_run_turn`, with no owner-facing notice. `agent_thinking`
   is set to `"error"` in the `finally` block, but that event is
   fire-and-forget pub/sub with no replay, and the frontend
   (`poc/composables/useAgentConfig.js::applyAgentThinking`) currently
   treats `"error"` identically to `"done"` - clears the indicator, shows
   nothing.

All four are config-mode-specific in impact (the owner's own chat) - none of
this touches execution-mode/judge behavior (ADR 0053/0074/0086/0088), which
already has its own fail-open and redirect-message machinery for real
customer-facing traffic.

## Decision

### Part A - knowledge-notice turn gets a real language signal

- `_build_knowledge_contents` takes the owner-agent chat's recent message
  history (same `get_message_history`/`_format_history_transcript` call
  `_build_initial_contents` already uses) and joins it ahead of the
  knowledge-update instruction, the same optional-join shape
  `_build_schedule_contents` uses for a schedule entry's `chat_id`.
- The instruction text itself is rephrased to make the language boundary
  explicit: the document excerpt may be in any language, but the reply
  describing it to the owner must be in the language the owner has been
  using in this chat - not the document's language. This sentence is added
  to the knowledge-turn prompt built in `router.py`, not to `STYLE_RULES`
  globally (scoped to this one turn shape; every other config-mode turn
  already has real chat history to infer language from).

### Part B - close the silent-turn-failure gaps

1. **Supersede paths**: no change to the supersede-and-say-nothing behavior
   for the common in-place case (still correct - the replacement turn is
   expected to answer). What changes: the replacement turn seeded after a
   superseded knowledge-notice turn is marked so its own prompt knows a
   prior update was in progress and unreported, instead of silently
   continuing as if nothing happened - avoiding the case where the
   replacement turn also has nothing new to say and (2) below lets it go
   quiet on top of an already-unreported update.
2. **`no_reply_needed` guard**: `STYLE_RULES`'s "when to stay silent"
   condition is narrowed to explicitly exclude ending a turn silently when
   the turn was seeded to report a completed action (knowledge ingestion,
   schedule firing) that has not yet been communicated - those turns must
   always produce user-visible text, never `no_reply_needed`. Enforced at
   the prompt level (these turn types already get a distinct instruction
   sentence per Part A); server-side, `invoke_worker.py` logs (not blocks -
   consistent with every other prompt-level-only behavior in this module)
   when `no_reply_needed` is called on a knowledge/schedule-fired turn, so a
   recurrence is visible in logs instead of only in a user report.
3. **`_post_config_reply` empty-text fallback**: when a config-mode turn's
   final text is empty and the turn is not ending via `no_reply_needed` or a
   `_MESSAGE_SENDING_TOOL_NAMES` call, post a short fixed fallback line
   instead of nothing (same posture as every other budget/error notice in
   this module - always give the owner *something* rather than a silent
   stop). Logged at `WARNING` so it's traceable as a real occurrence, not a
   deliberate `no_reply_needed`.
4. **Consumer-level failure notice**: `base_worker.py`'s catch-all no longer
   fails completely silently for config-mode turns - when the failing entry
   is `kind in ("message", "schedule", "knowledge")` and its `chat_id`
   resolves to the agent's own `owner_agent_chat_id`, a short fixed-text
   error notice is posted directly (bypassing `_run_turn`, since the
   exception means that path is already broken) via the same
   `message_service.process_outgoing`/`AGENT_REPLY_MESSAGE_TYPE` shape
   `_post_config_reply` uses. Execution-mode failures (real customer chats)
   are unchanged - no new customer-facing text, consistent with the
   existing fail-open posture there.
5. **No dedicated error UI.** Explicit owner request: a real failure must
   surface as an ordinary message in the owner's own agent chat (the same
   `_post_config_reply`/`AGENT_REPLY_MESSAGE_TYPE` path every normal agent
   reply already uses), never as a distinct "error" badge/bubble in
   `AgentChatView.js`. `agent_thinking` status `"error"` keeps being treated
   identically to `"done"` on the frontend - the fixed-text notices added by
   (3) and (4) are the only owner-facing signal, and they are indistinguishable
   from any other agent message. This also means the signal only ever reaches
   the owner's own private chat, never anything customer-facing (config-mode
   notices are never possible outside `owner_agent_chat_id`).

### Not changed by this ADR

- Token-budget notice cooldown (SET-NX, ADR 0059) - investigated, confirmed
  to be a real but low-probability contributor (a second exhaustion inside
  the same window is legitimately silent by design) and left as-is; the
  fixed-text fallback in (3) above already covers the specific empty-text
  dead-end this could otherwise cause.
- Execution-mode judge/escalation behavior (ADR 0053/0074/0086/0088) -
  entirely out of scope, already has its own fail-open and notice paths.
- `.claude_docs/ai_agent.md`'s stale claim that the daily active-time budget
  exhaustion sends no notice is corrected as a documentation fix alongside
  this ADR (ADR 0079 already made it send one).

## Consequences

- Knowledge-ingestion notices now always reply in the owner's own chat
  language, at the cost of one extra history fetch per knowledge/failure
  notice turn (same cost `_build_initial_contents` already pays for every
  message-fired turn).
- Config-mode turns can no longer end completely silently from the four
  causes above - every terminal state either posts real text or leaves a
  visible error trace, without adding a new notice for the one path
  (supersede-in-place) that is working as designed.
- No schema change. No new rate-limit bucket. No new settings.
