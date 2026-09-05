// Chat list + message pane — FACADE.
//
// The chat domain was split by responsibility into four modules + this
// backward-compatible entry point, so consuming components keep importing one
// name. Merge order matters (each layer reads the earlier layers off ctx.*):
//
//   useChatStore    - all shared reactive state + pure derived helpers
//   useChatMembers  - /chats/{id}/members resolvers, name/label + system-msg
//                     helpers, activeChat* label/avatar computeds
//   useChatList     - GET /chats, unread seeding, pin/mute mutations
//   useChatOpen     - selectChat (cache + network), scroll, keyset paging,
//                     draft chats, focus/reconnect refresh, write-through watch
//   useChatMenu     - sidebar chat-row right-click menu
//
// index.html loads useChatStore.js .. useChatMenu.js BEFORE this file, and
// still calls only `Object.assign(ctx, useChats(ctx))`.
//
// Global `useChats(ctx)` factory.
function useChats(ctx) {
  Object.assign(ctx, useChatStore(ctx));
  Object.assign(ctx, useChatMembers(ctx));
  Object.assign(ctx, useChatList(ctx));
  Object.assign(ctx, useChatOpen(ctx));
  Object.assign(ctx, useChatMenu(ctx));

  // Everything is already merged onto ctx; hand back the same object so the
  // caller's `Object.assign(ctx, useChats(ctx))` is a harmless no-op and any
  // destructuring consumer still sees the full surface.
  return ctx;
}
