# PoC composables — refactor status

`poc/index.html` `setup()` is a composable stitch: `Object.assign(ctx, useX(ctx))`
per module, in this order. Each `useX(ctx)` returns a slice; cross-module refs are
`ctx.*` at call time, so merge order only matters where a factory *runs* another
module's fn during `setup()` (rare).

## Modules (load order = merge order)
1. `core.js` — config, logging, `apiFetch`, upload constants, `shrinkImageToFit`
2. `useAuth.js`
3. `useWebsocket.js`
4. `usePresence.js`
5. `useTyping.js`
6. `useChatMeta.js`
7. `useChatStore.js` → `useChatMembers.js` → `useChatList.js` →
   `useChatOpen.js` → `useChatMenu.js` → `useChats.js` (facade over the five;
   `Object.assign`s them onto ctx in that order, returns ctx)
8. `useMembers.js`
9. `useNewChat.js`
10. `useMessageMenu.js` — right-click context menu, "Details" receipt modal,
    image/video orientation probing
11. `useMessageSend.js` — send text message, optimistic bubble, reply-to compose
    state (`replyingToMessage`, `startReplyTo`, `cancelReply`, `quotedPreviewFor`)
12. `useMessageEdit.js` — edit + delete a message, optimistic local update on
    delete (`editingMessage`, `startEditMessage`, `cancelEdit`, `canEditMessage`,
    `canDeleteMessage`, `deleteMessage`)
13. `useMediaUpload.js` — media messages + voice recording: upload-ticket → PUT
    bytes → `send_message` WS frame (`sendMediaMessage`, `mediaUploadBusy`,
    `isRecording`, `recordingSeconds`, `startRecording`, `stopRecording`)
14. `useWsRouter.js`

## History
- 2026-08-27: original `setup()` split (index.html 2135 → ~294 lines).
- 2026-08-28: `useMessageActions.js` (496 lines) split by responsibility into
  modules 10–13 above (pure code move, no behaviour change). File deleted.
- 2026-09-06: `useChats.js` (693 lines) split by sub-domain into `useChatStore`
  (shared state + pure helpers), `useChatMembers` (member resolvers, name/label
  + system-message helpers, activeChat* computeds), `useChatList` (GET /chats,
  unread seeding, pin/mute mutations), `useChatOpen` (selectChat cache+network,
  scroll, keyset paging, draft chats, focus/reconnect refresh, write-through
  watch), `useChatMenu` (sidebar row context menu). `useChats.js` is now a
  ~15-line facade; public API unchanged (pure code move, no behaviour change).

### Cross-module ties inside the message group (all `ctx.*` call-time)
- `useMessageSend.startReplyTo` → `ctx.editingMessage` / `ctx.cancelEdit` /
  `ctx.closeMessageContextMenu`
- `useMessageSend.sendMessage` → `ctx.editingMessage` (edit-send path lives here,
  next to the composer)
- `useMessageEdit.startEditMessage` → `ctx.replyingToMessage`
- `useMediaUpload` → `ctx.replyingToMessage`
