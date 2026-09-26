// Token-usage indicator for the AI agent drawer (ADR 0059). A circular ring
// button, filled by and labeled with the 5h window's percentage, sits in the
// drawer's header - clicking it toggles a popover with both linear progress
// bars (5h "session" + 7d "weekly"), each with a percentage and a live resets-in
// countdown (5h shows H:MM, 7d shows Nd Hh). This popover is the only place
// the 7d window's detail is shown - the ring itself only ever reflects the 5h
// window.
const UsageProgressBar = {
  props: {
    usage: { type: Object, default: null }, // {window_5h, window_7d} | null
  },
  data() {
    return { now: Date.now(), popoverOpen: false };
  },
  computed: {
    windows() {
      if (!this.usage) return [];
      return [
        { key: '5h', label: 'Session (5h)', data: this.usage.window_5h },
        { key: '7d', label: 'Weekly (7d)', data: this.usage.window_7d },
      ].filter((w) => w.data);
    },
    ringPercent() {
      return this.usage && this.usage.window_5h ? Math.min(100, this.usage.window_5h.percent) : 0;
    },
    ringBlocked() {
      const u = this.usage;
      if (!u) return false;
      return !!(u.window_5h && u.window_5h.is_blocked) || !!(u.window_7d && u.window_7d.is_blocked);
    },
    // The frozen banner shows whichever window resets furthest in the
    // future among the blocked ones - that's the one actually gating the
    // composer (AgentChatView disables it while ANY window is blocked).
    blockedWindow() {
      const blocked = this.windows.filter((w) => w.data.is_blocked);
      if (!blocked.length) return null;
      return blocked.reduce((a, b) => (this.remainingSeconds(b) > this.remainingSeconds(a) ? b : a));
    },
    ringDashoffset() {
      const r = 15;
      const circumference = 2 * Math.PI * r;
      return circumference * (1 - this.ringPercent / 100);
    },
  },
  mounted() {
    this._tickTimer = setInterval(() => { this.now = Date.now(); }, 1000);
    this._onDocClick = (e) => {
      if (this.popoverOpen && this.$refs.root && !this.$refs.root.contains(e.target)) this.popoverOpen = false;
    };
    document.addEventListener('click', this._onDocClick);
  },
  beforeUnmount() {
    if (this._tickTimer) clearInterval(this._tickTimer);
    document.removeEventListener('click', this._onDocClick);
  },
  methods: {
    // Ticks down from the value fetched at load time - fetchedAtMs anchors
    // the countdown so it stays accurate between polls (useAgentConfig.js
    // refetches /agents/me/usage every 30s; this fills the gap in between).
    remainingSeconds(w) {
      const elapsed = Math.floor((this.now - (this.usage._fetchedAtMs || this.now)) / 1000);
      return Math.max(0, w.data.resets_in_seconds - elapsed);
    },
    // 5h window: H:MM (no seconds). 7d window: Nd Hh (days + hours, no minutes).
    formatResetTime(w) {
      const total = this.remainingSeconds(w);
      const pad = (n) => String(n).padStart(2, '0');
      if (w.key === '7d') {
        const d = Math.floor(total / 86400);
        const h = Math.floor((total % 86400) / 3600);
        return d > 0 ? `${d}d ${h}h` : `${h}h`;
      }
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      return h > 0 ? `${h}:${pad(m)}` : `0:${pad(m)}`;
    },
    barColor(w) {
      if (w.data.is_blocked) return 'bg-rose-500';
      if (w.data.percent >= 80) return 'bg-amber-500';
      return 'bg-teal-600';
    },
    togglePopover() {
      this.popoverOpen = !this.popoverOpen;
    },
  },
  template: `
    <div v-if="usage" ref="root" class="relative">
      <button type="button" @click.stop="togglePopover"
              class="relative w-12 h-12 flex items-center justify-center rounded-full hover:bg-slate-100"
              :title="ringPercent + '% of session token budget used'">
        <svg viewBox="0 0 36 36" class="w-11 h-11 -rotate-90">
          <circle cx="18" cy="18" r="15" fill="none" stroke="#e2e8f0" stroke-width="3"></circle>
          <circle cx="18" cy="18" r="15" fill="none" stroke-width="3" stroke-linecap="round"
                  :stroke="ringBlocked ? '#f43f5e' : (ringPercent >= 80 ? '#f59e0b' : '#0f766e')"
                  :stroke-dasharray="2 * Math.PI * 15"
                  :stroke-dashoffset="ringDashoffset"
                  class="transition-all"></circle>
        </svg>
        <span class="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-slate-700">{{ ringPercent }}%</span>
      </button>

      <div v-if="popoverOpen" class="absolute right-0 top-9 z-10 w-64 rounded-lg border border-slate-200 bg-white shadow-lg p-3 space-y-2">
        <div v-for="w in windows" :key="w.key" class="text-[11px]">
          <div class="flex items-center justify-between mb-0.5 text-slate-500">
            <span>{{ w.label }}</span>
            <span>{{ w.data.percent }}% · Resets in {{ formatResetTime(w) }}</span>
          </div>
          <div class="h-1.5 rounded-full bg-slate-200 overflow-hidden">
            <div class="h-full rounded-full transition-all" :class="barColor(w)"
                 :style="{ width: Math.min(100, w.data.percent) + '%' }"></div>
          </div>
        </div>
      </div>
    </div>
  `,
};
