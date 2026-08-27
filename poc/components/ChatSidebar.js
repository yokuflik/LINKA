// Left sidebar: new-private form + "+ Group" (opens NewGroupModal) + the
// chat list itself. v-model-style props (showNewPrivate/newPrivatePhone) are
// two-way bound via update:* emits, matching the root's existing refs 1:1.
const ChatSidebar = {
  props: {
    showNewPrivate: { type: Boolean, required: true },
    newPrivatePhone: { type: String, required: true },
    chatFormError: { type: String, required: true },
    chatsError: { type: String, required: true },
    chats: { type: Array, required: true },
    activeChatId: { default: null },
    chatDisplayName: { type: Function, required: true },
    formatChatTime: { type: Function, required: true },
    chatAvatarUrl: { type: Function, required: true },
    chatAvatarName: { type: Function, required: true },
    chatAvatarColorKey: { type: Function, required: true },
    typingLabelForChat: { type: Function, required: true },
    unreadCountByChatId: { type: Object, required: true },
  },
  emits: [
    'update:showNewPrivate', 'update:newPrivatePhone',
    'create-private-chat', 'open-new-group', 'select-chat',
  ],
  methods: {
    // Real methods (not chained inline-statement handlers) - a chained
    // "$emit(a); $emit(b)" string is fragile in Vue's inline-handler
    // compiler and can silently drop the second call.
    openNewPrivate() {
      this.$emit('update:showNewPrivate', !this.showNewPrivate);
    },
  },
  template: `
    <aside class="w-72 flex flex-col border-r border-slate-200 bg-white">
      <div class="p-3 border-b border-slate-200 flex gap-2">
        <button @click="openNewPrivate"
                class="flex-1 text-xs px-2 py-1.5 rounded-lg border border-slate-300 hover:bg-slate-50">+ Private</button>
        <button @click="$emit('open-new-group')"
                class="flex-1 text-xs px-2 py-1.5 rounded-lg border border-slate-300 hover:bg-slate-50">+ Group</button>
      </div>

      <div v-if="showNewPrivate" class="p-3 border-b border-slate-200 bg-slate-50">
        <label class="block text-xs font-medium text-slate-500 mb-1">Their phone number</label>
        <input :value="newPrivatePhone" @input="$emit('update:newPrivatePhone', $event.target.value)"
               placeholder="+972501234567" @keyup.enter="$emit('create-private-chat')"
               class="w-full mb-2 px-2 py-1.5 text-sm border border-slate-300 rounded-lg font-mono" />
        <button @click="$emit('create-private-chat')" class="w-full py-1.5 text-sm bg-teal-700 text-white rounded-lg">Create</button>
      </div>

      <p v-if="chatFormError" class="px-3 py-1 text-xs text-red-600">{{ chatFormError }}</p>
      <p v-if="chatsError" class="px-3 py-1 text-xs text-red-600">{{ chatsError }}</p>

      <div class="flex-1 overflow-y-auto">
        <button v-for="item in chats" :key="item.chat.id" @click="$emit('select-chat', item.chat.id)"
                class="w-full text-left px-3 py-2.5 border-b border-slate-100 hover:bg-slate-50 flex items-center gap-3"
                :class="{ 'bg-teal-50': item.chat.id === activeChatId }">
          <Avatar :url="chatAvatarUrl(item.chat)" :name="chatAvatarName(item.chat)"
                  :colorKey="chatAvatarColorKey(item.chat)" sizeClass="w-10 h-10 text-base" />
          <div class="flex-1 min-w-0">
            <div class="flex items-baseline gap-2">
              <span class="flex-1 min-w-0 text-sm font-medium truncate">{{ chatDisplayName(item.chat) }}</span>
              <span class="shrink-0 text-[11px] text-slate-400">{{ formatChatTime(item.chat.last_message_at) }}</span>
            </div>
            <div class="flex items-center gap-2">
              <div class="flex-1 min-w-0 text-xs truncate"
                   :class="typingLabelForChat(item.chat.id) ? 'text-teal-600 italic' : 'text-slate-500'">
                {{ typingLabelForChat(item.chat.id) || item.chat.last_message_preview }}
              </div>
              <span v-if="unreadCountByChatId[item.chat.id]"
                    class="shrink-0 min-w-[1.25rem] h-5 px-1.5 rounded-full bg-teal-600 text-white text-[11px] font-semibold flex items-center justify-center">
                {{ unreadCountByChatId[item.chat.id] }}
              </span>
            </div>
          </div>
        </button>
        <p v-if="!chats.length" class="p-3 text-sm text-slate-400">No chats yet — create one above.</p>
      </div>
    </aside>
  `,
};
