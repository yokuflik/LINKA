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
      <div class="absolute w-44 bg-white rounded-lg shadow-lg border border-slate-200 py-1 text-sm"
           :style="{ top: position.y + 'px', left: position.x + 'px' }" @click.stop>
        <button @click="$emit('reply')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 hover:bg-slate-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 17 4 12l5-5" /><path d="M4 12h11a5 5 0 0 1 5 5v2" />
          </svg>
          <span>Reply</span>
        </button>
        <button v-if="canCopy" @click="$emit('copy')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 hover:bg-slate-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="9" width="11" height="11" rx="2" /><path d="M6 15H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1" />
          </svg>
          <span>Copy</span>
        </button>
        <button v-if="canShowDetails" @click="$emit('details')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 hover:bg-slate-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 8h.01" />
          </svg>
          <span>Details</span>
        </button>
        <button v-if="canForward" @click="$emit('forward')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 hover:bg-slate-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M15 17 20 12l-5-5" /><path d="M20 12H9a5 5 0 0 0-5 5v2" />
          </svg>
          <span>Forward</span>
        </button>
        <button v-if="canSaveMedia" @click="$emit('save-media')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 hover:bg-slate-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 19h14" />
          </svg>
          <span>Save to device</span>
        </button>
        <button v-if="canEdit" @click="$emit('edit')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 hover:bg-slate-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
          </svg>
          <span>Edit</span>
        </button>
        <button v-if="canDelete" @click="$emit('delete')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 text-red-600 hover:bg-red-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 6h18" /><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6" /><path d="M14 11v6" />
          </svg>
          <span>Delete</span>
        </button>
        <button v-if="canRestore" @click="$emit('restore')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 text-emerald-600 hover:bg-emerald-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" />
          </svg>
          <span>Restore</span>
        </button>
        <button v-if="canPurge" @click="$emit('purge')" class="w-full flex items-center gap-2.5 text-left px-3 py-2 text-red-600 hover:bg-red-50">
          <svg viewBox="0 0 24 24" class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 6h18" /><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="m9.5 11 5 6" /><path d="m14.5 11-5 6" />
          </svg>
          <span>Delete forever</span>
        </button>
      </div>
    </div>
  `,
};
