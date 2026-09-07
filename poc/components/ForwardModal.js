// "Forward to…" picker, opened from a message's context menu (ADR 0020).
// Same visual language as NewChatModal / SettingsModal.
//
//   - Search row: partial matches over the user's own chats (client-side) +
//     an exact username / phone lookup for people not in a chat yet. Auto-fires
//     ~1.5s after typing stops.
//   - Multi-select list with a modern round check on the right (no checkboxes).
//   - Footer: "N selected" + a Send button.
//
// All logic is in useForward on the root; this only owns the markup.
const ForwardModal = {
  props: {
    query: { type: String, required: true },              // v-model:query
    busy: { type: Boolean, default: false },              // a forward is in flight
    searchBusy: { type: Boolean, default: false },        // people lookup running
    error: { type: String, default: '' },
    userResult: { type: Object, default: null },          // a UserOut, or null
    chatMatches: { type: Array, default: () => [] },       // [{ chat, ... }]
    selectedCount: { type: Number, default: 0 },
    isChatSelected: { type: Function, required: true },
    isUserSelected: { type: Function, required: true },
    userLabel: { type: Function, required: true },
    chatLabel: { type: Function, required: true },
    chatAvatarUrl: { type: Function, required: true },
    chatAvatarPreview: { type: Function, required: true },
    chatAvatarColorKey: { type: Function, required: true },
  },
  emits: ['update:query', 'input-query', 'search', 'toggle-chat', 'toggle-user', 'confirm', 'close'],
  methods: {
    onInput(e) {
      this.$emit('update:query', e.target.value);
      this.$emit('input-query');
    },
  },
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="$emit('close')">
      <div class="w-full max-w-sm bg-white rounded-xl shadow-lg border border-slate-200 p-4 flex flex-col max-h-[85vh]">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold">Forward to…</h2>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <div class="relative mb-1">
          <input :value="query" @input="onInput" @keyup.enter="$emit('search')" autofocus
                 placeholder="Search chats, or a username / phone"
                 class="w-full px-3 py-1.5 text-sm border border-slate-300 rounded-lg" />
          <span v-if="searchBusy" class="absolute right-3 top-1.5 text-xs text-slate-400">…</span>
        </div>
        <p class="text-xs text-slate-400 mb-2">Your chats match partially · type an exact username or phone to reach someone new.</p>

        <InlineAlert :message="error" class="mb-2" />

        <div class="flex-1 min-h-0 overflow-y-auto -mx-1 px-1">
          <template v-if="userResult">
            <p class="text-[11px] uppercase tracking-wide text-slate-400 px-1 mt-1 mb-1">People</p>
            <button @click="$emit('toggle-user', userResult)"
                    class="w-full flex items-center gap-3 p-2 mb-1 rounded-lg hover:bg-slate-50 text-left">
              <Avatar :url="userResult.profile_pic_url" :preview="userResult.profile_pic_preview"
                      :name="userLabel(userResult)" :colorKey="String(userResult.id)"
                      sizeClass="w-9 h-9 text-sm" :enlargeable="false" />
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium truncate">{{ userLabel(userResult) }}</div>
                <div v-if="userResult.username" class="text-xs text-slate-400 truncate">{{ '@' + userResult.username }}</div>
              </div>
              <span class="shrink-0 w-5 h-5 rounded-full border flex items-center justify-center"
                    :class="isUserSelected(userResult.id) ? 'bg-teal-600 border-teal-600 text-white' : 'border-slate-300'">
                <svg v-if="isUserSelected(userResult.id)" viewBox="0 0 24 24" class="w-3.5 h-3.5" fill="none"
                     stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
              </span>
            </button>
          </template>

          <p class="text-[11px] uppercase tracking-wide text-slate-400 px-1 mt-2 mb-1">Your chats</p>
          <p v-if="!chatMatches.length" class="text-xs text-slate-400 px-1 py-2">No matching chats.</p>
          <button v-for="item in chatMatches" :key="item.chat.id"
                  @click="$emit('toggle-chat', item.chat.id)"
                  class="w-full flex items-center gap-3 p-2 rounded-lg hover:bg-slate-50 text-left">
            <Avatar :url="chatAvatarUrl(item.chat)" :preview="chatAvatarPreview(item.chat)"
                    :name="chatLabel(item.chat)" :colorKey="chatAvatarColorKey(item.chat)"
                    sizeClass="w-9 h-9 text-sm" :enlargeable="false" />
            <span class="flex-1 min-w-0 text-sm truncate">{{ chatLabel(item.chat) }}</span>
            <span v-if="item.chat.is_group" class="shrink-0 text-[11px] text-slate-400">group</span>
            <span class="shrink-0 w-5 h-5 rounded-full border flex items-center justify-center"
                  :class="isChatSelected(item.chat.id) ? 'bg-teal-600 border-teal-600 text-white' : 'border-slate-300'">
              <svg v-if="isChatSelected(item.chat.id)" viewBox="0 0 24 24" class="w-3.5 h-3.5" fill="none"
                   stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
            </span>
          </button>
        </div>

        <div class="border-t border-slate-200 pt-3 mt-2 shrink-0 flex items-center gap-3">
          <span class="text-xs text-slate-500 flex-1">
            {{ selectedCount === 0 ? 'Pick one or more chats' : selectedCount + ' selected' }}
          </span>
          <button @click="$emit('confirm')" :disabled="busy || selectedCount === 0"
                  class="px-4 py-2 text-sm font-medium bg-teal-700 text-white rounded-lg disabled:opacity-50 flex items-center gap-2">
            <svg viewBox="0 0 24 24" class="w-4 h-4" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M22 2 11 13" /><path d="M22 2 15 22l-4-9-9-4 20-7z" />
            </svg>
            {{ busy ? 'Sending…' : 'Send' }}
          </button>
        </div>
      </div>
    </div>
  `,
};
