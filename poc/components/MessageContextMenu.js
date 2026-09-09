// Right-click menu on a message bubble. "Reply", "Forward", "Details" and
// "Delete" are wired up (Details only for your own real messages - it shows the
// sent/delivered/read receipt progress; Delete only for a message you sent).
// Forward opens the "Forward to…" picker (ADR 0020) for any real, non-system
// message - yours or someone else's. "Save to device" (media messages only)
// fetches the presigned bytes into a blob and triggers a real browser download. Edit is wired up for your own text
// messages only; Delete for any of your own messages. Both are hidden entirely
// on messages you didn't send. "Delete forever" (ADR 0021) shows on your own
// already-soft-deleted message and purges it irreversibly (content wiped, media
// removed from S3).
const MessageContextMenu = {
  props: {
    position: { type: Object, required: true }, // { x, y } - already viewport-clamped by the caller
    canShowDetails: { type: Boolean, default: false }, // own, non-system, non-deleted message
    canCopy: { type: Boolean, default: false }, // message has text content to copy
    canForward: { type: Boolean, default: false }, // real, non-system message (media key recoverable)
    canSaveMedia: { type: Boolean, default: false }, // media message - offer "Save to device"
    canEdit: { type: Boolean, default: false }, // own, non-deleted text message
    canDelete: { type: Boolean, default: false }, // own, non-system, non-deleted message
    canRestore: { type: Boolean, default: false }, // own, currently-deleted message
    canPurge: { type: Boolean, default: false }, // own, soft-deleted, not-yet-purged message (ADR 0021)
  },
  emits: ['close', 'reply', 'copy', 'forward', 'save-media', 'details', 'edit', 'delete', 'restore', 'purge'],
  template: `
    <div class="fixed inset-0 z-50" @click="$emit('close')" @contextmenu.prevent="$emit('close')">
      <div class="absolute w-40 bg-white rounded-lg shadow-lg border border-slate-200 py-1 text-sm"
           :style="{ top: position.y + 'px', left: position.x + 'px' }" @click.stop>
        <button @click="$emit('reply')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Reply</button>
        <button v-if="canCopy" @click="$emit('copy')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Copy</button>
        <button v-if="canShowDetails" @click="$emit('details')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Details</button>
        <button v-if="canForward" @click="$emit('forward')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Forward</button>
        <button v-if="canSaveMedia" @click="$emit('save-media')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Save to device</button>
        <button v-if="canEdit" @click="$emit('edit')" class="w-full text-left px-3 py-2 hover:bg-slate-50">Edit</button>
        <button v-if="canDelete" @click="$emit('delete')" class="w-full text-left px-3 py-2 text-red-600 hover:bg-red-50">Delete</button>
        <button v-if="canRestore" @click="$emit('restore')" class="w-full text-left px-3 py-2 text-emerald-600 hover:bg-emerald-50">Restore</button>
        <button v-if="canPurge" @click="$emit('purge')" class="w-full text-left px-3 py-2 text-red-600 hover:bg-red-50">Delete forever</button>
      </div>
    </div>
  `,
};
