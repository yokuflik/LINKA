# 0055. Explicit `resume_paused_chat` config tool

Status: Accepted

## Context

ADR 0054 made escalation pauses auto-expire and added one implicit resume
path: any owner message in their own agent chat resumes the
most-recently-escalated paused chat. That heuristic breaks down with more
than one paused chat at once - the owner has no way to say "no, unblock the
chat with *this* customer specifically" from inside the agent chat itself.
The only precise tool today is the human-only REST endpoint
(`POST /agents/me/resume-chat/{chat_id}`), reached through a UI action, not
through conversation.

## Decision

Add **`resume_paused_chat`**, a new config-mode tool (ADR 0047 decision 6
catalog), reachable only in the Supervisor/Builder builder-states (same
scope as `resolve_user`/`set_trigger`). It takes a `phone_number` or
`username` (mirroring `resolve_user`'s contract exactly - never a free-form
name), resolves it to a real chat_id via the existing `resolve_user` logic,
and un-pauses that chat specifically via the existing `resume_agent_chat`
(the same human-only write path `POST /agents/me/resume-chat/{chat_id}`
already uses - this tool is just a second, conversational entry point into
it, not a new mutation).

This is **additive, not a replacement**: ADR 0054's most-recent-pause
resume-on-any-owner-message still fires unconditionally first in
`_evaluate_triggers`. `resume_paused_chat` exists for the case that
heuristic doesn't cover - multiple concurrent pauses, or a case where the
owner's message wasn't really "about" the freshest escalation.

If the resolved chat_id isn't actually in `paused_chat_ids` (nothing to
resume, or already expired/resumed), the tool returns a plain "not
currently paused" result rather than an error - a routine outcome the model
relays conversationally, same convention as `resolve_user`'s
`{"found": false}`.

## Consequences

- New tool schema `resume_paused_chat` in `CONFIG_TOOL_SCHEMAS`
  (`modules/agents/tools.py`), handler wired into both
  `BuilderState.SUPERVISOR` and `BuilderState.BUILDER` dispatch tables
  (`_BUILDER_STATE_HANDLERS`/`_BUILDER_STATE_TOOL_SCHEMAS`) - reachable as
  soon as the owner is in their agent chat, without requiring a full
  Builder interview first (unlike most config tools it's not a setup step,
  it's an action).
- Reuses `resume_agent_chat` (`modules/agents/crud.py`) unchanged - no new
  DB write path, no schema change.
- `sync_agent_cache` called after resume, same as every other trigger/pause
  mutation, so the pre-filter cache doesn't serve a stale paused chat_id.
- `BUILDER_PROMPT`/`SUPERVISOR_PROMPT` get a short instruction: if the owner
  asks to resume/unblock the agent for a specific person, call
  `resolve_user` then `resume_paused_chat`, and tell them plainly if that
  chat wasn't actually paused.
- No frontend change required - reachable purely through the existing agent
  chat conversation.
