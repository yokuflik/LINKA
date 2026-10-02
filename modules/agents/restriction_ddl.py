"""DB-level backstop for Agent.restrictions (ADR 0066).

Applied verbatim by both `scripts/init_db.py` (fresh / deployed dev DB) and
`tests/conftest.py` (the ephemeral per-run test DB, ADR 0032), same
convention as `modules/search/ddl.py` (ADR 0040). All statements are
idempotent (`IF NOT EXISTS` / `OR REPLACE` / `DROP ... IF EXISTS`).

This is a defense-in-depth backstop, NOT a replacement for the checks in
modules/agents/tools/execution.py - those still run first and give a clean
ToolDeniedError back to the model. These triggers exist so that a bug, a
missed check in a future tool handler, or any code path that writes an
agent-attributed row without going through execution.py still gets refused
by Postgres itself, unconditionally.

Two building blocks make this possible:
- `messages.sender_agent_id` - a real column recording which agent (if any)
  authored a message row (see modules/messaging/models.py). Never inferred
  from `type == AGENT_REPLY_MESSAGE_TYPE`, which is a display-only signal.
- `SET LOCAL app.current_agent_id = '<id>'` - issued by the write path
  (modules/messaging/send.py, modules/chats/membership.py,
  modules/chats/creation.py) inside the same transaction as the row
  mutation. Transaction-scoped, so it's safe under PgBouncer transaction
  pooling (a bare `SET` would leak onto the connection's next, unrelated
  request). `current_setting(..., true)` returns NULL instead of erroring
  when unset - the common case, a human-originated write.
"""

from sqlalchemy import text

_MESSAGE_RESTRICTIONS_FUNCTION = """
CREATE OR REPLACE FUNCTION agents_enforce_message_restrictions() RETURNS trigger AS $$
DECLARE
    r JSONB;
    owner_chat_id BIGINT;
    chat_is_group BOOLEAN;
BEGIN
    IF NEW.sender_agent_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT restrictions, owner_agent_chat_id INTO r, owner_chat_id
    FROM agents WHERE id = NEW.sender_agent_id;
    IF r IS NULL THEN
        RETURN NEW;
    END IF;

    -- ADR 0097: the agent's own owner<->agent chat is never a third party;
    -- config replies/greetings/notices must keep flowing to the owner.
    IF owner_chat_id IS NOT NULL AND owner_chat_id = NEW.chat_id THEN
        RETURN NEW;
    END IF;

    IF COALESCE((r->>'can_send_messages')::boolean, true) IS FALSE THEN
        RAISE EXCEPTION 'agent_restricted:can_send_messages';
    END IF;

    IF r ? 'blocked_read_chat_ids' AND
       (r->'blocked_read_chat_ids') @> to_jsonb(NEW.chat_id::text)
    THEN
        RAISE EXCEPTION 'agent_restricted:blocked_read_chat_ids';
    END IF;

    SELECT is_group INTO chat_is_group FROM chats WHERE id = NEW.chat_id;
    IF chat_is_group IS TRUE AND COALESCE((r->>'can_message_groups')::boolean, true) IS FALSE THEN
        RAISE EXCEPTION 'agent_restricted:can_message_groups';
    END IF;
    IF chat_is_group IS FALSE AND COALESCE((r->>'can_message_private')::boolean, true) IS FALSE THEN
        RAISE EXCEPTION 'agent_restricted:can_message_private';
    END IF;

    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

_LEAVE_GROUP_FUNCTION = """
CREATE OR REPLACE FUNCTION agents_enforce_leave_group() RETURNS trigger AS $$
DECLARE
    agent_id_setting TEXT;
    r JSONB;
