# AI Agent - Frontend (PoC) - ADR 0045 / 0046 / AGENT_DRAWER_UI_PLAN.md

Split out of `.claude_docs/ai_agent.md` on 2026-09-23 once that file passed
~300 lines (CLAUDE.md Rule 9). Backend design/state stays in `ai_agent.md`;
this file tracks the PoC frontend only. See `ai_agent.md`'s Status section
for the backend counterpart of everything referenced here.

## Current shape (drawer UI, supersedes the old centered modal)

`AgentConfigModal.js` (the original step-5 centered modal) is **deleted**
(2026-09-23) - fully superseded, no longer referenced anywhere in
`index.html`. Replaced by three components + a reshaped composable:

- **`poc/components/AgentDrawer.js`** - the shell: slides in from the right
  edge (`fixed inset-0` backdrop + `absolute inset-y-0 right-0` panel), the
  first drawer/off-canvas pattern in this codebase (every other overlay here
  is a centered `fixed inset-0 flex items-center justify-center` modal).
  Header row: back-arrow (settings view only), title, master `is_enabled`
  toggle (a real sliding switch, not a checkbox), settings-gear icon button
  (chat view only), close `×`. Body is `AgentChatView` or `AgentSettingsView`
  depending on `view`, wrapped in `opacity-50 pointer-events-none` when
  `form.is_enabled` is false (content stays mounted, not `v-if`'d away, so
  state survives toggling). Not-yet-activated state (`!form`) shows the
  single "Activate agent" CTA, same copy as the old modal.
