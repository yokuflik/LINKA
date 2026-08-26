// Left sidebar: new-private/new-group forms + the chat list itself.
// v-model-style props (showNewPrivate/showNewGroup/newPrivatePhone/...) are
// two-way bound via update:* emits, matching the root's existing refs 1:1.
const ChatSidebar = {
  props: {
    showNewPrivate: { type: Boolean, required: true },
    showNewGroup: { type: Boolean, required: true },
    newPrivatePhone: { type: String, required: true },
    newGroupTitle: { type: String, required: true },
    newGroupMemberPhones: { type: String, required: true },
    chatFormError: { type: String, required: true },
    chatsError: { type: String, required: true },
    chats: { type: Array, required: true },
    activeChatId: { default: null },
    chatDisplayName: { type: Function, required: true },
    formatChatTime: { type: Function, required: true },
  },
  emits: [
    'update:showNewPrivate', 'update:showNewGroup',
    'update:newPrivatePhone', 'update:newGroupTitle', 'update:newGroupMemberPhones',
    'create-private-chat', 'create-group-chat', 'select-chat',
  ],
  template: `
    <aside class="w-72 flex flex-col border-r border-slate-200 bg-white">
      <div class="p-3 border-b border-slate-200 flex gap-2">
        <button @click="$emit('update:showNewPrivate', !showNewPrivate); $emit('update:showNewGroup', false)"
                class="flex-1 text-xs px-2 py-1.5 rounded-lg border border-slate-300 hover:bg-slate-50">+ Private</button>
        <button @click="$emit('update:showNewGroup', !showNewGroup); $emit('update:showNewPrivate', false)"
                class="flex-1 text-xs px-2 py-1.5 rounded-lg border border-slate-300 hover:bg-slate-50">+ Group</button>
      </div>

      <div v-if="showNewPrivate" class="p-3 border-b border-slate-200 bg-slate-50">
        <label class="block text-xs font-medium text-slate-500 mb-1">Their phone number</label>
        <input :value="newPrivatePhone" @input="$emit('update:newPrivatePhone', $event.target.value)"
               placeholder="+972501234567" @keyup.enter="$emit('create-private-chat')"
               class="w-full mb-2 px-2 py-1.5 text-sm border border-slate-300 rounded-lg font-mono" />
        <button @click="$emit('create-private-chat')" class="w-full py-1.5 text-sm bg-teal-700 text-white rounded-lg">Create</button>
      </div>

      <div v-if="showNewGroup" class="p-3 border-b border-slate-200 bg-slate-50">
        <label class="block text-xs font-medium text-slate-500 mb-1">Title</label>
        <input :value="newGroupTitle" @input="$emit('update:newGroupTitle', $event.target.value)"
               class="w-full mb-2 px-2 py-1.5 text-sm border border-slate-300 rounded-lg" />
        <label class="block text-xs font-medium text-slate-500 mb-1">Member phone numbers (comma-separated)</label>
        <input :value="newGroupMemberPhones" @input="$emit('update:newGroupMemberPhones', $event.target.value)"
               placeholder="+972501234567, +972507654321"
               class="w-full mb-2 px-2 py-1.5 text-sm border border-slate-300 rounded-lg font-mono" />
        <button @click="$emit('create-group-chat')" class="w-full py-1.5 text-sm bg-teal-700 text-white rounded-lg">Create</button>
      </div>

      <p v-if="chatFormError" class="px-3 py-1 text-xs text-red-600">{{ chatFormError }}</p>
      <p v-if="chatsError" class="px-3 py-1 text-xs text-red-600">{{ chatsError }}</p>

      <div class="flex-1 overflow-y-auto">
        <button v-for="item in chats" :key="item.chat.id" @click="$emit('select-chat', item.chat.id)"
                class="w-full text-left px-3 py-2.5 border-b border-slate-100 hover:bg-slate-50"
                :class="{ 'bg-teal-50': item.chat.id === activeChatId }">
          <div class="flex items-baseline gap-2">
            <span class="flex-1 min-w-0 text-sm font-medium truncate">{{ chatDisplayName(item.chat) }}</span>
            <span class="shrink-0 text-[11px] text-slate-400">{{ formatChatTime(item.chat.last_message_at) }}</span>
          </div>
          <div class="min-h-[1rem] text-xs text-slate-500 truncate">{{ item.chat.last_message_preview }}</div>
        </button>
        <p v-if="!chats.length" class="p-3 text-sm text-slate-400">No chats yet — create one above.</p>
      </div>
    </aside>
  `,
};
