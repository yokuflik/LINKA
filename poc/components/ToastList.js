// Fixed top-right stack of auto-dismissing toast notifications.
const ToastList = {
  props: {
    toasts: { type: Array, required: true },
  },
  template: `
    <div class="fixed top-3 right-3 flex flex-col gap-2 z-50">
      <div v-for="t in toasts" :key="t.id"
           class="bg-slate-900 text-white text-sm px-3 py-2 rounded-lg shadow-lg">{{ t.text }}</div>
    </div>
  `,
};
