// Chat body of the AI agent drawer (AGENT_DRAWER_UI_PLAN.md Step 7). A
// minimal, dedicated read/compose loop over `agentMessages` - NOT MessageList
// (which hard-reads LinkaChatStore.messages/activeChatId; reusing it here
// would make opening the drawer act like navigating away from whatever chat
// the user has open behind it). Bubble look (shape/tail/colors), the
// timestamp + delivered/read ticks row, and the day-separator pills mirror
// MessageList.js exactly (same Tailwind classes, same statusTickSymbol/
// statusTickClass from LinkaChatStore) so the agent's own chat is visually
// indistinguishable from a normal 1:1 chat - only its content (agent replies
// are plain text, type 7) differs.
const AgentChatView = {
  props: {
    currentUser: { type: Object, required: true },
    messages: { type: Array, required: true },
    loading: { type: Boolean, default: false },
    hasMore: { type: Boolean, default: false },
    loadingOlder: { type: Boolean, default: false },
    thinkingStatus: { default: null }, // { status, detail } | null
  },
  emits: ['send', 'load-older'],
  data() {
    return { draft: '', pinnedToBottom: true, prependAdjust: null };
  },
  computed: {
    // Same day-grouping + WhatsApp-style clustering as MessageList.js's `rows`
    // computed - a message from the same sender within GROUP_WINDOW_MS of the
    // previous one is visually clustered (tight gap, one timestamp at the end).
    rows() {
      const GROUP_WINDOW_MS = 10 * 60 * 1000;
      function dayKey(iso) {
        const d = new Date(iso);
        return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
      }
      function dayLabel(iso) {
        const d = new Date(iso);
        const today = new Date();
        const yesterday = new Date();
        yesterday.setDate(today.getDate() - 1);
        if (dayKey(iso) === dayKey(today.toISOString())) return 'Today';
        if (dayKey(iso) === dayKey(yesterday.toISOString())) return 'Yesterday';
        const opts = d.getFullYear() === today.getFullYear()
          ? { day: 'numeric', month: 'long' }
          : { day: 'numeric', month: 'long', year: 'numeric' };
        return d.toLocaleDateString([], opts);
      }
      function sameCluster(prev, curr) {
        if (!prev || !curr) return false;
        if (prev.type !== curr.type) return false;
        if (!prev.created_at || !curr.created_at) return false;
        return dayKey(prev.created_at) === dayKey(curr.created_at)
          && (new Date(curr.created_at) - new Date(prev.created_at)) <= GROUP_WINDOW_MS;
      }
      const out = [];
      let lastKey = null;
      const list = this.messages;
      for (let i = 0; i < list.length; i++) {
        const m = list[i];
        if (!m.created_at) {
          out.push({ type: 'msg', m, groupStart: true, groupEnd: true });
          continue;
        }
        const k = dayKey(m.created_at);
        if (k !== lastKey) {
          out.push({ type: 'separator', key: 'sep-' + k, label: dayLabel(m.created_at) });
          lastKey = k;
        }
        const groupStart = !sameCluster(list[i - 1], m);
        const groupEnd = !sameCluster(m, list[i + 1]);
        out.push({ type: 'msg', m, groupStart, groupEnd });
      }
      return out;
    },
  },
  beforeUpdate() {
    // Preserve scroll position when older messages are prepended by
    // load-older, mirroring useChatOpen.js's loadOlderMessages - without
    // this the scroll-to-top trigger and the resulting prepend fight each
    // other and the view jumps back to the newly-loaded block's top.
    if (this.loadingOlderCaptured) {
      const el = this.$refs.scrollEl;
      if (el) this.prependAdjust = { prevHeight: el.scrollHeight, prevTop: el.scrollTop };
      this.loadingOlderCaptured = false;
    }
  },
  updated() {
    if (this.prependAdjust) {
      const el = this.$refs.scrollEl;
      if (el) el.scrollTop = this.prependAdjust.prevTop + (el.scrollHeight - this.prependAdjust.prevHeight);
      this.prependAdjust = null;
      return;
    }
    if (this.pinnedToBottom) this.scrollToBottom();
  },
  mounted() {
    this.scrollToBottom();
  },
  methods: {
    // Agent replies (type 7) render like "theirs"; everything else (the
    // owner's own sends) renders like "mine" - same rule AgentChatView has
    // always used, kept here for the tick/side logic below.
    isMine(m) { return m.type !== 7; },
    formatTime(iso) {
      return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    },
    scrollToBottom() {
      const el = this.$refs.scrollEl;
      if (el) el.scrollTop = el.scrollHeight;
    },
    onScroll() {
      const el = this.$refs.scrollEl;
      if (!el) return;
      this.pinnedToBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      if (el.scrollTop < 80 && this.hasMore && !this.loadingOlder) {
        this.loadingOlderCaptured = true;
        this.$emit('load-older');
      }
    },
    submit() {
      const text = this.draft.trim();
      if (!text) return;
      this.pinnedToBottom = true;
      this.$emit('send', text);
      this.draft = '';
      this.$nextTick(this.resizeDraft);
    },
    onEnter(event) {
      if (event.shiftKey) return; // allow newline
      event.preventDefault();
      this.submit();
    },
    // Auto-grow the textarea to fit its content, capped at ~6 lines.
    resizeDraft() {
      const el = this.$refs.draftEl;
      if (!el) return;
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 144) + 'px';
    },
  },
  template: `
    <div class="flex flex-col h-full min-h-0">
      <div ref="scrollEl" @scroll="onScroll" class="flex-1 min-h-0 overflow-y-auto p-4 chat-background">
        <div v-if="loadingOlder" class="flex flex-col items-center justify-center gap-1 py-3 text-xs text-slate-400">
          <span class="w-5 h-5 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
        </div>
        <p v-if="loading" class="text-xs text-slate-400 text-center py-2">Loading…</p>
        <template v-for="row in rows" :key="row.type === 'separator' ? row.key : (row.m.id || row.m.client_message_id)">
          <div v-if="row.type === 'separator'" class="day-separator flex justify-center py-1">
            <span class="inline-block px-3 py-1 rounded-full text-[11px] font-medium bg-slate-200 text-slate-600 shadow-sm whitespace-nowrap">{{ row.label }}</span>
          </div>
          <template v-else>
          <div :class="row.groupEnd ? 'mb-2' : 'mb-0.5'">
            <div class="max-w-md w-fit flex items-end gap-2 rounded-2xl"
                 :class="isMine(row.m) ? 'ml-auto text-right' : ''">
              <div class="min-w-0">
                <div dir="auto" class="inline-block text-sm cursor-default whitespace-pre-wrap break-words"
                     :class="isMine(row.m)
                       ? (row.groupEnd
                           ? 'px-3 py-2 rounded-2xl bubble-tail bg-teal-700 text-white rounded-br-none bubble-tail-mine'
                           : 'px-3 py-2 rounded-2xl bg-teal-700 text-white')
                       : (row.groupEnd
                           ? 'px-3 py-2 rounded-2xl bubble-tail bg-white border border-slate-200 rounded-bl-none bubble-tail-theirs'
                           : 'px-3 py-2 rounded-2xl bg-white border border-slate-200')">
                  <span v-if="row.m.content">{{ row.m.content }}</span>
                </div>
                <div v-if="row.groupEnd || row.m.send_failed || row.m.pending"
                     class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-1"
                     :class="isMine(row.m) ? 'justify-end' : ''">
                  <span>{{ formatTime(row.m.created_at) }}</span>
                  <span v-if="row.m.send_failed" class="text-sm font-bold leading-none text-red-500" title="Not sent">⚠️</span>
                  <span v-else-if="row.m.pending" class="text-sm leading-none text-slate-400" title="Sending…">🕓</span>
                </div>
              </div>
            </div>
          </div>
          </template>
        </template>
        <div v-if="thinkingStatus" class="mb-2 flex justify-start">
          <div class="max-w-[80%] rounded-2xl px-3 py-1.5 text-xs italic text-slate-500 bg-white border border-slate-200">
            {{ thinkingStatus.detail || 'Thinking…' }}
          </div>
        </div>
      </div>
      <div class="shrink-0 border-t border-slate-200 p-2 flex items-center gap-2">
        <textarea ref="draftEl" v-model="draft" @keydown.enter="onEnter" @input="resizeDraft"
                  rows="1" placeholder="Message your agent…" dir="auto"
                  class="flex-1 min-w-0 px-3 py-1.5 text-sm border border-slate-300 rounded-2xl resize-none leading-normal max-h-36 overflow-y-auto"></textarea>
        <button @click="submit" :disabled="!draft.trim()"
                class="w-9 h-9 shrink-0 self-end flex items-center justify-center rounded-full bg-teal-700 text-white disabled:opacity-40">
          <svg viewBox="0 0 24 24" class="w-4 h-4" fill="currentColor"><path d="M3 20l18-8L3 4v6l12 2-12 2z"/></svg>
        </button>
      </div>
    </div>
  `,
};
