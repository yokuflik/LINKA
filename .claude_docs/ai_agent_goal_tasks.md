# AI agent - goal-driven conversational tasks (ADR 0099)

`start_goal_task` (ONE_OFF_ACTION config tool) runs a multi-turn conversation with ONE person until a stated goal is reached, then reports to the owner and deletes itself. Extends ADR 0061's `on_ephemeral_task` (legacy "collect" entries untouched).

## Code map
- `modules/agents/goal_tasks.py` - all logic: `spawn_goal_task`, `find_goal_task_for_chat(triggers, chat_id)` (active = `mode=="converse"`, `target_chat_id` match, unexpired), `build_goal_prompt`/`build_closing_prompt`, `begin_goal_turn`, `record_turn_outcome`, `close_goal_task` (the single closing path).
- `modules/agents/tools/goal_task_schemas.py` (leaf) + `goal_task_tools.py` - `start_goal_task`/`cancel_goal_task` (config, in ONE_OFF_ACTION schema+handler sets) and `GOAL_TASK_HANDLERS` (`send_message`/`reply_message`/`read_history` chat-gated to the task chat, `complete_task`, `fail_task`).
- Hooks: `tools/dispatch.py` (goal branch in `get_tool_schemas_for_chat`/`execute_tool_call`, decided from `agent.triggers` + `chat_id` only), `trigger_engine.py` (goal match wakes the agent, bypasses time window/keywords, zeroes the generic trigger flags), `invoke_worker.py` (`begin_goal_turn`, goal prompt, terminal-tool exit, idle/last-turn accounting, sweep branch), `judge.py` (`goal_task_active` skips `on_topic`; malicious flags stay), `builder_flow.py` (ONE_OFF_ACTION prompt bullet).

## Entry shape (`Agent.triggers["on_ephemeral_task"][task_id]`)
`mode:"converse"`, `goal`, `done_when`, `constraints`, `may_commit`, `target_chat_id`, `owner_chat_id`, `turns_used`, `max_turns`, `idle_turns`, `created_at`, `expires_at`. One active task per chat; shares `AGENT_MAX_EPHEMERAL_TASKS` + schedule cap.

## Termination (every path -> `close_goal_task` -> exactly one owner summary turn)
1. Prompt contract every turn (goal, done_when, "turn N of max").
2. `complete_task(outcome: achieved|ready_for_owner_confirmation|declined_by_counterpart)`; 3. `fail_task(reason)`.
4. `may_commit=false` (default): prompt forbids binding commitments -> ends `ready_for_owner_confirmation`.
5. Server caps: `AGENT_GOAL_TASK_MAX_TURNS` (20; last-turn prompt demands a terminal call, no terminal call on last turn = force-closed), `AGENT_GOAL_TASK_MAX_IDLE_TURNS` (2 consecutive turns with no send).
6. Timeout: `_sweep_expired_ephemeral_tasks` (converse branch, status `timed_out`); owner cancel via `cancel_goal_task`.

## Known limits
Forced closes (cap/timeout) report only a canned reason - the closing turn has no target-chat history. Terminal-tool success ends the turn immediately. A successful `send_message`/`reply_message` no longer ends the turn (2026-10-02): the model may still call `complete_task`/`fail_task` in the same turn (a closing "deal!" message otherwise left the task open and the owner never notified); a 2nd send in the turn is refused in `dispatch_tool_call`, and a turn ending without a terminal call records the idle/turn-cap outcome via `finish_without_call`.
