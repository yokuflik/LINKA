// Chat domain — SIDEBAR CHAT-ROW CONTEXT MENU.
//
// The right-click menu on a chat row (ChatContextMenu.js): pin + mute with a
// duration picker. Just menu-open state and thin handlers that close the menu
// then delegate to useChatList's pin/mute actions.
//
// Merge order: after useChatStore / useChatList.
//
// Needs from ctx (call-time): chats, togglePinChat, muteChat, muteChatUntil,
// unmuteChat.
//
// Global `useChatMenu(ctx)` factory.
function useChatMenu(ctx) {
  const { ref, computed } = Vue;

  const chatMenuChatId = ref(null); // the chat the menu is open for, or null
  const chatMenuRawPosition = ref({ x: 0, y: 0 });
  const chatMenuPosition = computed(() => ({
    x: Math.min(chatMenuRawPosition.value.x, window.innerWidth - 184),
    y: Math.min(chatMenuRawPosition.value.y, window.innerHeight - 100),
  }));
  const chatMenuItem = computed(() => ctx.chats.value.find((c) => c.chat.id === chatMenuChatId.value) || null);

  function openChatContextMenu({ chatId, event }) {
    chatMenuChatId.value = chatId;
    chatMenuRawPosition.value = { x: event.clientX, y: event.clientY };
  }
  function closeChatContextMenu() {
    chatMenuChatId.value = null;
  }
  async function togglePinFromMenu() {
    const id = chatMenuChatId.value;
    closeChatContextMenu();
    if (id != null) await ctx.togglePinChat(id);
  }
  async function mutePresetFromMenu(presetKey) {
    const id = chatMenuChatId.value;
    closeChatContextMenu();
    if (id != null) await ctx.muteChat(id, presetKey);
  }
  async function muteUntilFromMenu(iso) {
    const id = chatMenuChatId.value;
    closeChatContextMenu();
    if (id != null) await ctx.muteChatUntil(id, iso);
  }
  async function unmuteFromMenu() {
    const id = chatMenuChatId.value;
    closeChatContextMenu();
    if (id != null) await ctx.unmuteChat(id);
  }

  return {
    chatMenuChatId, chatMenuPosition, chatMenuItem,
    openChatContextMenu, closeChatContextMenu, togglePinFromMenu,
    mutePresetFromMenu, muteUntilFromMenu, unmuteFromMenu,
  };
}
