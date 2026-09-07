// Standardized inline validation / error feedback for a form or panel.
// Renders nothing when :message is falsy, so it can be dropped in place of
// the old  <p v-if="someError" class="text-red-600">{{ someError }}</p>  blocks:
//   <InlineAlert :message="someError" />
// variant: 'error' (default) | 'warning'. Keep the message a short, polite,
// non-technical sentence (see core.js friendlyError).
const InlineAlert = {
  props: {
    message: { type: String, default: '' },
    variant: { type: String, default: 'error' },
  },
  computed: {
    cls() {
      return this.variant === 'warning'
        ? 'bg-amber-50 border-amber-200 text-amber-800'
        : 'bg-red-50 border-red-200 text-red-700';
    },
  },
  template: `
    <div v-if="message" :class="cls"
         class="flex items-start gap-1.5 text-xs rounded-lg border px-2.5 py-1.5">
      <svg class="w-3.5 h-3.5 mt-px shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
        <path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2z" clip-rule="evenodd" />
      </svg>
      <span class="leading-snug">{{ message }}</span>
    </div>
  `,
};
