// Right-click menu on a chat row in the sidebar. Two options for now:
// "Pin to top" / "Unpin" (wired to the backend), and "Mute" (UI-only stub -
// no mute logic exists yet, so it just shows a toast and closes).
const ChatContextMenu = {
  props: {
    position: { type: Object, required: true }, // { x, y } - viewport-clamped by caller
    pinned: { type: Boolean, default: false },
  },
  emits: ['close', 'toggle-pin', 'toggle-mute'],
  template: `
    <div class="fixed inset-0 z-50" @click="$emit('close')" @contextmenu.prevent="$emit('close')">
      <div class="absolute w-44 bg-white rounded-lg shadow-lg border border-slate-200 py-1 text-sm"
           :style="{ top: position.y + 'px', left: position.x + 'px' }" @click.stop>
        <button @click="$emit('toggle-pin')" class="w-full text-left px-3 py-2 hover:bg-slate-50">
          {{ pinned ? 'Unpin' : 'Pin to top' }}
        </button>
        <button @click="$emit('toggle-mute')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Mute</button>
      </div>
    </div>
  `,
};
