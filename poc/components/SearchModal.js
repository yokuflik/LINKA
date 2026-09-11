// Global message search (ADR 0040), opened via the magnifying-glass button in
// AppHeader. Centered overlay over the chat list, same modal chrome as the
// other modals but taller (results list). All state/fetching lives in
// useSearch on the root; this only owns the markup.
const SearchModal = {
  props: {
    query: { type: String, required: true },         // v-model:query
    busy: { type: Boolean, default: false },          // first page in flight
    moreBusy: { type: Boolean, default: false },      // next page in flight
    error: { type: String, default: '' },
    results: { type: Array, default: () => [] },      // SearchResultOut[]
    hasMore: { type: Boolean, default: false },
    scopedChatName: { type: String, default: '' },    // set -> in-chat search, empty -> global
    chats: { type: Array, required: true },           // ctx.chats - to resolve a hit's chat_id
    chatDisplayName: { type: Function, required: true },
    chatAvatarUrl: { type: Function, required: true },
    chatAvatarPreview: { type: Function, required: true },
    chatAvatarColorKey: { type: Function, required: true },
    formatChatTime: { type: Function, required: true },
  },
  emits: ['update:query', 'input-query', 'search', 'load-more', 'pick', 'close'],
  methods: {
    onInput(e) {
      this.$emit('update:query', e.target.value);
      this.$emit('input-query');
    },
    chatFor(result) {
      const item = this.chats.find((c) => c.chat.id === result.chat_id);
      return item ? item.chat : null;
    },
    // Smart / lazy pagination: only ask for the next page once the list is
    // scrolled near its bottom - never loads ahead of what's shown.
    onResultsScroll(e) {
      const el = e.target;
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
        this.$emit('load-more');
      }
    },
  },
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-start justify-center pt-[8vh] z-50" @click.self="$emit('close')">
      <div class="w-full max-w-md bg-white rounded-xl shadow-lg border border-slate-200 p-4 flex flex-col max-h-[80vh]">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold">{{ scopedChatName ? ('Search in ' + scopedChatName) : 'Search messages' }}</h2>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <div class="flex gap-2 mb-2">
          <div class="relative flex-1">
            <svg viewBox="0 0 24 24" class="w-4 h-4 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400"
                 fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.2-3.2" />
            </svg>
            <input :value="query" @input="onInput" @keyup.enter="$emit('search')" autofocus
                   :placeholder="scopedChatName ? 'Search in this chat…' : 'Search all chats…'"
                   class="w-full pl-8 pr-2 py-1.5 text-sm border border-slate-300 rounded-lg" />
          </div>
          <button @click="$emit('search')" :disabled="busy || !query.trim()"
                  class="px-3 py-1.5 text-sm bg-teal-700 text-white rounded-lg disabled:opacity-50">
            {{ busy ? '…' : 'Search' }}
          </button>
        </div>

        <InlineAlert :message="error" class="mb-2" />

        <p v-if="!busy && query.trim().length >= 2" class="text-xs text-slate-400 mb-1 px-0.5">
          {{ results.length }} result{{ results.length === 1 ? '' : 's' }}
        </p>

        <div class="flex-1 min-h-0 overflow-y-auto -mx-1 px-1" @scroll="onResultsScroll">
          <div v-if="busy" class="flex items-center justify-center py-8">
            <span class="w-5 h-5 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
          </div>

          <template v-else>
            <button v-for="r in results" :key="r.id"
                    @click="$emit('pick', r)"
                    class="w-full flex items-start gap-3 p-2 rounded-lg hover:bg-slate-50 text-left">
              <Avatar v-if="!scopedChatName && chatFor(r)" :url="chatAvatarUrl(chatFor(r))" :preview="chatAvatarPreview(chatFor(r))"
                      :name="chatDisplayName(chatFor(r))" :colorKey="chatAvatarColorKey(chatFor(r))"
                      sizeClass="w-9 h-9 text-sm shrink-0" :enlargeable="false" />
              <div class="flex-1 min-w-0">
                <div class="flex items-baseline justify-between gap-2">
                  <span v-if="!scopedChatName" class="text-sm font-medium truncate">{{ chatFor(r) ? chatDisplayName(chatFor(r)) : 'Chat' }}</span>
                  <span class="text-[11px] text-slate-400 shrink-0" :class="{ 'ml-auto': scopedChatName }">{{ formatChatTime(r.created_at) }}</span>
                </div>
                <div class="text-xs text-slate-500 truncate">{{ r.snippet || r.content || '' }}</div>
              </div>
            </button>

            <div v-if="moreBusy" class="flex items-center justify-center py-3">
              <span class="w-4 h-4 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
            </div>

            <p v-if="!results.length && query.trim().length >= 2" class="text-center text-sm text-slate-400 py-8">
              No messages found.
            </p>
            <p v-if="!query.trim()" class="text-center text-sm text-slate-400 py-8">
              Type at least 2 characters to search.
            </p>
          </template>
        </div>
      </div>
    </div>
  `,
};