BEGIN
    agent_id_setting := current_setting('app.current_agent_id', true);
    IF agent_id_setting IS NULL OR agent_id_setting = '' THEN
        RETURN OLD;
    END IF;

    SELECT restrictions INTO r FROM agents WHERE id = agent_id_setting::bigint;
    IF r IS NULL THEN
        RETURN OLD;
    END IF;

    IF COALESCE((r->>'can_leave_groups')::boolean, true) IS FALSE THEN
        RAISE EXCEPTION 'agent_restricted:can_leave_groups';
    END IF;

    RETURN OLD;
END
$$ LANGUAGE plpgsql
"""

_NEW_PRIVATE_CHAT_FUNCTION = """
CREATE OR REPLACE FUNCTION agents_enforce_new_private_chat() RETURNS trigger AS $$
DECLARE
    agent_id_setting TEXT;
    r JSONB;
    chat_is_group BOOLEAN;
    existing_participant_count INTEGER;
BEGIN
    agent_id_setting := current_setting('app.current_agent_id', true);
    IF agent_id_setting IS NULL OR agent_id_setting = '' THEN
        RETURN NEW;
    END IF;

    SELECT is_group INTO chat_is_group FROM chats WHERE id = NEW.chat_id;
    IF chat_is_group IS NOT FALSE THEN
        -- Group chat (or chat row not found yet) - never gated by
        -- can_message_new_private_contacts, which only concerns 1:1 chats.
        RETURN NEW;
    END IF;

    -- A brand-new private chat has exactly the row being inserted right now
    -- as its only participant so far (get_or_create_private_chat adds both
    -- sides back-to-back in the same transaction) - a pre-existing 1:1 chat
    -- being re-joined would already have participants.
    SELECT count(*) INTO existing_participant_count
    FROM participants WHERE chat_id = NEW.chat_id;
    IF existing_participant_count > 0 THEN
        RETURN NEW;
    END IF;

    SELECT restrictions INTO r FROM agents WHERE id = agent_id_setting::bigint;
    IF r IS NULL THEN
        RETURN NEW;
    END IF;

    IF COALESCE((r->>'can_message_new_private_contacts')::boolean, true) IS FALSE THEN
        RAISE EXCEPTION 'agent_restricted:can_message_new_private_contacts';
    END IF;

    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

RESTRICTION_DDL: tuple[str, ...] = (
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender_agent_id BIGINT "
    "REFERENCES agents(id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_messages_sender_agent_id ON messages (sender_agent_id) "
    "WHERE sender_agent_id IS NOT NULL",

    _MESSAGE_RESTRICTIONS_FUNCTION,
    "DROP TRIGGER IF EXISTS trg_agents_enforce_message_restrictions ON messages",
    "CREATE TRIGGER trg_agents_enforce_message_restrictions "
    "BEFORE INSERT ON messages "
    "FOR EACH ROW EXECUTE FUNCTION agents_enforce_message_restrictions()",

    _LEAVE_GROUP_FUNCTION,
    "DROP TRIGGER IF EXISTS trg_agents_enforce_leave_group ON participants",
    "CREATE TRIGGER trg_agents_enforce_leave_group "
    "BEFORE DELETE ON participants "
    "FOR EACH ROW EXECUTE FUNCTION agents_enforce_leave_group()",

    _NEW_PRIVATE_CHAT_FUNCTION,
    "DROP TRIGGER IF EXISTS trg_agents_enforce_new_private_chat ON participants",
    "CREATE TRIGGER trg_agents_enforce_new_private_chat "
    "BEFORE INSERT ON participants "
    "FOR EACH ROW EXECUTE FUNCTION agents_enforce_new_private_chat()",
)


async def apply_restriction_ddl(conn) -> None:
    """Run every statement in `RESTRICTION_DDL` on an open async connection.

    Call it after `messages`/`participants`/`agents` all exist (the message
    trigger attaches to `messages` and cascades to its partitions; the
    participants triggers attach to the plain, unpartitioned `participants`
    table directly).
    """
    for stmt in RESTRICTION_DDL:
        await conn.execute(text(stmt))
