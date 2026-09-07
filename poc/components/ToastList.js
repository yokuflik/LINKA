// Fixed top-right stack of auto-dismissing toast notifications.
// Each toast: { id, text, variant: 'info' | 'error' | 'success' }.
// Styled per variant (soft tinted background, matching icon), with a
// smooth slide/fade on enter and leave via <transition-group>.
const ToastList = {
  props: {
    toasts: { type: Array, required: true },
  },
  methods: {
    styleFor(variant) {
      if (variant === 'error') return 'bg-red-50 border-red-200 text-red-800';
      if (variant === 'success') return 'bg-emerald-50 border-emerald-200 text-emerald-800';
      return 'bg-slate-900 border-slate-900 text-white';
    },
  },
  template: `
    <div class="fixed top-3 right-3 flex flex-col gap-2 z-50 pointer-events-none">
      <transition-group name="toast">
        <div v-for="t in toasts" :key="t.id"
             :class="styleFor(t.variant)"
             class="pointer-events-auto flex items-start gap-2 max-w-xs text-sm px-3 py-2 rounded-xl border shadow-lg">
          <svg v-if="t.variant === 'error'" class="w-4 h-4 mt-0.5 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2z" clip-rule="evenodd" />
          </svg>
          <svg v-else-if="t.variant === 'success'" class="w-4 h-4 mt-0.5 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path fill-rule="evenodd" d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.146z" clip-rule="evenodd" />
          </svg>
          <span class="leading-snug">{{ t.text }}</span>
        </div>
      </transition-group>
    </div>
  `,
};
