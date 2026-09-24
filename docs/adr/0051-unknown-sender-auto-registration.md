# 0051. Auto-register unknown-sender chats into `on_specific_chats`

Status: Accepted

## Context

`on_unknown_sender` (ADR 0046 decision 2) wakes the agent exactly once - on
the first-ever message in a new private chat. Nothing keeps the agent
responding to that same person afterward unless the owner manually adds the
chat to `on_specific_chats`. For a `sales_agent`-type deployment this means
every fresh lead gets one reply and then silence, which surprised the owner
in practice (reported as "it only answered the first message").

## Decision

When `_matches_unknown_sender` fires and the turn is actually enqueued (i.e.
it also clears `is_enabled`, `blocked_read_chat_ids`, the hourly activation
quota, and the per-sender daily quota - the same point the message is handed
to `enqueue_invocation`), the chat is also merged into that agent's
`on_specific_chats` with empty keywords (wake on any message, no keyword
gate) and an `_auto_added_at` UTC timestamp. This makes future messages in
that chat match via the existing `_matches_trigger_config` path - no new
trigger type, no new match logic.

**Registration is permanent** until the owner removes it manually (via
`update_own_triggers`/`set_trigger`, or the settings UI) - no expiry, no
automatic lapsing.

**Capped at `AGENT_MAX_AUTO_CHATS` (200) per agent**, counting only entries
carrying `_auto_added_at` (manually-added `on_specific_chats` entries, which
have no `_auto_added_at`, are never evicted by this mechanism and don't count
against the cap). On overflow, the entries with the oldest
`_auto_added_at` are evicted first (FIFO) until back under the cap. The
timestamp is stored explicitly rather than relying on dict/JSONB key
insertion order, so eviction order is deterministic regardless of how the
JSONB round-trips through Postgres/Python.

Manually-added `on_specific_chats` entries (set by the owner or by
`update_own_triggers`/`set_trigger` without going through this path) never
carry `_auto_added_at` and are therefore immune to both the cap and the
eviction - only auto-added-via-`on_unknown_sender` entries can push each
other out.

## Consequences

- `on_specific_chats` entries are no longer a flat `{keywords}` shape only -
  an entry may additionally carry `_auto_added_at`. Every existing reader
  (`_matches_trigger_config`) only looks at `keywords`, so this is additive
  and backward compatible; no migration needed (JSONB, no schema change).
- The write happens inside `_evaluate_triggers` (`trigger_engine.py`), right
  after the per-sender quota check passes and before `enqueue_invocation` -
  same session, same commit as the rest of that function's reads (fire-and-
  forget, wrapped by the same broad `except Exception` in `evaluate_triggers`
  so a failure here never blocks message delivery).
- No new Redis cache invalidation beyond the existing `sync_agent_cache`
  call already used by every other `Agent.triggers` write - this reuses
  `update_agent_triggers`.
- No frontend change required for the mechanism to work (the auto-added
  chat just starts appearing as a normal `on_specific_chats` entry, subject
  to the same "no chat picker" known gap already logged for that trigger
  type in `.claude_docs/ai_agent_frontend.md`).
