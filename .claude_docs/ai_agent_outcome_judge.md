# Tool-outcome-mismatch judge (ADR 0096)

Full rationale: `docs/adr/0096-tool-outcome-mismatch-notification.md`.

## Problem this closes

Every tool-call failure (`ToolDeniedError` or any other exception) is caught
in `execute_tool_call` (`modules/agents/tools/dispatch.py:46-102`) and fed
back to Gemini as a `{"error": ...}` tool result - Gemini usually sees it and
recovers within the same turn (retries, picks a different approach). That
recovery path is untouched by this feature.

The gap was the other case: a turn ending *right after* an unresolved tool
failure, with the owner's underlying request never actually accomplished,
and nothing distinguishing that from an ordinary successful turn.

## Detection hook (`modules/agents/invoke_worker.py::_run_turn`)

A local `last_tool_error`/`last_tool_name` pair is tracked at the dispatch
site (right after `execute_tool_call`): set whenever the result dict
contains `"error"`, cleared on any subsequent successful call. This means a
failure a later round-trip recovers from never reaches either check below -
only the *last* tool outcome of the turn matters.

The check (`_check_outcome_mismatch`, module-level helper above
`_format_router_recent_turns`) only runs at the two points a turn can
actually end holding an unresolved error:

1. **No-function-call branch** (plain text or empty response ends the
   turn) - both config-mode and execution-mode.
2. **Round-trip-cap branch** - the call that hit the cap is itself the
   failure (`"tool round-trip limit reached for this turn"`), regardless of
   what the last *dispatched* call did.

Excluded on purpose: `_TurnSuperseded`, `GeminiChatError`, `MAX_TOKENS`, and
the pre-send supersede checkpoint - those are transport/budget failures,
already notice-covered by ADR 0079, unrelated to tool outcome.

## Goal text

Whichever of `knowledge_instruction` / `schedule_instruction` / the
triggering message's own `content` (re-fetched via `message_id` right where
`contents` is seeded, separate from `_build_initial_contents`'s full-history
transcript) seeded the turn - captured once into a local `goal_text` before
the round-trip loop starts, no new data plumbing.

## `modules/agents/outcome_judge.py`

Same shape as `attachment_judge.py` (ADR 0086):

- `evaluate_tool_outcome(session, agent, chat_id, *, goal_text, tool_name,
  tool_error) -> OutcomeVerdict` - one jev Noul question (`matches_goal`,
  free-text `instructions`/`criteria`, same precedent as
  `attachment_judge.py`'s `matches_request`), thresholded by
  `AGENT_OUTCOME_JUDGE_MISMATCH_THRESHOLD` (default 0.5).
- **Fails open to SILENCE** (`is_mismatch=False`), not "approved" - this
  judge gates a notification, not an action, so a broken jev call or
  exhausted rate bucket must never itself produce owner-facing noise. Empty
  `goal_text` also returns no-mismatch without calling jev, and (unlike
  `attachment_judge.py`'s empty-caption case) is **not logged** - there is
  always real seed text for a genuine turn, so this is a defensive branch,
  not a frequent real path.
- Own rate bucket: `AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE` /
  `_WINDOW_SECONDS` (`config/agent_settings.py`, default 60/min) - never
  shares `agent_judge_calls`, `attachment_judge_calls`, or
  `agent_router_calls`.
- Every **real** verdict (mismatch or not) logged to `AgentOutcomeJudgeLog`
  (`modules/agents/models.py`: `id`, `agent_id` FK indexed, `chat_id`
  indexed, `tool_name`, `is_match`, `reason`, `created_at`) - a fail-open
  verdict (rate limit, jev error) is not logged, nothing to tune from an
  outage.
- `notify_outcome_mismatch(session, agent, *, goal_text, tool_name,
  tool_error)` - on a confirmed mismatch, authors a short owner-facing
  explanation via a minimal, tool-free, history-free Gemini call (reusing
  `AGENT_JUDGE_REDIRECT_MODEL`, the same cheap tier ADR 0076's redirect call
  and `clarify.py`'s question call already use), falling back to the fixed
  `LOCAL_OUTCOME_EXPLANATION` template on any generation failure. Posted via
  `send_system_message` directly into `agent.owner_agent_chat_id` - **not**
  `escalate_chat` (no pause/freeze, this is informational) and **not**
  `_post_config_reply` (reserved for in-persona replies). New notice glyph
  `❗`, a fourth alongside `🤝` (model-initiated handoff), `⚠️` (malicious
  intent), `📎` (unseeable media).

The explanation call shares `_check_gemini_call_budget` with the rest of the
turn (no separate bucket) - if exhausted, the mismatch is still logged but
no notification is sent.

## Tests

`tests/modules/agents/test_outcome_judge.py` - the judge module in
isolation (mismatch/no-mismatch, fail-open paths, rate limit, empty goal
text, notify + its fallback). `tests/modules/agents/
test_run_turn_outcome_mismatch.py` - the `_run_turn` hook itself: confirmed
mismatch triggers the notice, no-mismatch verdict stays silent, a
**recovered** failure (error followed by a later successful call) never
reaches the check at all, a turn with no failure never calls the judge, and
the round-trip-cap branch feeds the capped call's own error through.
