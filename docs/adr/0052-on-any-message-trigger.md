# 0052. `on_any_message` trigger - wake on every private message to the owner

Status: Accepted

## Context

Today the only way to have the agent respond broadly is `on_specific_chats`
(opt-in per chat, empty keywords = wake on any message in *that* chat) or
`on_unknown_sender` (fires once, on the first message of a brand-new private
chat, then relies on ADR 0051's auto-registration to keep responding). There
is no single switch for "reply to every new message sent to me, in any
private chat, without adding each chat one by one" - reported directly by
the user as a missing option after the `on_specific_chats` chat-picker fix.

## Decision

New trigger key `on_any_message`, shape `{"enabled": bool}` (same shape as
`on_unknown_sender`), added to `DEFAULT_AGENT_TRIGGERS` in
`modules/agents/models.py`:

```python
DEFAULT_AGENT_TRIGGERS = {
    "on_time_window": {...},
    "on_specific_chats": {...},
    "on_unknown_sender": {...},
    "on_any_message": {"enabled": False},
    "on_schedule": [...],
}
```

**Scope: private (1:1) chats only, matching `on_unknown_sender`'s existing
scope.** Groups are excluded - a group can have many participants, and
"reply to every message in every group I'm in" is a much bigger blast
radius the owner did not ask for; group coverage stays exclusively
`on_specific_chats`-opt-in.

**Match rule:** `on_any_message` fires when the message's chat is private
(`chat.is_group is False`) and `on_any_message.enabled` is true - independent
of `on_specific_chats` (a chat already listed there still matches via the
existing path first; this is just a broader catch-all for the private chats
that aren't). It is still gated by the existing `on_time_window` check and by
`blocked_read_chat_ids` / `paused_chat_ids` / `is_enabled`, exactly like every
other trigger - no new bypass.

**Rate limiting: reuses the existing hourly `agent_activation` quota only.**
No new quota bucket (unlike `on_unknown_sender`'s extra per-sender daily
cap) - a broad "any message" trigger is expected to activate more often by
design, and the owner already has `on_time_window` + the hourly cap as
knobs; a second limiter was judged unnecessary complexity for v1.

**No ADR 0051-style auto-registration interaction needed:** since
`on_any_message` already covers every private chat while enabled, there is
nothing to promote into `on_specific_chats`.

## Consequences

- `trigger_engine._matches_trigger_config` gets a new independent check
  (parallel to `_matches_unknown_sender`, not folded into it - the two have
  different persistence semantics: `on_unknown_sender` is first-message-only
  and mutates `on_specific_chats` on match, `on_any_message` is stateless and
  never writes anything). Needs a DB round-trip for `chat.is_group` like
  `_matches_unknown_sender` does, so it's checked the same way (only if the
  cheap cache-only checks don't already match).
- `AgentTriggersOut`/`AgentTriggersIn`/`AgentUnknownSenderOut`/`In` in
  `modules/agents/schemas.py`: add `AgentAnyMessageOut`/`In` (same shape as
  the unknown-sender pair) and a required `on_any_message` field on
  `AgentTriggersOut`.
- `update_own_triggers`/`set_trigger` tool schemas (`modules/agents/tools.py`)
  document the new key so the agent can self-configure it, same as
  `on_unknown_sender`.
- Settings UI (`AgentSettingsView.js`): one new checkbox, "Reply to every new
  private message" - commits immediately like the other enabled-flags
  (`on_unknown_sender`'s toggle), not a Save/Cancel text field.
- No migration - JSONB, additive default key, existing rows read back with
  `on_any_message` implicitly `False` only if the reader defaults missing
  keys; per the September 2026 `AgentOut` validation incident (missing
  `on_time_window.start/end` 500), reads must **not** assume the key exists
  on old rows. `_merge_triggers`/`AgentTriggersOut` handle this the same way
  `on_time_window`/`on_unknown_sender` already do: `.get(..., {})` with a
  safe default in the trigger-matching code path, and a one-off backfill of
  `{"enabled": False}` onto existing agent rows (same shape as the
  `on_time_window` repair already done) so `GET /agents/me` doesn't 500 on a
  pre-existing row missing the key.
