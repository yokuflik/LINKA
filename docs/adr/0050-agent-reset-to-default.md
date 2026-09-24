# 0050 — Agent Reset to Default

Status: Accepted

## Context

The PoC's agent drawer has no way to wipe an agent back to a clean slate. Users
who have been experimenting with prompts/triggers/knowledge want a single
"start over" action instead of manually undoing each setting and deleting
messages one by one (no bulk-delete exists for a chat's messages at all today
- only the per-message `purge_message` path from ADR 0021, which is
sender-scoped and requires the message to already be soft-deleted).

## Decision

Add `POST /agents/me/reset` (owner-only, no request body):

1. **Hard-delete every message in `owner_agent_chat_id`** - both the owner's
   and the agent's own messages - via a new bulk crud helper
   `purge_all_chat_messages`. Unlike `purge_message` (ADR 0021) this is not
   sender-scoped (the caller already owns the whole chat via the agent) and
   does not require a prior soft-delete: it soft-deletes then purges each row
   in one pass. Media referenced by purged messages is deref'd and its S3
   object deleted on last ref, reusing `modules.media.crud.deref_blob` /
   `media_service.delete_object` exactly as the single-message path does.
   Irreversible - no undelete, matching the existing purge semantics.
2. **Reset all soft settings**: `system_prompt` → `""`, `triggers` →
   `DEFAULT_AGENT_TRIGGERS`, `active_skill` → `DEFAULT_AGENT_ACTIVE_SKILL`,
   `builder_state` → `DEFAULT_AGENT_BUILDER_STATE`, `paused_chat_ids` → `[]`,
   `encrypted_gemini_api_key` → `NULL` (BYOK key dropped, falls back to the
   shared `settings.GEMINI_API_KEY`).
3. **Delete the agent's knowledge base** - all `AgentKnowledgeDocument` /
   `AgentKnowledgeChunk` rows for this agent (cascades via FK), plus their S3
   objects.
4. **Reset all hard settings**: `restrictions` → `DEFAULT_AGENT_RESTRICTIONS`.
5. `is_enabled` is left untouched - resetting configuration shouldn't silently
   re-arm a paused agent, and it's neither a "hard" restriction nor a "soft"
   prompt/trigger setting.
6. `owner_agent_chat_id` itself is **kept** (not recreated) - only its
   messages are wiped. Recreating it would require re-subscribing the Rust
   `ws_gateway` and could race with any open drawer's socket subscription, for
   no user-visible benefit over an empty existing chat.
7. Same-shape response as every other agent-config endpoint: `AgentOut`
   (mirrors GET/POST/PATCH), plus the opening greeting re-sent as a genuine
   persisted message (same `process_outgoing` path as agent creation) so the
   now-empty chat isn't confusingly blank.
8. Cache/event side effects mirror PATCH: `sync_agent_cache` (Redis
   pre-filter, ADR 0046) + `agent_config_changed` personal-channel event
   (cross-tab sync, ADR 0048) + `sync_schedule_zset` (triggers.on_schedule is
   now empty, clears any due-ZSET entries).

Frontend: a "Reset agent" button in `AgentSettingsView.js`'s header, gated by
a `window.confirm` (matching the existing delete-message-forever convention,
`useMessageEdit.js`) warning the wipe is irreversible. On confirm, calls
`POST /agents/me/reset` and reloads local state from the response - same
`applyAgentConfigChanged`-shaped update `useAgentConfig.js` already uses for
the PATCH echo.

## Consequences

- New irreversible bulk-purge code path in `modules/agents/` (not
  `modules/messaging/`, since it's agent-reset-specific or use rather than a
  general chat-purge feature - no other caller needs "purge every message in
  a chat").
- Knowledge base and BYOK key are wiped even though they're technically
  "soft" (owner-authored, not security-enforced) - explicit product decision
  (2026-09-24) that "reset to default" means fully clean, not partially
  clean.
