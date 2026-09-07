// Bottom-center stack of auto-dismissing toast notifications (2026 style):
// frosted floating pills with a soft coloured accent dot, a spring-ish
// scale/blur entrance (CSS in index.html), and a slide as siblings settle.
// Each toast: { id, text, variant: 'info' | 'error' | 'success' }.
const ToastList = {
  props: {
    toasts: { type: Array, required: true },
  },
  methods: {
    // Frosted surface + per-variant accent. The pill itself stays neutral
    // (glass), the meaning is carried by the leading icon chip.
    chipFor(variant) {
      if (variant === 'error') return 'bg-red-500/15 text-red-600';
      if (variant === 'success') return 'bg-emerald-500/15 text-emerald-600';
      return 'bg-slate-500/15 text-slate-600';
    },
  },
  template: `
    <div class="fixed inset-x-0 bottom-[calc(env(safe-area-inset-bottom,0px)+1rem)] z-50
                flex flex-col items-center gap-2 px-4 pointer-events-none">
      <transition-group name="toast">
        <div v-for="t in toasts" :key="t.id"
             class="pointer-events-auto flex items-center gap-2.5 max-w-[22rem]
                    pl-2 pr-3.5 py-2 rounded-full
                    bg-white/80 backdrop-blur-xl backdrop-saturate-150
                    border border-black/5 text-slate-800 text-[13px] font-medium
                    shadow-[0_8px_30px_-8px_rgba(0,0,0,0.25)]">
          <span class="shrink-0 w-6 h-6 rounded-full flex items-center justify-center"
                :class="chipFor(t.variant)">
            <svg v-if="t.variant === 'error'" class="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fill-rule="evenodd" d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM10 5.5a.75.75 0 0 1 .75.75v4a.75.75 0 0 1-1.5 0v-4A.75.75 0 0 1 10 5.5zM10 14a1 1 0 1 0 0-2 1 1 0 0 0 0 2z" clip-rule="evenodd" />
            </svg>
            <svg v-else-if="t.variant === 'success'" class="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fill-rule="evenodd" d="M16.704 5.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.146z" clip-rule="evenodd" />
            </svg>
            <svg v-else class="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fill-rule="evenodd" d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM10 6a1 1 0 1 0 0-2 1 1 0 0 0 0 2zm.75 3a.75.75 0 0 0-1.5 0v5a.75.75 0 0 0 1.5 0V9z" clip-rule="evenodd" />
            </svg>
          </span>
          <span class="leading-snug py-0.5">{{ t.text }}</span>
        </div>
      </transition-group>
    </div>
  `,
};
