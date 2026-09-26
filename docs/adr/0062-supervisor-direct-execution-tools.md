# 0062 - Supervisor gets direct execution-mode tools in the owner's own chat

Status: Accepted

## Context

The owner's chat with their own agent (`Agent.owner_agent_chat_id`) always
runs in config mode (ADR 0047 decision 4's hard `chat_id`-keyed gate,
`dispatch.py::is_config_mode`). Within config mode, ADR 0049 further
branches on `Agent.builder_state`. The default/idle state, `supervisor`, only
ever had three tools: `transfer_to_builder`, `transfer_to_help`, and
`resume_paused_chat` (ADR 0055) - a pure router with one action-shaped
exception.

This meant a direct imperative command typed straight into the owner's own
chat - "send a message to +972-5-xxx and tell me what they answer" - had no
tool to reach. `send_message`/`create_chat`/etc. are execution-mode-only
(never offered inside config mode at all, by any builder_state); Supervisor's
only path to "message someone" was the narrower `spawn_ephemeral_task`
config tool (ADR 0061), and even that wasn't wired into
`BuilderState.SUPERVISOR`'s schema list - only `BuilderState.BUILDER`'s.  The
agent correctly reported it could not do this, which is the bug report this
ADR resolves.

## Decision

`BuilderState.SUPERVISOR`'s tool schema set (`schemas.py::BUILDER_STATE_TOOL_SCHEMAS`)
and handler set (`builder_handoff.py::BUILDER_STATE_HANDLERS`) are extended to
include the **full execution-mode toolset** (`TOOL_SCHEMAS` /
`EXECUTION_TOOL_HANDLERS`), alongside its existing three tools. In effect,
when the owner talks to their agent about anything other than
configuring/building it, the agent can act exactly as if it had been invoked
from any other chat as the owner's service account - send/reply to any chat,
create a new 1:1, search, read history, leave a group, update its own
triggers, escalate - all still going through the exact same handler
functions, hard-scoped and rate-limited exactly as before (ADR 0045):
`Agent.restrictions` (`can_send_messages`, `can_message_groups`,
`can_message_private`, `can_message_new_private_contacts`,
`blocked_read_chat_ids`, `max_messages_per_day`), the daily send quota, the
owner's own shared WS send-rate budget (ADR 0058), and `is_participant`
checks. Nothing about `execute_tool_call`'s handler bodies changes - only
which handlers are reachable from the Supervisor state.

`BuilderState.BUILDER` and `BuilderState.HELP` are unaffected: the Builder
interview flow keeps its own disjoint config-tool set (unchanged), and Help
keeps its single `transfer_to_builder` escape hatch. This is deliberate -
"act as me" is specifically a Supervisor (idle/default) behavior, not
something available mid-interview or mid-explanation, where a stray
execution-shaped request would derail the flow those states exist for.

This is **not a relaxation of the hard tool-mode gate** (ADR 0047 decision 4)
- that gate is about **which chat_id** can reach config tools at all (an
external customer's chat structurally cannot, regardless of prompt
injection). This ADR only changes what one specific, already-config-mode-only
chat_id (the owner's own) can additionally do. The gate's core invariant -
selection is a pure function of `(chat_id, builder_state)`, never
`active_skill`/`system_prompt`/anything model-asserted - is preserved exactly;
this just adds one more state → tool-set mapping to the same lookup table.

## Consequences

- `pause_and_escalate` (a `CHAT_SCOPED_TOOL_NAMES` tool - its handler receives
  the *triggering* chat_id, never a model-supplied one) becomes reachable from
  Supervisor, where the triggering chat_id is always `owner_agent_chat_id`
  itself. Calling it there attempts to pause the owner's own config chat -
  nonsensical, but not a new attack surface (the handler only ever pauses the
  chat it was invoked from, same as everywhere else); worst case is a
  self-pause of the config chat, recoverable the same way any pause is
  (auto-expiry, ADR 0054, or a human `POST /agents/me/resume-chat/{id}`). Left
  as-is rather than special-cased out - not worth the complexity for a
  degenerate case the model has no motivation to hit while chatting with its
  own owner.
- `spawn_ephemeral_task` (ADR 0061) and `resolve_user` remain available from
  Supervisor as before (already `CONFIG_TOOL_SCHEMAS` entries, now joined by
  the rest of `TOOL_SCHEMAS`) - `spawn_ephemeral_task`'s multi-turn
  wait-for-reply-then-summarize shape is still useful for coordinating with
  several people at once even though a single-person "send X, tell me the
  chat_id's reply" can now also be done more simply as a direct `send_message`
  + a later `read_history` call, or by asking the owner to just wait for the
  normal trigger-driven reply to land in that chat.
- No schema/migration - purely a Python dict composition change in
  `schemas.py` and `builder_handoff.py`. No new settings, no new rate-limit
  bucket - every consumed action rides its pre-existing limit.
- `SUPERVISOR_PROMPT` (`builder_flow.py`) needs a matching update so the model
  actually knows it now has these tools when the owner asks directly (a
  system-prompt change, not a security boundary - the real enforcement is the
  handler/schema union above, exactly as ADR 0045 established for every other
  tool).
