# AI Agent - Frontend: usage indicator, in-chat search, known follow-ups

Split out of `.claude_docs/ai_agent_frontend.md` on 2026-09-28 (that file kept
re-crossing the ~300-line CLAUDE.md split threshold). Core drawer/chat/
BYOK/attach-menu frontend detail stays in `ai_agent_frontend.md`; this file
holds the token-usage indicator (ADR 0059), in-chat search (2026-09-26), and
the running "known follow-ups" list.

## Usage indicator: ring + popover, not an always-visible bar (ADR 0059, 2026-09-26)

`poc/components/UsageProgressBar.js` is a self-contained widget - a small
circular ring button (SVG `stroke-dasharray`/`stroke-dashoffset`, filled by
the 5h window's percentage only, colored teal/amber/rose by percent/blocked)
that lives in `AgentDrawer.js`'s header row (both chat and settings views,
next to the Reset/toggle buttons - not scoped to `view === 'chat'` like the
composer is). Clicking it toggles a small popover (own `popoverOpen` local
state, closed on any outside click via a `document` click listener added in
`mounted`/removed in `beforeUnmount`) containing both linear progress bars
("Session (5h)" / "Weekly (7d)"), each with a percentage and a live "Resets
in HH:MM:SS" countdown. **This popover is the only place either window's
percentage/bar is shown, per explicit user requirement** - the ring itself
never surfaces a number, and the 7d window is never shown anywhere else. The
countdown ticks client-side off a `now` timer (`setInterval`, 1s, cleared in
`beforeUnmount`) anchored to `_fetchedAtMs` (a non-server field stamped onto
the response by `loadAgentUsage` at fetch time) so it stays accurate between
polls without extra network traffic.

**Frozen-state banner shows an exact clock time, not a countdown**:
`AgentChatView.js` (which already disables the composer while
`usageBlocked`) also takes a `usage` prop and computes `freezeEndsAt` -
whichever blocked window resets furthest in the future (the one actually
still gating the composer) converted to an absolute `Date`, ticked by the
same 1s-interval pattern. The inline note above the composer reads "Usage
limit reached - frozen until HH:MM" (or "D Mon, HH:MM" if the reset falls on
a different day) instead of a vague "try again later" - this is the one
exception to the popover-only rule above, since it's a single derived
instant, not the percentage/bar detail itself.

`useAgentConfig.js`: `agentUsage` ref (`{window_5h, window_7d} | null`) and
`agentUsageBlocked` computed (true if either window's `is_blocked`).
`loadAgentUsage()` fetches `GET /agents/me/usage`; `startAgentUsagePolling`/
`stopAgentUsagePolling` wrap it in a 30s `setInterval`, following the
closure-variable-timer idiom already used by `useWebsocket.js`'s
`heartbeatTimer`/`useMediaUpload.js`'s `recordingTimer` (store the timer id,
clear it explicitly) rather than `useChatStore.js`'s fire-once-never-cleared
`setInterval` - this one is scoped to the drawer being open, not the app's
whole lifetime. Started in `openAgentDrawer`, stopped in `closeAgentDrawer`
and `resetAgentConfig` (logout teardown). This is the first "poll a REST
endpoint on an interval" pattern in `poc/` - usage isn't pushed over WS from
any single call site the way `agent_thinking`/`agent_config_changed` are, so
polling was the more direct route.

When `agentUsageBlocked` is true, `AgentChatView.js` disables the textarea,
the attach button, and the send button (`:disabled`, greyed out) and shows a
short inline note. UX convenience only - the real enforcement is
`invoke_worker.py`'s server-side pre-flight gate (see `ai_agent.md`).

## Search inside the agent's own chat (2026-09-26, no ADR)

Reuses the existing message-search stack (ADR 0040/`search.md`) unmodified -
the owner-agent chat is a real `chat_id` with the owner as its sole
participant, so `GET /chats/{owner_agent_chat_id}/messages/search` and
`.../messages/around/{id}` work with zero backend change. Frontend-only wiring:

- **`AgentDrawer.js`** - new magnifying-glass button in the header row (chat
  view only, next to the settings-gear icon), emits `open-search`.
- **`index.html`** - `@open-search="openSearchModal(myAgent &&
  myAgent.owner_agent_chat_id)"`: opens the same `SearchModal`/`useSearch.js`
  used everywhere else, scoped to that chat id exactly like `ChatHeader`'s
  `open-chat-search`.
- **`useSearch.js`'s `openSearchResult`** - the one real special case. A
  normal hit's chat is in `LinkaChatStore` and goes through
  `ctx.jumpToMessage` (`selectChat` + `/messages/around/{id}`). The agent
  chat is deliberately kept **out** of the store (see `ai_agent_frontend.md`),
  so `jumpToMessage` can't target it - `openSearchResult` now checks
  `result.chat_id === ctx.myAgent.value.owner_agent_chat_id` first and, if so,
  calls `ctx.openAgentDrawer()` (reopens the drawer on top of whatever chat
  is open behind it) then `ctx.jumpToAgentMessage(chat_id, id)` instead.
- **`useAgentConfig.js`'s `jumpToAgentMessage`/`agentHighlightedId`** - mirrors
  `useChatOpen.js::jumpToMessage`'s shape (already-loaded → just highlight;
  else fetch `/messages/around/{id}` and replace `agentMessages`) but writes
  into the local `agentMessages` list instead of the store, since the agent
  chat has no `LinkaChatStore` entry to update.
- **`AgentChatView.js`** - `highlightedId` prop, `data-msg-id` on each
  bubble, same `bg-amber-200/70` flash treatment as `MessageList.js`
  (`highlightedId`/`highlightTimer`), plus a `watch` that scrolls the target
  bubble into view (`scrollIntoView({block:'center'})`) since a jump can land
  outside the currently-rendered page.

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
