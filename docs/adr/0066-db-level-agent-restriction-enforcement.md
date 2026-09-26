# 0066 - DB-level enforcement of Agent.restrictions

Status: Accepted

## Context

ADR 0045 introduced `Agent.restrictions` (a JSONB denylist: `can_send_messages`,
`can_message_groups`, `can_message_private`, `can_message_new_private_contacts`,
`can_leave_groups`, `blocked_read_chat_ids`) documented as a "hard,
server-enforced" boundary. In practice every check lives in Python, in
`modules/agents/tools/execution.py`'s tool handlers, immediately before each
handler calls the underlying `modules.messaging`/`modules.chats` service. This
is real enforcement against a misbehaving *prompt* (the model can't argue its
way past a Python `if`), but it is **not** enforcement against a bug: a new
tool handler that forgets the check, a future code path that calls
`process_outgoing`/`remove_participant` directly for an agent without going
through `execution.py`, or a refactor that reorders the check past the write,
would silently bypass every restriction. Nothing at the database layer would
stop it.

The owner explicitly asked for a database-level backstop that holds "no
matter what hallucination/bug the model or the code has" - i.e. defense in
depth below the application layer, not a replacement for it.

### Why not Postgres roles + Row-Level Security

The natural-looking answer - a dedicated Postgres role per agent, with RLS
policies gating INSERT/DELETE - was considered and rejected. The app's async
engine (`infra/db/connection.py`) is a single shared connection pool,
optionally behind PgBouncer in **transaction pooling** mode (`USE_PGBOUNCER`).
Connections are not sticky per request or per identity, so there is no
existing concept of "this session is agent X" at the Postgres role level, and
building one would mean either abandoning transaction pooling (defeats its
purpose at this scale) or a `SET ROLE`/re-authenticate dance per statement.
That's a much larger infra change than the actual problem calls for.

### Chosen mechanism: BEFORE triggers + `SET LOCAL`

Postgres triggers, keyed off a transaction-scoped session variable, give the
same guarantee (the database itself refuses the write) without touching the
connection/pooling model:

- `SET LOCAL app.current_agent_id = '<agent.id>'` is issued inside the same
  transaction as the write. `SET LOCAL` is scoped to the current transaction
  and is safe under PgBouncer transaction pooling (unlike a bare `SET`, which
  would leak onto whatever request the connection serves next).
- A `BEFORE INSERT`/`BEFORE DELETE` trigger function reads that setting with
  `current_setting('app.current_agent_id', true)` (the `true` makes it return
  NULL instead of erroring when unset - the common case, a human-originated
  write) and, if set, looks up that agent's `restrictions` directly from the
  `agents` table and raises if the write violates them.
- The trigger is the single source of truth's *reader*, not a second copy of
  the policy: it reads the same `agents.restrictions` JSONB the Python layer
  already reads, so there is exactly one place an owner's configured
  restriction lives.

This is strictly additive to `execution.py`'s checks, which stay as-is (they
give a clean `ToolDeniedError` back to the model without a DB round trip and
without a raw Postgres exception surfacing anywhere). The trigger is the net
that catches everything the Python check might miss.

## Decision

1. **New column** `messages.sender_agent_id BIGINT NULL REFERENCES agents(id)
   ON DELETE SET NULL`, populated by `create_message`/`process_outgoing`
   whenever the caller is an agent (never inferred from `type ==
   AGENT_REPLY_MESSAGE_TYPE`, which is a display-only signal and not reliable
   enough to gate on). This is the DB-visible fact a trigger keys on.

2. **Trigger `trg_agents_enforce_message_restrictions`** (`BEFORE INSERT ON
   messages`): if `NEW.sender_agent_id IS NOT NULL`, loads that agent's
   `restrictions`, and raises (SQLSTATE `raise_exception`, custom message
   `agent_restricted:<reason>`) if `can_send_messages` is false, or
   `NEW.chat_id` is present in `blocked_read_chat_ids`, or the chat's
   `is_group` disagrees with `can_message_groups`/`can_message_private`.
   Mirrors the same fields `_tool_send_message`/`_tool_reply_message` already
   check in Python (`modules/agents/tools/execution.py:56-68`) - the trigger
   is a second reader of the identical column, not a new policy surface.

