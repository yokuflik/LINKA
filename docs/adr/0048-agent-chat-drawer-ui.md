# 0048 - Agent chat drawer UI, live status, and cross-tab sync

Status: Accepted

## Context

The AI agent (ADR 0045/0046) had a config-only centered modal
(`AgentConfigModal.js`) with no way to actually talk to the agent from the
UI, no live feedback while it's processing a turn, and no cross-tab sync
when config changed on another device. The user asked for a proper chat
surface: a slide-in drawer combining live chat with the agent's own
dedicated chat plus its settings, a live "thinking…" indicator, an unread
badge synced across devices, and (layered on afterwards) a masked BYOK
Gemini-key field. Full UI plan: `AGENT_DRAWER_UI_PLAN.md` (superseded by
this ADR + `.claude_docs/ai_agent_frontend.md` once implemented).

Work was split into two waves because ADR 0046/0047 were being implemented
concurrently by other sessions against the same backend files
(`invoke_worker.py`, `router.py`, `trigger_engine.py`): Wave 1 (frontend
only) landed first; Wave 2 (the backend pieces below) landed once this
became the only active session.

## Decision

1. **Drawer, not modal.** `AgentDrawer.js` slides in from the right edge -
   the first off-canvas pattern in this codebase (every other overlay is a
   centered `fixed inset-0` modal). Same single entry point as before (the
   circular avatar button in `AppHeader.js`, left of the user's own
   avatar), but it now toggles a drawer instead of a modal.

2. **The agent's own chat is kept out of `LinkaChatStore`.**
   `LinkaChatStore.messages`/`activeChatId` is a strict single-active-chat
   contract (`selectChat`'s). Opening the agent drawer must never act like
   navigating away from whatever chat the user has open behind it, so
   `useAgentConfig.js` owns a separate `agentMessages` list, fetched via the
   same `GET /chats/{id}/messages` REST endpoint the main pane uses. Only
   the unread-badge *counter* stays in the shared store (same map every
   other chat's badge uses) - just not the message list itself.

3. **`send_message` reused for talking to the agent; no new send endpoint.**
   The compose box in `AgentChatView.js` sends a normal `send_message` WS
   frame targeting `owner_agent_chat_id`. What makes the agent actually
   respond is decision 6 below (the trigger engine special case) - the send
   path itself needed no changes.

4. **Save semantics split by field type**, per explicit user instruction:
   checkboxes/toggles (`is_enabled`, every `restrictions.can_*`, trigger
   add/remove) commit immediately on change; every text field
   (`system_prompt`, `max_messages_per_day`, per-chat keyword lists, the
   BYOK key input) uses a local dirty flag + explicit Save/Cancel buttons.

5. **Live "thinking" status and cross-tab config sync, over the existing
   personal-channel primitive.** Two new WS event types, both fanned out via
   `realtime.realtime_service.publish_user_event` - the same mechanism
   `chat_pin_changed`/`chat_mute_changed` already use, confirmed to need
   **zero Rust `ws_gateway` changes** (`fanin.rs::handle_user_event` already
   forwards any personal-channel event's raw JSON verbatim to every one of
   the user's live connections):
   - `agent_config_changed` - published by `PATCH /agents/me` after every
     successful patch, full `AgentOut` payload. Guarded on the frontend
     (`applyAgentConfigChanged`) against clobbering an in-progress local
     text edit (dirty-field check per section).
   - `agent_thinking` - published by `invoke_worker.py::_run_turn`: once at
     turn start, once before each tool dispatch (short human label per tool
     name), and exactly once at turn end via a `try/finally` (covers every
     early-return path - disabled agent, BYOK decrypt failure, Gemini
     budget exhausted, call failure, round-trip cap, plain-text response).
     Ephemeral - never persisted, no new rate limit needed (bounded by the
     turn's own existing round-trip cap).

6. **Owner-chat direct-wake special case in the Trigger Rule Engine.**
   The agent's dedicated 1:1 chat has only the owner as a participant, so
   the existing trigger loop (which only ever evaluates *other*
   participants' agents) never enqueued an invocation for a message sent
   into it - talking to your own agent from the drawer would otherwise
   silently do nothing. `trigger_engine.py::_evaluate_triggers` now checks,
   before the normal loop, whether the message's `chat_id` is some agent's
   `owner_agent_chat_id` (`crud.get_agent_by_owner_chat`) and its sender is
   that agent's owner; if so, it wakes the agent directly - bypassing
   `on_specific_chats`/`on_time_window`/keyword gating entirely (a message
   here is always an explicit, deliberate wake) - but still subject to
   `is_enabled` and the existing hourly `agent_activation` quota, same as
   any other trigger match.

7. **BYOK Gemini key, masked on display.** Layered onto the settings view
   after the drawer shipped, per explicit user request: a `type="password"`
   input for the write-only key draft (the server never echoes the stored
   key - `AgentOut.has_custom_key: bool` is the only signal), status line
   ("Custom key set" / "Using shared key"), Save/Cancel/Clear. No new
   backend needed - ADR 0046 decision 5 already shipped `PATCH /agents/me`'s
   `gemini_api_key` field.

8. **New-agent-only default-restrictions change**, confirmed explicitly by
   the user (CLAUDE.md Rule 10 - security-relevant): `can_message_groups`
   defaults to `false` for newly created agents (was `true`). No migration
   or backfill - existing agents keep whatever value they already have,
   matching this repo's no-migrations convention.

## Consequences

- No Rust `ws_gateway` changes were needed for the two new WS event types -
  confirmed by reading `crates/ws_gateway/src/fanin.rs` rather than assumed.
- The agent's chat history is now fetched via two different code paths
  depending on which chat: `LinkaChatStore`'s selectChat flow for every
  normal chat, and `useAgentConfig.js`'s dedicated `loadAgentMessages` for
  the agent's own chat. This is deliberate (see decision 2) but is a
  divergence future contributors should not try to "clean up" by merging
  the agent chat into the store without re-reading this ADR's reasoning.
- `AgentConfigModal.js` is deleted (fully unreferenced after the drawer
  replaced it); no other component followed the old centered-modal-only
  pattern for this feature, so there is no lingering dead code from the
  earlier design.
- Frontend detail lives in `.claude_docs/ai_agent_frontend.md` (split out of
  `.claude_docs/ai_agent.md` once that file passed ~300 lines); backend
  detail (the trigger engine special case, the two new publish call sites,
  the default-restrictions change) stays in `ai_agent.md`.
