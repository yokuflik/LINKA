# ADR 0100 — Split `invoke_worker.py` further: `_run_turn` into phase modules

Status: Accepted
Date: 2026-10-02

## Context

After ADR 0082 `modules/agents/invoke_worker.py` was still ~1200 lines
(CLAUDE.md Rule 9), ~670 of them the single `_run_turn` function. ADR 0082
deliberately left it whole because its mutable locals (`contents`,
`ended_status`, `gemini_call_made`, `peer_typing_task`, goal/outcome state)
cross every checkpoint. That cost is now paid explicitly: the state moves into
one dataclass instead of being threaded as parameters.

## Decision

Pure structural split, no behaviour change:

- `invoke_turn_ctx.py` — `TurnCtx` dataclass holding all former locals.
- `invoke_turn.py` — `_run_turn`: setup, typing/thinking indicators, teardown.
- `invoke_turn_pre.py` — Judge gate, owner-chat router, CLARIFY, contents seeding, goal-turn start. Each gate returns `True` when it ended the turn.
- `invoke_turn_loop.py` — `run_round_trips` / `run_round_trip` / `dispatch_tool_call`.
- `invoke_turn_steps.py` — supersede handling, Gemini budget wait, system prompt, token accounting, the three turn-ending branches, `_check_outcome_mismatch`.
- `invoke_poll_loops.py` — `_fire_schedule_entry`, ephemeral sweep, debounce + schedule poll loops.
- `invoke_worker.py` — `AgentInvokeConsumer`, `run_forever`; re-exports `_run_turn`, `_fire_schedule_entry`, `_schedule_poll_loop` (existing imports keep working).

Tests that patch a name by dotted path were retargeted to the module that
*calls* it (`evaluate_message`/`route_owner_turn`/`generate_clarify_question`
→ `invoke_turn_pre`; `evaluate_tool_outcome`/`notify_outcome_mismatch` →
`invoke_turn_steps`; `due_members`/`_fire_schedule_entry` →
`invoke_poll_loops`). `_run_turn`, `has_budget_remaining`,
`record_active_seconds` stay patched on `invoke_worker`.

## Consequences

Largest new file is 322 lines. `modules/agents` tests: 380 passed before and after.
Supersedes ADR 0082's "`_run_turn` stays in place" for `_run_turn` only.
