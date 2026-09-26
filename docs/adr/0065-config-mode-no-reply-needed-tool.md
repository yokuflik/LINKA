# 0065 - Config-mode `no_reply_needed` tool

Status: Accepted

## Context

Config-mode turns (Supervisor/Builder/Help - `owner_agent_chat_id`) have no
`send_message`-shaped tool. Whenever a turn ends with plain text and no
function call, `invoke_worker.py::_run_turn` unconditionally posts that text
into the owner's own agent chat via `_post_config_reply` - there was no way
for the model to end a turn silently.

This becomes a real UX bug combined with ADR 0063's debounce/coalescing: the
owner sends a burst of messages, the agent already asked a question and is
waiting on an answer, and a coalesced/follow-up turn fires again for the same
`(agent_id, chat_id)` even though nothing about the pending question changed.
Since the model has no way to signal "nothing new needs to be said," it ends
up re-asking the same question or emitting filler ("OK", "Got it") - a
redundant message the owner never asked for and that muddies a chat where
they're actively waiting for the agent, not the other way around.

A prompt-only instruction ("if you have nothing new to say, don't reply")
can't work here: config-mode turns end via the `call is None` branch in
`_run_turn`, and that branch has no notion of "empty reply" - it posts
whatever text the model returns, even a single word. The model needs an
explicit way to end the turn without producing chat-visible text, the same
way `pause_and_escalate` gives it an explicit way to end an execution-mode
turn with a side effect instead of a reply.

## Decision

New no-argument config-mode tool `no_reply_needed`, available in every
`BuilderState` (Supervisor, Builder, Help Building, Help General) exactly
like every other config tool - added to `CONFIG_TOOL_SCHEMAS`/
`CONFIG_TOOL_HANDLERS` in `modules/agents/tools/config_mode.py`, then unioned
into each state's schema/handler set the same way `resolve_user` and
`spawn_ephemeral_task` already are (`schemas.py`'s `BUILDER_STATE_TOOL_SCHEMAS`,
`builder_handoff.py`'s `BUILDER_STATE_HANDLERS`).

The handler is a pure no-op: it returns `{"status": "ok"}` (something to
close the function-calling round-trip) and performs no DB write. The actual
behavior change is in `invoke_worker.py::_run_turn`: calling this tool sets a
local `skip_reply = True` flag; after the round-trip loop's normal function-
call dispatch, if `skip_reply` is set the turn ends immediately without
falling through to the `call is None` / `_post_config_reply` path - same
`ended_status = "done"` bookkeeping as every other early return, just no
message posted.

Each config-chat system prompt (`builder_flow.py`'s `STYLE_RULES`, shared by
all four states) gains one instruction: call `no_reply_needed` instead of
replying when the owner's latest message doesn't require a new response -
e.g. it already answers a question the agent itself asked and is now waiting
on, it's an acknowledgement with nothing left to add, or a coalesced batch of
messages (ADR 0063) turned out not to change anything since the agent's last
turn. The model still decides case-by-case; this only gives it a mechanism.

## What this deliberately does not change

- Execution-mode turns are untouched - they already end silently on a
  text-only reply with no tool call (`_run_turn`'s existing `call is None`
  branch only posts via `_post_config_reply` when `is_config_mode` is true).
  This ADR only closes the equivalent gap on the config-mode side.
- ADR 0063's debounce/mutex mechanism is untouched - this doesn't reduce how
  often a turn fires, only gives a fired turn a way to conclude "nothing to
  say" instead of always producing text.
- No schema/DB change. No new rate-limit bucket - `no_reply_needed` calls
  still count toward the per-turn round-trip cap like any other tool call, so
  a model that loops calling it repeatedly is still bounded by
  `AGENT_TURN_MAX_TOOL_ROUNDTRIPS`.
