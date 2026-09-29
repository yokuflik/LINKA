# ADR 0082 — Split `modules/agents/invoke_worker.py`'s helper groups into sibling modules

Status: Accepted
Date: 2026-09-28

## Context

`modules/agents/invoke_worker.py` had grown to ~1165 lines. CLAUDE.md Rule 9
flags files over ~300 lines. Rule 7 requires an ADR before a significant
structural change. Same shape of problem as `modules/agents/tools.py` before
ADR 0056.

`_run_turn` (~500 lines) is a single sequential state machine threading
several pieces of mutable local state across its whole body (`contents`,
`api_key`, `gemini_call_made`, `ended_status`, `peer_typing_task`,
`owns_typing_indicator`, `config_mode_turn`) and multiple supersede/budget
checkpoints (ADR 0063/0074/0075/0077/00732) whose ordering relative to each
other is load-bearing and documented inline. Breaking it into smaller
functions would not reduce complexity - it would move the same mutable state
across function boundaries via extra parameters/return values, adding
indirection without removing risk. `_run_turn` is left untouched, in place.

Existing tests (`test_invoke_debounce.py`, `test_invoke_worker_schedule.py`)
patch several names directly on the `modules.agents.invoke_worker` module
object (`_run_turn`, `_fire_schedule_entry`, `due_members`,
`record_active_seconds`, ...). `AgentInvokeConsumer.process_entry` and the two
poll loops (`_invoke_debounce_poll_loop`, `_schedule_poll_loop`) call these
names as bare module-level references, so `unittest.mock.patch` swapping the
attribute on the `invoke_worker` module works precisely because the call site
and the patched name live in the same module. Moving `_fire_schedule_entry`
(or anything else patched this way) to a different module would silently
break these patches unless `invoke_worker.py` re-imports the name into its
own namespace - the same hazard ADR 0056's facade re-export pattern exists to
avoid, but riskier here since these are private per-test patches rather than
a public API. To sidestep this entirely, only helpers that nothing patches by
dotted path move out.

## Decision

Keep `AgentInvokeConsumer`, `_run_turn`, `run_forever`,
`_invoke_debounce_poll_loop`, `_schedule_poll_loop`, and `_fire_schedule_entry`
in `invoke_worker.py` (patched-by-name or structurally central). Extract the
remaining, independent helper groups into new sibling modules under
`modules/agents/`:

| Module | Moved from `invoke_worker.py` |
|---|---|
| `invoke_turn_helpers.py` | `_pending_confirmation_note`, `_post_config_reply`, `_TOOL_THINKING_LABELS`, `_format_history_transcript`, `_build_initial_contents`, `_build_schedule_contents`, `_check_gemini_call_budget`, `_generate_turn_or_supersede`, `_TurnSuperseded`, `_SUPERSEDE_POLL_SECONDS` |
| `invoke_notify.py` | `_publish_agent_thinking`, `_publish_peer_typing_loop`, `_PEER_TYPING_REFRESH_SECONDS`, `_MESSAGE_SENDING_TOOL_NAMES`, `_notify_token_budget_exhausted`, `_notify_daily_budget_exhausted`, `_TOKEN_BUDGET_EXHAUSTED_NOTICE`, `_ROUND_TRIP_CAP_NOTICE` |
| `invoke_worker.py` (stays) | `_run_turn`, `AgentInvokeConsumer`, `_fire_schedule_entry`, `_sweep_expired_ephemeral_tasks`, `_invoke_debounce_poll_loop`, `_schedule_poll_loop`, `run_forever` - imports everything above by name |

No `__init__.py` package facade (unlike ADR 0056) - `invoke_worker.py` stays a
plain module and simply imports the extracted names, since nothing outside
this file imports the extracted helpers directly (verified: only
`invoke_worker.py` itself and `_run_turn`'s docstring reference them). No
behaviour change, no schema/DB change. Pure `git mv`-shaped extraction +
import rewrite, same pattern as ADR 0013/0022/0056.
