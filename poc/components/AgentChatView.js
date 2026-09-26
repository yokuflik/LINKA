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
    // ADR 0059 - true once either token-usage window is exhausted; disables
    // the composer (input, +, send) until the window resets. Server-side is
    // the real enforcement (invoke_worker.py's pre-flight gate) - this is a
    // UX convenience only.
    usageBlocked: { type: Boolean, default: false },
    // {window_5h, window_7d} | null - used only to compute the exact
    // "frozen until HH:MM" clock time shown above the composer while blocked
    // (UsageProgressBar's popover is the only place the percentages/bars
    // themselves are shown, per explicit user requirement).
    usage: { type: Object, default: null },
  },
  emits: ['send', 'load-older', 'pick-pdf'],
  data() {
    return { draft: '', pinnedToBottom: true, prependAdjust: null, now: Date.now() };
  },
  mounted() {
    this.scrollToBottom();
    this._tickTimer = setInterval(() => { this.now = Date.now(); }, 1000);
  },
  beforeUnmount() {
    if (this._tickTimer) clearInterval(this._tickTimer);
  },
  computed: {
    // Whichever blocked window resets furthest in the future is the one
    // actually still gating the composer (usageBlocked is true if ANY
    // window is blocked).
    freezeEndsAt() {
      if (!this.usage) return null;
      const candidates = ['window_5h', 'window_7d']
        .map((k) => this.usage[k])
        .filter((w) => w && w.is_blocked);
      if (!candidates.length) return null;
      const elapsedMs = this.now - (this.usage._fetchedAtMs || this.now);
      const latest = candidates.reduce((a, b) => (b.resets_in_seconds > a.resets_in_seconds ? b : a));
      const remainingMs = Math.max(0, latest.resets_in_seconds * 1000 - elapsedMs);
      return new Date(this.now + remainingMs);
    },
    freezeEndsAtLabel() {
      const d = this.freezeEndsAt;
      if (!d) return '';
      const sameDay = d.toDateString() === new Date(this.now).toDateString();
      const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      return sameDay ? time : `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })}, ${time}`;
    },
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
      // Purged/soft-deleted rows (deleted_at set) survive in the DB as
      // content=null tombstones so a live in-place delete can render
      // (ADR 0021) and so purge keeps the partition key (ADR 0050's
      // chat-wide reset) - but this view has no "message deleted" bubble
      // like MessageList.js does, so just drop them from what's shown.
      const list = this.messages.filter(m => !m.deleted_at);
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
  methods: {
    // Agent replies (type 7) and system messages (type 6, e.g. the
    // pause_and_escalate handoff notice, sender_id=null) render like
    // "theirs"; only the owner's own sends (any other type, always
    // sender_id=owner) render like "mine".
    isMine(m) { return m.type !== 7 && m.type !== 6; },
    // Global function from composables/messageFormat.js (shared with
    // MessageList.js): WhatsApp-style *bold* + "- " bullet-list rendering.
    formatMessageContent,
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
      if (this.usageBlocked) return;
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
    openPdfPicker() {
      this.$refs.pdfFileInput.value = '';
      this.$refs.pdfFileInput.click();
    },
    onPdfFileChosen(event) {
      const file = event.target.files && event.target.files[0];
      if (file) this.$emit('pick-pdf', file);
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
                  <span v-if="row.m.content" v-html="formatMessageContent(row.m.content)"></span>
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
      <p v-if="usageBlocked" class="shrink-0 px-3 py-1 text-[11px] text-rose-600 bg-rose-50 border-t border-rose-100 text-center">
        Usage limit reached - frozen until {{ freezeEndsAtLabel }}
      </p>
      <div class="shrink-0 border-t border-slate-200 p-2 flex items-center gap-2">
        <button type="button" @click="openPdfPicker" :disabled="usageBlocked"
                class="w-9 h-9 shrink-0 self-end rounded-full bg-slate-100 hover:bg-slate-200 text-slate-600 text-xl leading-none flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed"
                title="Attach PDF">+</button>
        <input ref="pdfFileInput" type="file" class="hidden" accept="application/pdf" @change="onPdfFileChosen" />
        <textarea ref="draftEl" v-model="draft" @keydown.enter="onEnter" @input="resizeDraft" :disabled="usageBlocked"
                  rows="1" placeholder="Message your agent…" dir="auto"
                  class="flex-1 min-w-0 px-3 py-1.5 text-sm border border-slate-300 rounded-2xl resize-none leading-normal max-h-36 overflow-y-auto disabled:bg-slate-100 disabled:cursor-not-allowed"></textarea>
        <button @click="submit" :disabled="!draft.trim() || usageBlocked"
                class="w-9 h-9 shrink-0 self-end flex items-center justify-center rounded-full bg-teal-700 text-white disabled:opacity-40 disabled:cursor-not-allowed">
          <svg viewBox="0 0 24 24" class="w-4 h-4" fill="currentColor"><path d="M3 20l18-8L3 4v6l12 2-12 2z"/></svg>
        </button>
      </div>
    </div>
  `,
};
