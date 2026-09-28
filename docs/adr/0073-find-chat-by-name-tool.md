# 0073. `find_chat_by_name` config tool - free-form name lookup over the owner's own chat list

Status: Accepted

## Context

`resolve_user` (ADR 0045/0055) resolves a person by their exact `phone_number`
or `username` only - by design, ADR 0017/0024 explicitly forbid fuzzy or
substring matching against `users.username`/`display_name` at the database
level (unbounded table, anti-scraping/anti-scan rationale). But in practice
the owner talks to their agent the way they'd talk to a person: "message
Dana about tomorrow", "what did mom say earlier" - a name or nickname, not a
phone number or exact username. Today the Supervisor/Builder prompts force
`resolve_user` for this, which means the agent either fails outright or,
worse, is tempted to fall back to a message-content search
(`search_messages`/`search_semantic`) to find the right chat - the wrong
tool, since those search message bodies, not who the chat is with.

## Decision

Add **`find_chat_by_name`**, a new config-mode tool, scope and wiring
identical to `resolve_user`/`resume_paused_chat` (Supervisor + Builder
builder-states only, per ADR 0062's Supervisor union).

Unlike `resolve_user`, it takes a single free-form `name: str` and matches it
against the **titles of the owner's own chats as they'd see them in their
chat list** - never a table-wide user search:

- Group chats: `Chat.title`.
- 1:1 chats: the peer's `display_name || username || phone_number` (the same
  ADR 0024 fallback the chat-list UI uses).

The candidate set is always the owner's own bounded chat list (bounded by
`Participant.user_id`, at most `MAX_PAGE_SIZE` rows per ADR-established
convention) - fetched fresh per call, held only in memory for that one tool
call, never persisted or indexed. Matching against that already-small,
already-authorized list is done in Python (case-insensitive exact check
first, then substring / `difflib` similarity for the rest) - this is
deliberately **not** a database-level `ILIKE`/trigram search, keeping the
ADR 0017/0024 "no fuzzy matching over an unbounded users table" rule intact:
the fuzziness happens only after the data is already scoped to what the
owner is allowed to see.

Returns `{"matches": [{"chat_id", "name", "is_group"}, ...]}` - 0, 1, or
several candidates (capped, best-first). It **never picks for the model**:
the tool itself does not collapse near-ties into a single answer. The
Supervisor/Builder prompts are updated to require calling this whenever the
owner names a person by name/nickname rather than phone/username, and:

- 0 matches: say plainly no chat by that name was found, and offer
  `resolve_user` (exact phone/username) as a fallback.
- Exactly 1 match: proceed directly, no confirmation needed.
- 2+ matches: never guess - list the top candidate names back to the owner
  and ask which one they meant, before taking any action on a `chat_id`.

`resolve_user` is unchanged and still the only path for a *verified*
identity (needed before `set_trigger`/`schedule_one_off_task` target a
person by phone/username, per ADR 0055's "never a free-form name" rule for
that tool specifically). `find_chat_by_name` is for the different, more
common case: routing an in-the-moment action (`send_message`,
`read_history`, `spawn_ephemeral_task`, etc.) to the right existing chat by
what the owner calls that person, not for provisioning a new verified
trigger target.

## Consequences

- New schema `find_chat_by_name` in `CONFIG_TOOL_SCHEMAS`
  (`modules/agents/tools/schemas.py`), aliased and added to
  `BUILDER_STATE_TOOL_SCHEMAS[SUPERVISOR]` and `[BUILDER]` alongside
  `_RESOLVE_USER_SCHEMA`.
- New handler `_tool_find_chat_by_name`
  (`modules/agents/tools/config_mode.py`), registered in
  `CONFIG_TOOL_HANDLERS` and re-exported into
  `BUILDER_STATE_HANDLERS[SUPERVISOR]`/`[BUILDER]`
  (`modules/agents/tools/builder_handoff.py`), same pattern as
  `resolve_user`.
- New read helper for "chat title as the owner would see it" over
  `get_user_chats` + `get_chat_participants_with_users` (`modules/chats/`) -
  read-only, no schema change, no new endpoint.
- `SUPERVISOR_PROMPT`/`BUILDER_PROMPT` (`modules/agents/builder_flow.py`)
  updated: prefer `find_chat_by_name` over `resolve_user` when the owner
  names a person informally; on 2+ matches, ask the owner to disambiguate
  rather than picking; on 0 matches, offer `resolve_user` as the exact-match
  fallback.
- No frontend change, no migration.
