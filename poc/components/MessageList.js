// Scrollable message pane: system-message pills, bubbles (mine/theirs), and
// the "no messages" empty state. Kept as one component (not split further
// into a MessageBubble child) since the v-for body's mine/theirs/system
// branches share the same list and messagesEl ref must stay on this
// scrolling container for scrollMessagesToBottom to keep working unchanged.
const MessageList = {
  props: {
    messages: { type: Array, required: true },
    messagesError: { type: String, required: true },
    currentUser: { type: Object, required: true },
    shouldShowSystemMessage: { type: Function, required: true },
    systemMessageText: { type: Function, required: true },
    senderLabel: { type: Function, required: true },
    statusTickSymbol: { type: Function, required: true },
    statusTickClass: { type: Function, required: true },
  },
  // Exposes the scrollable element so the root's scrollMessagesToBottom()
  // (which needs messagesEl.value.scrollTop/scrollHeight) keeps working
  // unchanged across the component boundary.
  setup() {
    const messagesEl = Vue.ref(null);
    return { messagesEl };
  },
  expose: ['messagesEl'],
  template: `
    <div ref="messagesEl" class="flex-1 overflow-y-auto p-4 space-y-2">
      <p v-if="messagesError" class="text-sm text-red-600">{{ messagesError }}</p>
      <template v-for="m in messages" :key="m.id">
        <!-- System messages (sender_id == null, e.g. "X added Y to the group") -
             centered, small, gray pill, like WhatsApp's own group-event lines.
             shouldShowSystemMessage filters out "role_changed" notices for
             anyone but the actor/target - see chat_service.change_member_role. -->
        <div v-if="m.sender_id == null && shouldShowSystemMessage(m)" class="flex justify-center">
          <span class="inline-block px-2.5 py-1 rounded-full text-[11px] bg-slate-200 text-slate-600">{{ systemMessageText(m) }}</span>
        </div>
      <div v-else-if="m.sender_id != null"
           class="max-w-md" :class="m.sender_id === currentUser.id ? 'ml-auto text-right' : ''">
        <div class="inline-block px-3 py-2 rounded-2xl text-sm bubble-tail"
             :class="m.sender_id === currentUser.id ? 'bg-teal-700 text-white rounded-br-none bubble-tail-mine' : 'bg-white border border-slate-200 rounded-bl-none bubble-tail-theirs'">
          <div v-if="m.sender_id !== currentUser.id" class="text-[11px] opacity-60 mb-0.5">{{ senderLabel(m.sender_id) }}</div>
          {{ m.content }}
          <span v-if="m.is_edited" class="text-[10px] opacity-60"> (edited)</span>
        </div>
        <div class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-1"
             :class="m.sender_id === currentUser.id ? 'justify-end' : ''">
          <span>{{ new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}</span>
          <span v-if="m.sender_id === currentUser.id" class="text-sm font-bold leading-none" :class="statusTickClass(m.status)">{{ statusTickSymbol(m.status) }}</span>
        </div>
      </div>
      </template>
      <p v-if="!messages.length" class="h-full flex items-center justify-center text-sm text-slate-400">No messages here</p>
    </div>
  `,
};
