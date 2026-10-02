# 0096 - Notify the owner when a turn ends on a tool failure that likely left the goal unmet

Status: Accepted

## Context

Every tool-call failure (`ToolDeniedError`, or any other exception) is
already caught in `execute_tool_call`
(`modules/agents/tools/dispatch.py:46-102`) and turned into a
`{"error": ...}` dict that is fed back to Gemini as a normal tool result
(`invoke_worker.py:684`) - Gemini sees it and usually retries/adapts within
the same turn (e.g. re-resolves a misspelled name, picks a different tool).
That recovery path is correct and must not change.

The gap is the other case: the turn ends *right after* an unresolved tool
failure - the model gives up, hits the round-trip cap, or simply stops
calling tools - and the owner's underlying goal was never actually
accomplished. Today nothing distinguishes this from a normal successful
turn. The existing "turn ended with nothing delivered" fixes (ADR 0079,
ADR 0089) only cover a *different* failure shape - an empty/malformed
Gemini response with no text and no tool call at all - not "a tool call
failed and nothing came after it that fixed things."

Firing a check on every tool failure would be wrong: most failures are
recovered mid-turn by the model itself, and a judgment call on each one
would be noisy and wasteful. The check only makes sense at the point the
turn is actually ending.

## Decision

Add a narrow, turn-ending-only outcome check in `_run_turn`
(`modules/agents/invoke_worker.py`), gated by the jev classifier already
used elsewhere (ADR 0076), with notification authored by a cheap Gemini
call (ADR 0076's `AGENT_JUDGE_REDIRECT_MODEL` pattern) - no new vendor
integration.

### Detection trigger

`_run_turn` tracks the last `tool_result` dict produced in the round-trip
loop (already a local variable at the dispatch site, `invoke_worker.py:648`
- no new per-round-trip state beyond remembering it past the loop body).
The check only runs at the two points a turn can end *holding* an
unresolved error:

1. The no-function-call branch (`invoke_worker.py:542-581`, both
   config-mode and execution-mode) - when the *previous* round-trip's
   `tool_result` contained `"error"` and this round-trip produced no
   further tool call to address it.
2. The round-trip-cap branch (`invoke_worker.py:594-617`) - when the
   capped-out call itself errored, or the last successful round-trip's
   result was already an error.

Turns ending via `_TurnSuperseded`, `GeminiChatError`, `MAX_TOKENS`, or the
pre-send supersede checkpoint are excluded - those are transport/budget
failures already notice-covered by ADR 0079 and unrelated to tool outcome;
running this check there would just add noise on top of an existing notice.

### Goal text

The "goal" compared against is the turn's own seed instruction - whichever
of the triggering message's content, `schedule_instruction`, or
`knowledge_instruction` already seeded `contents` via
`_build_initial_contents`/`_build_knowledge_contents`/
`_build_schedule_contents` (`invoke_turn_helpers.py:208-249`). No new data
plumbing - this text is already a local variable in `_run_turn` before the
round-trip loop starts.

### New module: `modules/agents/outcome_judge.py`

Same shape as `attachment_judge.py` (ADR 0086) - the existing precedent for
an open-ended "does X plausibly match Y" jev question (jev's Noul questions
take free-text `instructions`/`criteria`, not just closed-vocabulary
labels):

- One Noul question, `matches_goal`, comparing the goal text against
  `f"{tool_name} failed: {tool_error}"` - "does the tool failure described
  below plausibly mean the person's request was NOT accomplished."
- Own rate bucket: `AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE` /
  `_WINDOW_SECONDS`, via `infra.ratelimit.service.check_and_increment` -
  never shares `agent_judge_calls`, `attachment_judge_calls`, or
  `agent_router_calls` (same "independent gate, independent budget"
  principle as every prior judge).
- **Fails open to silence**, not to "approved" - a judge-call failure here
  gates a notification, not an action, so the safe failure direction is
  "say nothing" rather than "nag the owner on every transient jev outage."
- Every real verdict logged to a new `AgentOutcomeJudgeLog` table
  (`modules/agents/models.py`) - same shape as `AgentAttachmentJudgeLog`:
  `id`, `agent_id` (FK, indexed), `chat_id` (indexed), `tool_name`,
  `is_match` (bool), `reason` (Text), `created_at` (indexed). A new,
  separate table, not a column on `AgentJudgeLog` - different question,
  different call site (end of turn, not pre-turn), own rate bucket, same
  reasoning ADR 0086 gave for `AgentAttachmentJudgeLog`'s own table.

### Notification

On a confirmed mismatch (`is_match=False`): a short, tool-free, history-free
Gemini call (reusing `AGENT_JUDGE_REDIRECT_MODEL`, the same cheap tier ADR
0076's redirect-text call and `clarify.py`'s question-generation call
already use) authors a short explanation of what happened, in the owner's
language. Delivered via `send_system_message` into
`agent.owner_agent_chat_id` - the same transport `escalate_chat` uses, but
**not** `escalate_chat` itself (no pause/freeze: the chat stays live, this
is informational, not a security escalation) and **not**
`_post_config_reply` (reserved for the agent's own in-persona replies).
New notice glyph `❗`, a fourth distinct visual signal alongside `🤝`
(model-initiated handoff), `⚠️` (malicious intent), `📎` (unseeable media) -
so the owner can tell at a glance this is "something you asked for didn't
happen," not a different kind of alert.

The explanation call goes through the same `_check_gemini_call_budget` gate
as any other Gemini call in the turn; if the budget is exhausted, the
mismatch is still logged to `AgentOutcomeJudgeLog` but no notification is
sent (no separate bucket for this call - it only fires after a confirmed
mismatch, already inherently rate-limited by how often that happens).

## Consequences

- Two new settings: `AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE` /
  `_WINDOW_SECONDS` (`config/agent_settings.py`).
- One new table, `AgentOutcomeJudgeLog` (`modules/agents/models.py`,
  `Base.metadata.create_all` picks it up automatically via
  `scripts/init_db.py`'s existing `modules.agents.models` import - no
  ALTER TABLE needed, this is a new table, not a new column on an existing
  one).
- One new jev call and, only on confirmed mismatch, one new cheap Gemini
  call per turn that ends on an unresolved tool error - bounded by how
  often that actually happens, not by turn volume overall.
- A fourth owner-notice glyph (`❗`) alongside `🤝`/`⚠️`/`📎`.
- No change to how tool failures are caught or fed back to Gemini
  mid-turn - recovery behavior is untouched; this only adds a check at the
  points the turn is already ending.
