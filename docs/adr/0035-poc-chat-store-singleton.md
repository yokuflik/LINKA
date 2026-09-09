# ADR 0035 — PoC `chatStore` singleton as the single source of truth for shared chat state

**Status:** Accepted
**Date:** 2026-09-10
**Scope:** `poc/` frontend only. No backend / Rust change.

## Context

The PoC `setup()` is a composable stitch: `const ctx = {}` then 34×
`Object.assign(ctx, useX(ctx))` ([index.html](../../poc/index.html)). Every
composable takes the shared, untyped `ctx` "god-bag", writes its slice onto it,
and reads every sibling's slice back off `ctx.*` at call time.

Item 9 from the architecture analysis: **composables mutate each other's refs
through `ctx`.** The worst offender is [useWsRouter.js](../../poc/composables/useWsRouter.js)
(~506 lines) — a WebSocket event router that:

- **owns** shared state it shouldn't: `unreadCountByChatId`, the
  `pendingChatMessages` buffer;
- **mutates** state owned by `useChatStore` through `ctx`: `ctx.messages`,
  `ctx.chats`, `ctx.activeChatId`, `ctx.userById`, `ctx.groupChatMembers`,
  `ctx.privateChatTitles`, `ctx.privateChatOtherUserId`, `ctx.sortChats`.

`useChatStore` already exists as the state module, but only as a
`useChatStore(ctx)` factory whose refs exist *because* the `useChats` facade
merges them onto `ctx` — there is no importable store object.

Killing `ctx` outright means rewriting all 34 composables and is out of scope.
This ADR does the coherent first slice.

## Decision

1. **Promote `useChatStore.js` to a module-level singleton, `LinkaChatStore`.**
   Built once at script-load via an IIFE (classic script, no build step — the
   same global-lexical pattern the components already use). Holds every shared
   chat ref + the pure helpers (`sortChats`, mute/tick helpers, role/status
   formatters, consts). Takes no `ctx`.

2. **Move unread-badge + buffered-message state into the store.**
   `unreadCountByChatId` / `bumpUnreadCount` / `clearUnreadCount` and the
   `pendingChatMessages` map (now `bufferMessage` + `takeBufferedMessages`)
   move from `useWsRouter` into `LinkaChatStore`. `useWsRouter` stops owning
   any state.

3. **Back-compat shim.** `useChatStore.js` still exports a
   `function useChatStore(_ctx) { return LinkaChatStore; }`, so the
   `useChats` facade's `Object.assign(ctx, useChatStore(ctx))` keeps copying
   **the same ref objects** onto `ctx`. The other 31 composables
   (`useChatOpen`, `useChatList`, `useChatMeta`, `useAuth`, …) and the
   `ChatSidebar` `:unreadCountByChatId` prop are unchanged — same identity,
   same reactivity.

4. **`useWsRouter` depends on the store, not `ctx`, for state.** All state
   reads/writes go through `LinkaChatStore.*`. It still calls sibling
   *behaviour* off `ctx` (`scrollMessagesToBottom`, `decryptInPlace`,
   `sendReceipt`, `bumpChatPreview`, `refreshMessageStatuses`, `showToast`,
   `probeMediaOrientation`, …) — those are service calls, not ref mutation,
   and stay until a later services extraction. `useWsRouter` now returns only
   `handleWsMessage`.

5. **`MessageList.js` reads `messages` from the store by injection.** The root
   `provide('chatStore', LinkaChatStore)`; `MessageList` drops its `messages`
   prop and `Vue.inject('chatStore')` → `computed(() => store.messages.value)`.
   All other `MessageList` props are presentation helpers (formatters,
   flags) and stay.

## Consequences

- One importable source of truth for chat/message/unread state. `useWsRouter`
  is a pure `wsEvent → LinkaChatStore` mapper (+ documented service-call
  boundary) and owns zero refs.
- `ctx` still exists for the other 31 composables and is still what `setup()`
  returns for template binding — full `ctx` removal is a later, larger change.
- The store's `setInterval` (mute-expiry tick) now starts at page load instead
  of at app mount — harmless (ticks a ref, clears lapsed mutes on a
  then-empty list).
- No behaviour change intended. Verified by the user manually (Rule 6 — no
  autonomous visual testing).
- `.claude_docs/frontend.md` updated in the same change.
