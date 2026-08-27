// "Details" popup for a message (opened from the right-click menu). Shows,
// per participant, when the message was Delivered / Read / Played, pulled
// from GET /chats/{id}/messages/{mid}/receipts (the message_receipt_log -
// see CLAUDE.md). Works for any message, sent by anyone: in a 1:1 chat it
// collapses to three timestamped rows; in a group it lists who reached each
// state (and who is still pending). A very large group returns counts only
// (`truncated`), and this renders those instead of a name list.
const MessageDetailsModal = {
  props: {
    message: { type: Object, required: true },
    receipts: { type: Object, default: null }, // MessageReceiptsOut, or null while loading
    loading: { type: Boolean, default: false },
    error: { type: String, default: '' },
    snippet: { type: String, default: '' },
    nameFor: { type: Function, required: true }, // (userId) => display string
  },
  emits: ['close'],
  methods: {
    fmt(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      return d.toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    },
    entriesFor(key) {
      const r = this.receipts;
      if (!r || !Array.isArray(r[key])) return [];
      return r[key]
        .map((e) => ({ name: this.nameFor(e.user_id), at: this.fmt(e.occurred_at) }))
        .sort((a, b) => a.name.localeCompare(b.name));
    },
  },
  computed: {
    isGroup() { return !!this.receipts && this.receipts.is_group; },
    isAudio() { return !!this.receipts && this.receipts.message_type === 4; },
    total() { return this.receipts ? this.receipts.participant_count : 0; },
    counts() { return (this.receipts && this.receipts.counts) || { delivered: 0, read: 0, played: 0 }; },
    // 1:1 collapsed rows.
    oneToOneRows() {
      const r = this.receipts;
      if (!r) return [];
      const first = (k) => (Array.isArray(r[k]) && r[k][0] ? this.fmt(r[k][0].occurred_at) : null);
      const rows = [
        { key: 'sent', label: 'Sent', at: this.message ? this.fmt(this.message.created_at) : null, reached: true },
        { key: 'delivered', label: 'Delivered', at: first('delivered_by'), reached: !!first('delivered_by') },
        { key: 'read', label: 'Read', at: first('read_by'), reached: !!first('read_by') },
      ];
      if (this.isAudio) rows.push({ key: 'played', label: 'Played', at: first('played_by'), reached: !!first('played_by') });
      return rows;
    },
    pendingNames() {
      const r = this.receipts;
      if (!r || !Array.isArray(r.pending)) return [];
      return r.pending.map((uid) => this.nameFor(uid)).sort((a, b) => a.localeCompare(b));
    },
  },
  template: `
    <div class="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" @click="$emit('close')">
      <div class="bg-white rounded-xl shadow-xl w-full max-w-sm p-5 max-h-[80vh] overflow-y-auto" @click.stop>
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold text-slate-800">Message info</h3>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <p v-if="snippet" class="text-xs text-slate-500 italic mb-4 line-clamp-2 border-l-2 border-slate-200 pl-2">{{ snippet }}</p>

        <div v-if="loading" class="text-sm text-slate-400 py-4 text-center">Loading…</div>
        <div v-else-if="error" class="text-sm text-rose-500 py-4 text-center">{{ error }}</div>

        <template v-else-if="receipts">
          <!-- Large group: counts only -->
          <ul v-if="receipts.truncated" class="space-y-2 text-sm text-slate-700">
            <li class="flex justify-between"><span>Delivered</span><span class="text-slate-500">{{ counts.delivered }} / {{ total }}</span></li>
            <li class="flex justify-between"><span>Read</span><span class="text-slate-500">{{ counts.read }} / {{ total }}</span></li>
            <li v-if="isAudio" class="flex justify-between"><span>Played</span><span class="text-slate-500">{{ counts.played }} / {{ total }}</span></li>
            <li class="text-[11px] text-slate-400 pt-1">Group too large to list names.</li>
          </ul>

          <!-- 1:1 chat: timestamped checklist -->
          <ul v-else-if="!isGroup" class="space-y-3">
            <li v-for="s in oneToOneRows" :key="s.key" class="flex items-start gap-3">
              <span class="mt-0.5 text-sm font-bold leading-none w-4 text-center"
                    :class="s.reached ? (s.key === 'read' || s.key === 'played' ? 'text-sky-500' : 'text-emerald-500') : 'text-slate-300'">
                {{ s.reached ? '✓' : '·' }}
              </span>
              <div class="flex-1">
                <div class="text-sm" :class="s.reached ? 'text-slate-800' : 'text-slate-400'">{{ s.label }}</div>
                <div v-if="s.reached && s.at" class="text-[11px] text-slate-400">{{ s.at }}</div>
                <div v-else-if="!s.reached" class="text-[11px] text-slate-300">Not yet</div>
              </div>
            </li>
          </ul>

          <!-- Group: who reached each state -->
          <div v-else class="space-y-4">
            <div v-if="isAudio">
              <div class="text-xs font-semibold text-sky-600 mb-1">Played by ({{ counts.played }}/{{ total }})</div>
              <ul class="space-y-1">
                <li v-for="e in entriesFor('played_by')" :key="'p'+e.name" class="flex justify-between text-sm">
                  <span class="text-slate-700">{{ e.name }}</span><span class="text-[11px] text-slate-400">{{ e.at }}</span>
                </li>
                <li v-if="!entriesFor('played_by').length" class="text-[11px] text-slate-300">Nobody yet</li>
              </ul>
            </div>
            <div>
              <div class="text-xs font-semibold text-sky-600 mb-1">Read by ({{ counts.read }}/{{ total }})</div>
              <ul class="space-y-1">
                <li v-for="e in entriesFor('read_by')" :key="'r'+e.name" class="flex justify-between text-sm">
                  <span class="text-slate-700">{{ e.name }}</span><span class="text-[11px] text-slate-400">{{ e.at }}</span>
                </li>
                <li v-if="!entriesFor('read_by').length" class="text-[11px] text-slate-300">Nobody yet</li>
              </ul>
            </div>
            <div>
              <div class="text-xs font-semibold text-emerald-600 mb-1">Delivered to ({{ counts.delivered }}/{{ total }})</div>
              <ul class="space-y-1">
                <li v-for="e in entriesFor('delivered_by')" :key="'d'+e.name" class="flex justify-between text-sm">
                  <span class="text-slate-700">{{ e.name }}</span><span class="text-[11px] text-slate-400">{{ e.at }}</span>
                </li>
                <li v-if="!entriesFor('delivered_by').length" class="text-[11px] text-slate-300">Nobody yet</li>
              </ul>
            </div>
            <div v-if="pendingNames.length">
              <div class="text-xs font-semibold text-slate-400 mb-1">Pending ({{ pendingNames.length }})</div>
              <ul class="space-y-1">
                <li v-for="n in pendingNames" :key="'x'+n" class="text-sm text-slate-400">{{ n }}</li>
              </ul>
            </div>
          </div>
        </template>
      </div>
    </div>
  `,
};
