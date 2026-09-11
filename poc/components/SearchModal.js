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
  data() {
    return {
      MEDIA_LABELS: { 2: 'Photo', 3: 'Video', 4: 'Voice message', 5: 'File' },
      MEDIA_ICONS: {
        2: 'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14M4 6h16a1 1 0 011 1v10a1 1 0 01-1 1H4a1 1 0 01-1-1V7a1 1 0 011-1zM9.5 9a1 1 0 11-2 0 1 1 0 012 0z',
        3: 'M15 10l4.55-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.45.894L15 14M5 6h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2z',
        4: 'M12 1a3 3 0 00-3 3v7a3 3 0 006 0V4a3 3 0 00-3-3zM5 10a1 1 0 10-2 0 9 9 0 007 8.72V21H8a1 1 0 100 2h8a1 1 0 100-2h-2v-2.28A9 9 0 0021 10a1 1 0 10-2 0 7 7 0 01-14 0z',
        5: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8l-6-6zM14 2v6h6',
      },
    };
  },
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
    // Splits a snippet into plain/matched/plain parts around the query so the
    // hit itself can be bolded - falls back to plain text for no/failed match.
    snippetParts(result) {
      const text = result.snippet || result.content || '';
      const q = this.query.trim();
      if (!q || !text) return [{ text, match: false }];
      const idx = text.toLowerCase().indexOf(q.toLowerCase());
      if (idx === -1) return [{ text, match: false }];
      const parts = [];
      if (idx > 0) parts.push({ text: text.slice(0, idx), match: false });
      parts.push({ text: text.slice(idx, idx + q.length), match: true });
      if (idx + q.length < text.length) parts.push({ text: text.slice(idx + q.length), match: false });
      return parts;
    },
    isMediaOnly(result) {
      return result.type >= 2 && result.type <= 5 && !result.content;
    },
    mediaLabel(result) {
      return this.MEDIA_LABELS[result.type] || 'Attachment';
    },
    mediaIconPath(result) {
      return this.MEDIA_ICONS[result.type] || this.MEDIA_ICONS[5];
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
                    class="w-full flex items-start gap-3 px-2.5 py-2.5 rounded-lg hover:bg-slate-50 active:bg-slate-100 text-left transition-colors mb-1.5 last:mb-0"
                    :class="scopedChatName ? 'border border-slate-200' : 'border-b border-slate-100 last:border-b-0'">
              <Avatar v-if="!scopedChatName && chatFor(r)" :url="chatAvatarUrl(chatFor(r))" :preview="chatAvatarPreview(chatFor(r))"
                      :name="chatDisplayName(chatFor(r))" :colorKey="chatAvatarColorKey(chatFor(r))"
                      sizeClass="w-10 h-10 text-sm shrink-0" :enlargeable="false" />
              <svg v-else-if="scopedChatName && isMediaOnly(r)" viewBox="0 0 24 24"
                   class="w-9 h-9 shrink-0 rounded-lg bg-teal-50 text-teal-600 p-2"
                   fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path :d="mediaIconPath(r)" />
              </svg>
              <div class="flex-1 min-w-0">
                <div class="flex items-baseline justify-between gap-2">
                  <span v-if="!scopedChatName" class="text-sm font-medium text-slate-800 truncate">{{ chatFor(r) ? chatDisplayName(chatFor(r)) : 'Chat' }}</span>
                  <span v-else-if="isMediaOnly(r)" class="text-sm font-medium text-slate-700 truncate">{{ mediaLabel(r) }}</span>
                  <span class="text-[11px] text-slate-400 shrink-0" :class="{ 'ml-auto': scopedChatName && !isMediaOnly(r) }">{{ formatChatTime(r.created_at) }}</span>
                </div>
                <div v-if="!isMediaOnly(r)" class="text-xs text-slate-500 leading-snug line-clamp-2 mt-0.5">
                  <template v-for="(part, i) in snippetParts(r)" :key="i">
                    <mark v-if="part.match" class="bg-teal-100 text-teal-800 rounded-sm px-0.5 font-medium">{{ part.text }}</mark>
                    <template v-else>{{ part.text }}</template>
                  </template>
                </div>
                <div v-else-if="r.media_name" class="text-xs text-slate-400 truncate mt-0.5">{{ r.media_name }}</div>
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
