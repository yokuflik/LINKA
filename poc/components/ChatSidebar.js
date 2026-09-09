// Left sidebar: one big "New chat" button (opens NewChatModal) + the chat
// list itself.
const ChatSidebar = {
  props: {
    chatFormError: { type: String, required: true },
    chatsError: { type: String, required: true },
    chats: { type: Array, required: true },
    activeChatId: { default: null },
    chatDisplayName: { type: Function, required: true },
    // Returns "@username" for a 1:1 chat whose peer has a display_name, else ''
    // (ADR 0024). Keeps an impersonating nickname next to the real handle.
    chatSubLabel: { type: Function, default: () => '' },
    formatChatTime: { type: Function, required: true },
    chatAvatarUrl: { type: Function, required: true },
    chatAvatarName: { type: Function, required: true },
    chatAvatarColorKey: { type: Function, required: true },
    chatAvatarPreview: { type: Function, required: true },
    typingLabelForChat: { type: Function, required: true },
    unreadCountByChatId: { type: Object, required: true },
    isChatMuted: { type: Function, required: true },
  },
  emits: ['open-new-chat', 'select-chat', 'chat-contextmenu'],
  template: `
    <aside class="w-full md:w-72 shrink-0 flex flex-col border-r border-slate-200 bg-white">
      <div class="p-3 border-b border-slate-200">
        <button @click="$emit('open-new-chat')"
                class="w-full flex items-center justify-center gap-2 py-2.5 text-sm font-semibold bg-teal-700 text-white rounded-xl hover:bg-teal-800">
          <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          New chat
        </button>
      </div>

      <InlineAlert :message="chatFormError" class="mx-3 my-1" />
      <InlineAlert :message="chatsError" class="mx-3 my-1" />

      <div class="flex-1 overflow-y-auto">
        <div v-for="item in chats" :key="item.chat.id" @click="$emit('select-chat', item.chat.id)"
                @contextmenu.prevent="$emit('chat-contextmenu', { chatId: item.chat.id, event: $event })"
                class="w-full text-left px-3 py-2.5 border-b border-slate-100 hover:bg-slate-50 flex items-center gap-3 cursor-pointer"
                :class="{ 'bg-teal-50': item.chat.id === activeChatId }">
          <Avatar :url="chatAvatarUrl(item.chat)" :preview="chatAvatarPreview(item.chat)" :name="chatAvatarName(item.chat)"
                  :colorKey="chatAvatarColorKey(item.chat)" sizeClass="w-10 h-10 text-base" />
          <div class="flex-1 min-w-0">
            <div class="flex items-baseline gap-2">
              <span class="flex-1 min-w-0 text-sm font-medium truncate">{{ chatDisplayName(item.chat) }}</span>
              <svg v-if="item.pinned" viewBox="0 0 24 24" class="shrink-0 w-3.5 h-3.5 text-slate-600"
                   fill="currentColor" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <title>Pinned</title>
                <path d="M9 4h6l-1 5 3 3v2H7v-2l3-3-1-5z" />
                <line x1="12" y1="17" x2="12" y2="21" />
              </svg>
              <svg v-if="isChatMuted(item)" viewBox="0 0 24 24" class="shrink-0 w-3.5 h-3.5 text-slate-600"
                   fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <title>Muted</title>
                <path d="M3 9v6h4l5 5V4L7 9H3z" fill="currentColor" />
                <line x1="16" y1="9" x2="22" y2="15" />
                <line x1="22" y1="9" x2="16" y2="15" />
              </svg>
              <span class="shrink-0 text-[11px] text-slate-400">{{ formatChatTime(item.chat.last_message_at) }}</span>
            </div>
            <div v-if="chatSubLabel(item.chat)" class="text-[11px] text-slate-400 font-mono truncate">{{ chatSubLabel(item.chat) }}</div>
            <div class="flex items-center gap-2">
              <div class="flex-1 min-w-0 text-xs truncate"
                   :class="typingLabelForChat(item.chat.id) ? 'text-teal-600 italic' : 'text-slate-500'">
                {{ typingLabelForChat(item.chat.id) || item.chat.last_message_preview }}
              </div>
              <span v-if="unreadCountByChatId[item.chat.id]"
                    class="shrink-0 min-w-[1.25rem] h-5 px-1.5 rounded-full text-white text-[11px] font-semibold flex items-center justify-center"
                    :class="isChatMuted(item) ? 'bg-slate-400' : 'bg-teal-600'">
                {{ unreadCountByChatId[item.chat.id] }}
              </span>
            </div>
          </div>
        </div>
        <p v-if="!chats.length" class="p-3 text-sm text-slate-400">No chats yet — tap "New chat" above.</p>
      </div>
    </aside>
  `,
};