3. **Session-variable convention**: any write path that persists an
   agent-attributed row issues `SET LOCAL app.current_agent_id = :id` at the
   start of its transaction, immediately before the row-mutating statement.
   Used by: `create_message` (send/reply), `remove_participant` (leave
   group), `get_or_create_private_chat`'s new-chat branch (new private
   contact). This is the *only* per-statement identity Postgres has - it is
   not stored anywhere, not readable outside the transaction, and never used
   for anything except these enforcement triggers.

4. **Trigger `trg_agents_enforce_leave_group`** (`BEFORE DELETE ON
   participants`): if `current_setting('app.current_agent_id', true)` is set
   and that agent's `can_leave_groups` is false, raises. `remove_participant`
   (`modules/chats/crud/crud_participant.py:396`) does a real `DELETE`, not a
   soft state change, so this is the correct statement to gate.

5. **Trigger `trg_agents_enforce_new_private_chat`** (`BEFORE INSERT ON
   participants`, restricted via `WHEN` to inserts happening inside an
   agent-attributed transaction): if `can_message_new_private_contacts` is
   false and the insert is creating a brand-new private (non-group) chat for
   the owner (not adding a member to an existing chat/group the owner already
   belongs to), raises. Distinguishing "new private chat" from "any
   participant insert" is done by checking there is no pre-existing
   1:1 chat between the same two users at trigger time, mirroring
   `get_or_create_private_chat`'s own get-or-create logic.

6. **`blocked_read_chat_ids` for reads (`read_history`/`search_messages`)
   is intentionally NOT given a DB trigger.** A `SELECT` cannot be gated by a
   row trigger (Postgres has no `BEFORE SELECT`), and RLS is exactly the
   heavier mechanism rejected above. Read restriction stays
   application-enforced only, same as today - documented here as a known,
   deliberate gap, not an oversight.

7. **`max_messages_per_day`** stays application/Redis-enforced
   (`_check_daily_send_quota`) - a rolling quota is not expressible as a
   row-level DB constraint without its own counter table and race-prone
   read-modify-write, and the existing Redis counter already serves this
   correctly; a DB trigger would add complexity without closing a real gap
   (worst case of a missed check is one over-quota message, not an
   unrestricted bypass).

## Consequences

- A bug or a new code path that skips `execution.py`'s checks now still hits
  a hard Postgres exception on `send_message`/`reply_message`/`leave_group`/
  new private chat creation - the four restriction fields with the highest
  blast radius (an agent messaging someone it shouldn't, or leaving a group
  it shouldn't). This closes the gap the owner asked about for exactly that
  scenario.
- `blocked_read_chat_ids` (read-side) and `max_messages_per_day` remain
  application-only, called out explicitly above rather than silently assumed
  covered.
- Every agent-attributed write path must remember to `SET LOCAL
  app.current_agent_id` - a new convention, documented in
  `.claude_docs/ai_agent.md`, that future write paths need to follow. Missing
  it means the trigger sees no `sender_agent_id`/no session var and treats
  the write as human-originated (fails open on the *new* backstop, but the
  existing `execution.py` check still applies) - not a regression from
  today's behavior, but worth flagging in review.
- DDL follows the exact idempotent pattern from `modules/search/ddl.py`
  (`CREATE OR REPLACE FUNCTION` / `DROP TRIGGER IF EXISTS` / `CREATE
  TRIGGER`), applied via `scripts/init_db.py` and `tests/conftest.py`, so it
  works identically on a fresh DB, the deployed DB, and the ephemeral test DB
  (ADR 0032).
