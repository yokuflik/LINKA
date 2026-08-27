// Right-click menu on a message bubble. "Reply" and "Details" are wired up
// (Details only for your own real messages - it shows the sent/delivered/read
// receipt progress). Forward/Edit/Delete are shown but disabled (see
// CLAUDE.md's "not yet built" note) so the menu's final shape is visible
// without pretending those work.
const MessageContextMenu = {
  props: {
    position: { type: Object, required: true }, // { x, y } - already viewport-clamped by the caller
    canShowDetails: { type: Boolean, default: false }, // own, non-system, non-deleted message
  },
  emits: ['close', 'reply', 'details'],
  template: `
    <div class="fixed inset-0 z-50" @click="$emit('close')" @contextmenu.prevent="$emit('close')">
      <div class="absolute w-40 bg-white rounded-lg shadow-lg border border-slate-200 py-1 text-sm"
           :style="{ top: position.y + 'px', left: position.x + 'px' }" @click.stop>
        <button @click="$emit('reply')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Reply</button>
        <button v-if="canShowDetails" @click="$emit('details')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Details</button>
        <button disabled class="w-full flex items-center justify-between px-3 py-2 text-slate-300 cursor-not-allowed">
          Forward <span class="text-[10px]">🔒</span>
        </button>
        <button disabled class="w-full flex items-center justify-between px-3 py-2 text-slate-300 cursor-not-allowed">
          Edit <span class="text-[10px]">🔒</span>
        </button>
        <button disabled class="w-full flex items-center justify-between px-3 py-2 text-slate-300 cursor-not-allowed">
          Delete <span class="text-[10px]">🔒</span>
        </button>
      </div>
    </div>
  `,
};
