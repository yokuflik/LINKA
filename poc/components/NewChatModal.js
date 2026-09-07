// New-chat modal, opened from the single big "New chat" button in the
// sidebar. Same visual style as SettingsModal / NewGroupModal.
//
//   - Search row: find a user by EXACT username or phone (ADR 0017 - no
//     prefix search). Auto-fires ~1.5s after the user stops typing (no button
//     needed; the button is still there for an immediate search).
//   - Under it, a scrollable list of PARTIAL matches among EXISTING chats
//     (groups + private peers) - pure client-side, capped height so it
//     never hides the "New group" button below.
//   - "New group" button -> forwards to the existing group-creation flow.
//
// All the work lives in useNewChat on the root; this only owns the markup.
const NewChatModal = {
  props: {
    query: { type: String, required: true },        // v-model:query
    busy: { type: Boolean, default: false },
    error: { type: String, default: '' },
    result: { type: Object, default: null },        // a UserOut, or null
    existingMatches: { type: Array, default: () => [] }, // [{ chat, ... }]
    userLabel: { type: Function, required: true },   // (user) -> display name
    chatLabel: { type: Function, required: true },   // (chat) -> display name
    chatAvatarUrl: { type: Function, required: true },
    chatAvatarPreview: { type: Function, required: true },
    chatAvatarColorKey: { type: Function, required: true },
  },
  emits: ['update:query', 'input-query', 'search', 'pick', 'pick-chat', 'new-group', 'close'],
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
          <h2 class="text-sm font-semibold">New chat</h2>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <label class="block text-xs font-medium text-slate-500 mb-1">Find someone by username or phone number</label>
        <div class="flex gap-2 mb-1">
          <input :value="query" @input="onInput" @keyup.enter="$emit('search')" autofocus
                 placeholder="username or +972501234567"
                 class="flex-1 px-2 py-1.5 text-sm border border-slate-300 rounded-lg" />
          <button @click="$emit('search')" :disabled="busy || !query.trim()"
                  class="px-3 py-1.5 text-sm bg-teal-700 text-white rounded-lg disabled:opacity-50">
            {{ busy ? '…' : 'Search' }}
          </button>
        </div>
        <p class="text-xs text-slate-400 mb-2">Exact match to add someone new · partial matches from your chats below.</p>

        <InlineAlert :message="error" class="mb-2" />

        <!-- Scrollable results area: exact user hit + partial chat matches.
             Capped height so the "New group" button below stays visible. -->
        <div class="flex-1 min-h-0 overflow-y-auto -mx-1 px-1">
          <button v-if="result" @click="$emit('pick', result)"
                  class="w-full flex items-center gap-3 p-2 mb-1 rounded-lg border border-slate-200 hover:bg-slate-50 text-left">
            <Avatar :url="result.profile_pic_url" :preview="result.profile_pic_preview"
                    :name="userLabel(result)" :colorKey="String(result.id)"
                    sizeClass="w-10 h-10 text-base" :enlargeable="false" />
            <div class="flex-1 min-w-0">
              <div class="text-sm font-medium truncate">{{ userLabel(result) }}</div>
              <div v-if="result.username" class="text-xs text-slate-400 truncate">{{ '@' + result.username }}</div>
            </div>
          </button>

          <p v-if="existingMatches.length" class="text-[11px] uppercase tracking-wide text-slate-400 px-1 mt-2 mb-1">From your chats</p>
          <button v-for="item in existingMatches" :key="item.chat.id"
                  @click="$emit('pick-chat', item.chat.id)"
                  class="w-full flex items-center gap-3 p-2 rounded-lg hover:bg-slate-50 text-left">
            <Avatar :url="chatAvatarUrl(item.chat)" :preview="chatAvatarPreview(item.chat)"
                    :name="chatLabel(item.chat)" :colorKey="chatAvatarColorKey(item.chat)"
                    sizeClass="w-9 h-9 text-sm" :enlargeable="false" />
            <span class="flex-1 min-w-0 text-sm truncate">{{ chatLabel(item.chat) }}</span>
            <span v-if="item.chat.is_group" class="shrink-0 text-[11px] text-slate-400">group</span>
          </button>
        </div>

        <div class="border-t border-slate-200 pt-3 mt-2 shrink-0">
          <button @click="$emit('new-group')"
                  class="w-full flex items-center justify-center gap-2 py-2 text-sm font-medium border border-slate-300 rounded-lg hover:bg-slate-50">
            <svg viewBox="0 0 24 24" class="w-4 h-4" fill="none" stroke="currentColor"
                 stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M17 21v-2a4 4 0 0 0-3-3.87" />
              <path d="M9 21v-2a4 4 0 0 1 4-4h0a4 4 0 0 1 1 .13" />
              <circle cx="9" cy="7" r="4" />
              <line x1="19" y1="8" x2="19" y2="14" />
              <line x1="22" y1="11" x2="16" y2="11" />
            </svg>
            New group
          </button>
        </div>
      </div>
    </div>
  `,
};