- **`poc/components/AgentChatView.js`** - a minimal, dedicated read/compose
  loop over `agentMessages` (see below). Deliberately **not** `MessageList.js`
  - that component hard-reads `LinkaChatStore.messages`/`activeChatId`
  (the single currently-open chat's contract), and opening the agent drawer
  must never act like navigating away from whatever chat the user has open
  behind it. Also renders `thinkingStatus` as an italic "typing"-styled
  bubble.

**Opening greeting is now a real, persisted message** (2026-09-24, no ADR -
in-scope bug fix, not a new architectural decision): `create_my_agent`
(`modules/agents/router.py`) sends it via `message_service.process_outgoing`
right after the chat/agent commit, `type=AGENT_REPLY_MESSAGE_TYPE`,
`sender_id=user_id` (same shape as any agent reply - see below). Survives
reload, shows on any device, appears in `GET /chats/{id}/messages` like any
other message. The old client-side-only `makeGreetingRow()` fallback
(shown when `agentMessages` was empty, never persisted/sent) is deleted -
`greetingRow` prop removed from `AgentChatView`/`AgentDrawer`/`index.html`.

**Bubble side/color bug fix (found + fixed same session):** every message in
the owner-agent chat - the owner's own sends AND the agent's replies - has
`sender_id = owner_user_id` (the agent has no user_id of its own, ADR 0045).
`AgentChatView.js` used to key bubble side/color off
`m.sender_id === currentUser.id`, so agent replies were indistinguishable
from the owner's own messages and rendered right-aligned/teal ("mine") - a
real pre-existing bug, not limited to the greeting. Fixed by giving
agent-authored messages a distinct `Message.type` -
`AGENT_REPLY_MESSAGE_TYPE = 7` (`modules/messaging/common.py`, no migration,
same pattern as `SYSTEM_MESSAGE_TYPE = 6`) - set by
`_tool_send_message`/`_tool_reply_message` (`modules/agents/tools.py`) and
by the greeting insert above. `AgentChatView.js` now keys off `m.type === 7`
(left/gray) vs everything else (right/teal). `MessageList.js` (the main chat
pane) has no special case for type 7 - it falls through to plain-text
rendering exactly like type 1, so an agent `send_message`/`reply_message`
into a *different* chat (not the owner-agent chat) still renders normally
there, styled by that chat's own sender_id logic.
- **`poc/components/AgentSettingsView.js`** - the settings body, unchanged
  visual contract from the old modal (soft dashed-sky section on top, hard
  solid-rose section below) plus two more bordered sections added
  concurrently: **Knowledge base** (ADR 0046 decision 4) and **Your own
  Gemini key** (ADR 0046 decision 6, BYOK) - see `ai_agent.md` for the
  backend side of both. Checkboxes commit immediately on `@change`; every
  text field (`system_prompt`, `max_messages_per_day`, per-chat keyword
  lists, the BYOK key input) uses a local dirty flag + Save/Cancel buttons
  that appear only while dirty. Takes two new props (`chats`, `chatDisplayName`,
  threaded through `AgentDrawer.js` from `index.html`'s existing `ctx.chats`/
  `ctx.chatDisplayName`): the `on_specific_chats` "Chat ID to add" free-text
  box (2026-09-24 bug - stored a typed contact name as a bogus key that could
  never match a real `chat_id`, silently breaking that trigger) is now a
  `<select>` of the owner's actual private chats (excludes chats already
  watched and the agent's own owner-agent chat), and watched entries show
  the resolved `chatDisplayName` instead of a raw `Chat {id}` label. A new
  "Reply to every new private message" checkbox (ADR 0052, `on_any_message`)
  sits right below the time-window block, same immediate-commit pattern as
  the time-window toggle.
- **`poc/composables/useAgentConfig.js`** - public API is now drawer-shaped:
  `openAgentDrawer`/`closeAgentDrawer`/`showAgentDrawer`/`agentDrawerView`
  (`'chat'|'settings'`)/`openAgentSettings`/`backToAgentChat`/
  `toggleAgentEnabled` replace the old `openAgentModal`/`showAgentModal`/
  `saveAgentConfig`. Per-section save functions: `saveSoftPrompt`/
  `cancelSoftPromptEdit` (soft), `setRestrictionCheckbox` (hard checkboxes,
  immediate PATCH) + `saveHardTextFields`/`cancelHardTextEdit` (hard text,
  `max_messages_per_day`), `saveByokKey`/`cancelByokKeyEdit`/`clearByokKey`
  (BYOK). Trigger-list helpers (`addAgentChatTrigger`/
  `removeAgentChatTrigger`/`setTimeWindowField`) PATCH immediately;
  `onChatTriggerKeywordsInput`/`saveChatTriggerKeywords`/
  `cancelChatTriggerKeywordsEdit` are dirty-guarded per chat id
  (`chatKeywordsDirty: {chat_id: bool}`).
- **`AppHeader.js`**'s `@open-agent` now calls `openAgentDrawer`; entry point
  unchanged otherwise (circular avatar button left of the user's own
  name/avatar). New `agentUnreadCount: Number` prop renders a small badge
  pill on that button (same visual treatment as `ChatSidebar.js`'s
  per-chat unread pill) - the root computes it from
  `unreadCountByChatId[myAgent.owner_agent_chat_id]`.

## The agent's own chat is NOT in `LinkaChatStore`

Deliberate architectural choice (AGENT_DRAWER_UI_PLAN.md Step 4): the agent's
1:1 `owner_agent_chat_id` chat is kept **out of** `LinkaChatStore.messages`/
`activeChatId`. That ref pair is a strict single-active-chat contract
(`selectChat`'s), and opening the agent drawer must never act like navigating
away from whatever chat the user has open behind it.

- `useAgentConfig.js` owns its own small `agentMessages` ref, fetched via the
  same `GET /chats/{id}/messages?limit=50` endpoint the main chat pane uses
  (`loadAgentMessages`, called once per session on first drawer open -
  `agentMessagesLoaded` guard). Newest-last, same row shape as the main
  chat's `messages` ref.
- `onAgentChatMessage(msg)` / `onAgentChatMessageFailed(clientMessageId)`
  reconcile optimistic sends and append the agent's replies - called by
  `useWsRouter.js`'s dedicated branch (see below), not the generic
  `new_message`/`message_failed` handling.
- `sendAgentChatMessage(text)` sends a normal `send_message` WS frame
  targeting `owner_agent_chat_id` (`ctx.sendRaw`) - **no new backend
  endpoint for sending**, it rides the existing send path exactly like any
  other chat message. What makes the agent actually respond is a backend
  special case in the Trigger Rule Engine (see `ai_agent.md`'s "Owner-chat
  direct wake" entry) - the frontend send call itself is unremarkable.
- Unread badge: bumped via `store.bumpUnreadCount` (the normal store
  helper) when an agent reply arrives while the drawer isn't open; cleared
  via `store.clearUnreadCount` when `openAgentDrawer` runs. So the badge
  counter itself **is** shared state in `LinkaChatStore` (same map every
  other chat's badge uses) - only the *message list* is kept separate.

## BYOK Gemini key (ADR 0046 decision 6, frontend)

Backend (decision 5: `Agent.encrypted_gemini_api_key`, `PATCH /agents/me`'s
write-only `gemini_api_key` field, `AgentOut.has_custom_key`) is documented
in `ai_agent.md`. Frontend UI to set/clear it and show status:

- `useAgentConfig.js`: `byokKeyInput` (write-only local draft, always starts
  empty since `AgentOut` never echoes the stored key back) + `byokDirty`.
  `saveByokKey` PATCHes `{gemini_api_key: byokKeyInput.trim() || null}`;
  `clearByokKey` PATCHes `{gemini_api_key: null}` directly (enabled only
  when `form.has_custom_key`, no draft needed to clear). Both reset the
  local draft and re-clone `agentForm` from the response on success (so
  `has_custom_key` updates). `openAgentDrawer` resets `byokKeyInput`/
  `byokDirty` on every open, same as `promptDirty`/`hardTextDirty`.
- `AgentSettingsView.js`: a plain bordered "Your own Gemini key" section
  (not soft/hard-styled like the sections above it, since it isn't a
  restriction) - status line (`has_custom_key` -> "Custom key set" + inline
  Clear button, else "Using shared key"), a `type="password"` input
  (`autocomplete="off"`, masked-with-asterisks per the user's explicit
  request) for the write-only draft, Save/Cancel shown only while
  `byokDirty`.
- `AgentDrawer.js` / `index.html`: `byokDirty`/`byokKeyInput` threaded
  through as props, four events (`byok-key-input`/`save-byok-key`/
  `cancel-byok-key`/`clear-byok-key`) bubbled the same way as the other
  hard-text-field events.

## "Thinking" indicator no longer waits on the `done` event (2026-09-24)
`onAgentChatMessage` (useAgentConfig.js) now calls `applyAgentThinking(null)` itself
whenever the incoming message is agent-authored (`msg.type === 7`,
`AGENT_REPLY_MESSAGE_TYPE`) - the reply arriving IS the "turn is over" signal, more
reliable than waiting on the separate `agent_thinking {status: "done"}` event, which
travels over fire-and-forget Redis pub/sub (`realtime_service.publish_user_event`, no
persistence/replay) and can be lost if this tab's WS connection isn't subscribed at the
exact moment it's published - found via a real case where the reply posted correctly but
the indicator never cleared. `applyAgentThinking` also now sets a 30s safety-net timer on
every non-terminal update (`started`/`tool_call`), comfortably above
`AGENT_TURN_TIMEOUT_SECONDS` (20s), so a lost reply event too (not just a lost `done`)
can't leave the indicator stuck forever with no way to tell it's stale.

## `useWsRouter.js` wiring

Three new branches, added near the top of `handleWsMessage` (right after the
`ack`/`heartbeat_ack` guards, before the generic `message_failed`/
`new_message` handling they'd otherwise fall into):

1. **Agent-chat message routing** - if `msg.chat_id ===
   ctx.myAgent.value.owner_agent_chat_id` and the event is
   `new_message`/`message_failed`/`message_already_sent`, route to
   `ctx.onAgentChatMessage(Failed)` instead of the generic handling, and
   bump the badge only when the drawer isn't currently open
   (`!ctx.showAgentDrawer.value`).
2. **`agent_thinking`** -> `ctx.applyAgentThinking({status, detail})`.
   Ephemeral (never persisted/replayed on reconnect, same semantics as the
   existing `typing` event) - pushed live by
   `modules/agents/invoke_worker.py::_run_turn` at turn start, before each
   tool call, and at turn end (fixed 2s auto-clear on `done`/`error`, see
   `useAgentConfig.js::applyAgentThinking`'s `thinkingClearTimer`).
3. **`agent_config_changed`** -> `ctx.applyAgentConfigChanged(msg.agent)`.
   Cross-tab/cross-device sync, pushed by `modules/agents/router.py`'s
   `PATCH /agents/me` over the owner's personal channel (same
   `realtime_service.publish_user_event` pattern as `chat_pin_changed`/
   `chat_mute_changed`). `applyAgentConfigChanged` guards against clobbering
   an in-progress local edit: if `promptDirty`/`hardTextDirty`/a given
   `chatKeywordsDirty[chatId]` is true, that specific field is preserved
   from the local `agentForm` instead of being overwritten by the incoming
   sync: everything else (checkboxes, `is_enabled`, `has_custom_key`, etc.)
   updates live immediately, matching the "checkbox = always reflects server
   state" rule the settings view already follows.

Both push events (2 and 3) are backend-implemented and live (landed
2026-09-23, same session as this file's split) - not speculative/pre-wired
dead code.

## Older-history pagination in the drawer (2026-09-23)

`AgentChatView` originally only ever fetched the first `limit=50` page once
(`loadAgentMessages`, on first drawer open) and never paged further - long
agent conversations lost older turns from view, unlike every real chat pane
(`useChatOpen.js`'s `loadOlderMessages`/`hasMoreMessages`/scroll-threshold
trigger). Brought to parity:

- `useAgentConfig.js`: `agentHasMoreMessages`/`agentLoadingOlderMessages`
  refs + `loadOlderAgentMessages(chatId)` - same keyset convention
  (`before_id` = oldest loaded id, page size 50, `hasMore` = page came back
  full). Self-contained (no `LinkaChatStore`/cache involvement, matching the
  "kept out of the store" design above).
- `AgentChatView.js`: `hasMore`/`loadingOlder` props, `onScroll` fires
  `load-older` when scrolled within 80px of the top (guarded by `hasMore` +
  not already loading); `beforeUpdate`/`updated` capture-and-restore the
  scroll offset around a prepend so the view doesn't jump, same technique as
  `useChatOpen.js`. Auto-scroll-to-bottom on new messages now only fires
  while `pinnedToBottom` (near the bottom already), so paging up to read
  history doesn't get yanked back down by an incoming reply.
- `AgentDrawer.js` / `index.html`: `messagesHasMore`/`messagesLoadingOlder`
  props and `load-older-messages` event threaded through, same pattern as
  the other drawer props/events.

## Delivered/read ticks removed from the agent chat (2026-09-25)

`AgentChatView.js`'s per-message footer no longer renders the delivered/read
tick (`statusTickSymbol`/`statusTickClass`, the ✓✓ shown in every real 1:1
chat via `MessageList.js`) - user request: the owner's own chat with the
agent is a synchronous, always-answered loop, so a receipt tick added noise
with no real signal. `send_failed`/`pending` indicators are untouched. Purely
a `v-else-if` branch removed plus the two now-dead helper methods deleted
from the component - no store/backend change, `LinkaChatStore.statusTick*`
itself is untouched (`MessageList.js` still uses it normally).

## Typing indicator never shows an unresolved identity (2026-09-25)

**Root cause was backend, not frontend**: `_publish_peer_typing_loop`
(`modules/agents/invoke_worker.py`) sent `user_id` as a raw Python int
instead of a string, the one event on the wire not following this
codebase's Snowflake-id-as-string convention - broke every strict `===` id
check in the frontend for this event type, including the `userById` lookup
below. Fixed with `str(sender_id)` - see `ai_agent.md` for the full trace.
The two frontend hardenings below were real, defensible fixes (Rule 5
compliance, avoiding a resolve/render race) but were NOT what caused the
reported "Someone is typing" symptom - keeping them as defense-in-depth:

- `useWsRouter.js`'s `typing` handler used to call `noteUserTyping`
  immediately and only *lazily* kick off `resolveChatMemberPhones` alongside
  it when the sender wasn't yet in `ctx.userById` - so the indicator could
  render for one or more frames before the name resolved. Now, when the
  sender is unresolved, `noteUserTyping` is deferred until AFTER
  `resolveChatMemberPhones` resolves (`.then()`), instead of firing in
  parallel - the indicator only ever appears already carrying the right name.
  A short delay before "X is typing…" first appears is preferred over it ever
  showing a placeholder or raw id.
- `useChatMembers.js::userLabelById(userId)` used to `return userId` (the raw
  numeric id) as its no-user-cached fallback - violates CLAUDE.md Rule 5
  (never display `user_id`). Now falls back to `'Someone'`. With the above
  fix this path is now a rare-error-only fallback (e.g. `resolveChatMember
  Phones`'s fetch itself fails), not the common case.

## Known follow-ups (frontend)

- Owner-agent-chat avatar showing the agent's picture in its chat header/
  sidebar row - currently falls back to the colored-initial circle
  (`useChatMembers`/`useChatMeta` have no special-cased avatar resolution
  for this chat yet).
- Tool-call log UI (`AgentToolCallLog` has no read endpoint yet either -
  backend follow-up too).
- Frontend UI for `on_unknown_sender` toggle (ADR 0046 decision 2) and
  `on_schedule` entries (decision 3, add/edit/remove) - both backend-only so
  far, no `AgentSettingsView.js` section yet.
- No frontend tests exist for any of this (no test harness convention in
  this PoC generally).
