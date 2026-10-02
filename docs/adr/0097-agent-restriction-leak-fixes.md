# 0097 - Close agent restriction leaks (global search, owner-chat trigger exemption)

Status: Accepted

## Context

Audit of ADR 0045/0066 "hard" restrictions (2026-10-02) found:

1. **`blocked_read_chat_ids` leak.** `search_messages` / `search_semantic`
   only checked the blocklist when the model passed an explicit `chat_id`.
   With no `chat_id` they ran owner-wide and returned hits from blocked chats.
2. **Backstop blocks the agent's own owner-chat replies.** The
   `trg_agents_enforce_message_restrictions` trigger (ADR 0066) checks every
   row with `sender_agent_id`, including config replies, greetings and
   notices posted into the agent's private owner chat. Turning off
   `can_send_messages` / `can_message_private` would therefore make Postgres
   refuse every message the agent sends its own owner.
3. No test exercised the triggers.

## Decision

1. Search entry points (`search_in_chat`/`search_global`, `semantic_search`)
   gain an `exclude_chat_ids` parameter applied in the SQL (`NOT IN`), passed
   by the two agent tools from `restrictions.blocked_read_chat_ids`. Reads stay
   application-enforced (no `BEFORE SELECT` in Postgres, per ADR 0066).
2. `agents_enforce_message_restrictions` returns early when
   `NEW.chat_id = agents.owner_agent_chat_id` for the sending agent. Safe:
   the send/reply tools already refuse that chat, and it is a 1:1 with the
   agent's own owner, never a third party. DDL is idempotent
   (`CREATE OR REPLACE FUNCTION`); re-run `scripts.init_db`.
3. Add DB-trigger tests that bypass the tool layer.
