# 0099 - Goal-driven conversational task (`start_goal_task`)

Status: Accepted (implemented)

## Context

ADR 0061's `spawn_ephemeral_task` is a one-shot relay: one outbound message,
replies are only *recorded* by the Trigger Rule Engine (no agent turn), then a
summary. ADR 0095's disposable `on_specific_chats` triggers do wake the agent
on replies, but the owner's goal lives only in the owner chat - the turn in the
third-party chat runs with the generic persona, has no stop condition, and the
Judge (ADR 0053) can reject on-task replies as off-topic. Result: "buy X from
person Y with settings Z" sends one message and dies.

## Decision

Extend the existing `on_ephemeral_task` map and `one_off_action` state (no new
agent, no new `builder_state`, no new table, no migration - JSONB only).

**Entry shape** - existing entry plus `mode: "converse"` (absent = legacy
"collect", untouched):

```python
{
  "mode": "converse", "goal": "...", "done_when": "...", "constraints": "...",
  "may_commit": False,            # owner explicitly allowed an irrevocable step
  "target_chat_id": "123",        # exactly one chat per goal task
  "owner_chat_id": "...", "turns_used": 0, "max_turns": 12, "idle_turns": 0,
  "created_at": "...", "expires_at": "...",
}
```

**Spawn** - new `one_off_action` config tool `start_goal_task(chat_id, goal,
done_when, constraints?, may_commit?, max_turns?, timeout_minutes?)`
(`resolve_user`-resolved chat_id only). Writes the entry, schedules one
immediate scoped opener turn (same `on_schedule` `execute_at="now"` path as
ADR 0061). Rules: one active goal task per chat (else
`GoalTaskConflictError`); shares `AGENT_MAX_EPHEMERAL_TASKS` and the
schedule-entry cap; `max_turns`/timeout clamped to new
`AGENT_GOAL_TASK_MAX_TURNS` (default 12) / existing
`AGENT_EPHEMERAL_TASK_MAX_MINUTES`. Owner cancel: `cancel_goal_task(task_id)`
(config tool, closes with `outcome="cancelled"`, no summary turn needed beyond
a one-line owner notice).

**Reply turns** - `trigger_engine` finds a `converse` task for the message's
chat *before* the legacy `find_task_for_chat` branch: it still honours
`blocked_read_chat_ids` / `paused_chat_ids` and consumes the hourly
`agent_activation` quota, then arms the normal debounce (ADR 0063). It does not
call `consume_matched_trigger_fire`. In `_run_turn`, an active goal task for
the chat selects (a) `scoped_system_prompt = build_goal_prompt(task)` and (b) a
restricted tool set chosen by code, never by the model: `send_message` /
`reply_message` / `read_history` / `complete_task` / `fail_task`. A hard gate in
tool dispatch denies `send_message`/`reply_message` to any chat other than
`target_chat_id` (`ToolDeniedError`) - prompt-injection from the counterpart
cannot redirect the agent. Every send still passes the unmodified
`_tool_send_message` checks and owner send budget (ADR 0058).

**Judge** - for goal-task chats the `on_topic` question is skipped (the topic
is the goal); `prompt_injection` / `info_extraction` / `code_execution` and the
ADR 0074 escalation stay active.

## Knowing when to finish (hard requirement)

The model is never left to "feel" done; termination is enforced at six layers:

1. **Explicit contract in every turn's prompt**: restates `goal`, `done_when`,
   `constraints`, "turn N of max_turns", and says each turn MUST end in exactly
   one of: reply to continue, `complete_task`, or `fail_task`. Check
   `done_when` against the latest message *before* replying.
2. **`complete_task(outcome, summary)`** - `outcome` in `achieved |
   ready_for_owner_confirmation | declined_by_counterpart`. Deletes the entry,
   schedules a summary turn into the owner chat (existing
   `fire_summary_and_complete`, now carrying status + summary). Subsequent
   messages from that chat fall back to normal trigger behavior.
3. **`fail_task(reason)`** - `cannot_achieve | counterpart_unresponsive |
   out_of_scope`. Same closure path, owner told why.
4. **No irrevocable step without permission**: unless `may_commit` is true the
   prompt forbids confirming a purchase/payment/binding agreement; the agent
   instead ends with `ready_for_owner_confirmation` and the owner finalizes.
5. **Server-side counters**: `turns_used` increments per turn; at
   `max_turns - 1` the prompt says "finalize this turn"; at `max_turns` the
   server force-closes as failed (`max_turns_reached`). A turn that ends with no
   send and no terminal call bumps `idle_turns`; two in a row force-closes.
6. **Timeout sweep**: the existing `_sweep_expired_ephemeral_tasks` closes
   lapsed `converse` tasks with a `timed_out` summary (partial progress
   included from `read_history`-derived notes in the summary turn).

The owner always receives exactly one closing message per task, whatever path
ended it.

## What does not change

Legacy "collect" tasks, `on_schedule`, `on_specific_chats` (ADR 0095),
`pause_and_escalate` behavior, token/time budgets (ADR 0059), owner-chat router
(ADR 0093; `start_goal_task` is just another `one_off_action` tool plus a short
prompt paragraph - a separate state is revisited only if that prompt degrades
simple-action accuracy).

## Implementation plan

1. New `modules/agents/goal_tasks.py` (spawn/find/close/force-close helpers,
   `build_goal_prompt`, quota + conflict errors) - all logic lives here.
2. Thin hooks: `tools/schemas.py` + `tools/config_mode.py` (`start_goal_task`,
   `cancel_goal_task`, registered in `one_off_action`), `tools/execution.py` +
   `dispatch.py` (`complete_task`/`fail_task`, goal-chat tool set + chat gate),
   `trigger_engine.py` (converse branch), `invoke_worker.py` (prompt/tool
   selection, counters, sweep), `judge.py` (skip `on_topic`),
   `builder_flow.py` (`one_off_action` prompt paragraph), `config/agent_settings.py`
   (`AGENT_GOAL_TASK_MAX_TURNS`).
3. Tests in `tests/modules/agents/` (new `test_goal_tasks.py`, plus trigger
   engine / dispatch cases): each of the six termination paths, chat-gate
   denial, one-task-per-chat conflict, Judge skip.
4. Update `.claude_docs/ai_agent.md` (tool table, trigger shapes) in the same task.
