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
    senderAvatarUrl: { type: Function, required: true },
    statusTickSymbol: { type: Function, required: true },
    statusTickClass: { type: Function, required: true },
    quotedPreviewFor: { type: Function, required: true },
  },
  emits: ['message-contextmenu', 'load-older'],
  // Exposes the scrollable element so the root's scrollMessagesToBottom()
  // (which needs messagesEl.value.scrollTop/scrollHeight) keeps working
  // unchanged across the component boundary.
  setup(props, { emit }) {
    const messagesEl = Vue.ref(null);
    // On scroll, count how many message rows are still fully above the top of
    // the viewport and hand that to the root - it decides when to page.
    function onScroll() {
      const el = messagesEl.value;
      if (!el) return;
      const top = el.scrollTop;
      let rowsAbove = 0;
      for (const child of el.children) {
        if (child.offsetTop + child.offsetHeight < top) rowsAbove++;
        else break;
      }
      emit('load-older', rowsAbove);
    }
    return { messagesEl, onScroll };
  },
  expose: ['messagesEl'],
  template: `
    <div ref="messagesEl" @scroll="onScroll" class="flex-1 overflow-y-auto p-4 space-y-2">
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
           class="max-w-md w-fit flex items-end gap-2"
           :class="m.sender_id === currentUser.id ? 'ml-auto text-right' : ''">
        <Avatar v-if="m.sender_id !== currentUser.id"
                :url="senderAvatarUrl(m.sender_id)" :name="senderLabel(m.sender_id)"
                :colorKey="m.sender_id" sizeClass="w-7 h-7 text-xs"
                class="shrink-0 mb-[18px]" />
        <div class="min-w-0">
        <div class="inline-block px-3 py-2 rounded-2xl text-sm bubble-tail cursor-pointer"
             :class="m.sender_id === currentUser.id ? 'bg-teal-700 text-white rounded-br-none bubble-tail-mine' : 'bg-white border border-slate-200 rounded-bl-none bubble-tail-theirs'"
             @contextmenu.prevent="$emit('message-contextmenu', { message: m, event: $event })">
          <div v-if="m.sender_id !== currentUser.id" class="text-[11px] opacity-60 mb-0.5">{{ senderLabel(m.sender_id) }}</div>
          <!-- Quoted reply preview (WhatsApp-style) - only when this message
               is itself a reply (reply_to_message_id set). quotedPreviewFor
               looks the original message up client-side (it's a lookup, not
               a re-render decision, so it stays a plain function prop). -->
          <div v-if="quotedPreviewFor(m)" class="mb-1 px-2 py-1 rounded border-l-4 text-left text-xs"
               :class="m.sender_id === currentUser.id ? 'bg-white/10 border-white/60 text-white/90' : 'bg-slate-100 border-teal-600 text-slate-600'">
            <div class="font-semibold truncate">{{ quotedPreviewFor(m).sender }}</div>
            <div class="truncate opacity-90">{{ quotedPreviewFor(m).snippet }}</div>
          </div>
          {{ m.content }}
          <span v-if="m.is_edited" class="text-[10px] opacity-60"> (edited)</span>
        </div>
        <div class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-1"
             :class="m.sender_id === currentUser.id ? 'justify-end' : ''">
          <span>{{ new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}</span>
          <span v-if="m.sender_id === currentUser.id" class="text-sm font-bold leading-none" :class="statusTickClass(m.status)">{{ statusTickSymbol(m.status) }}</span>
        </div>
        </div>
      </div>
      </template>
      <p v-if="!messages.length" class="h-full flex items-center justify-center text-sm text-slate-400">No messages here</p>
    </div>
  `,
};
