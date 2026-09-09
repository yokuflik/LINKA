// Compose-and-schedule a message (ADR 0026), opened from the [+] attach menu's
// "⏰ Schedule message" entry. Same modal style as SettingsModal. Parent owns
// state (useScheduleMessage): message text, an optional attached file, a
// datetime-local picker + quick presets. On submit the parent converts the
// local time to absolute UTC and POSTs /chats/{id}/scheduled-messages.
const ScheduleMessageModal = {
  props: {
    form: { type: Object, required: true },   // { content, scheduled_for, media, editingId, hasMedia }
    busy: { type: Boolean, default: false },
    error: { type: String, default: '' },
  },
  emits: ['update:form', 'submit', 'preset-1h', 'preset-tonight', 'preset-tomorrow', 'close'],
  computed: {
    isEdit() { return !!this.form.editingId; },
    mediaName() {
      if (this.form.media && this.form.media.file) return this.form.media.file.name;
      return '';
    },
  },
  methods: {
    setField(key, value) {
      this.$emit('update:form', { ...this.form, [key]: value });
    },
  },
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="$emit('close')">
      <div class="w-full max-w-sm bg-white rounded-xl shadow-lg border border-slate-200 p-4">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold">{{ isEdit ? 'Edit scheduled message' : 'Schedule message' }}</h2>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <div v-if="mediaName" class="mb-2 flex items-center gap-2 rounded-lg bg-slate-50 border border-slate-200 px-3 py-2 text-sm text-slate-600">
          <span>📎</span><span class="truncate">{{ mediaName }}</span>
        </div>
        <p v-else-if="isEdit && form.hasMedia" class="mb-2 text-xs text-slate-400">
          Editing the caption only. To change the attachment, cancel and schedule a new one.
        </p>

        <label class="block text-xs font-medium text-slate-500 mb-1">Message</label>
        <textarea :value="form.content" @input="setField('content', $event.target.value)"
                  rows="3" placeholder="Type your message…"
                  class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg mb-3 resize-none"></textarea>

        <label class="block text-xs font-medium text-slate-500 mb-1">Send at</label>
        <input type="datetime-local" :value="form.scheduled_for"
               @input="setField('scheduled_for', $event.target.value)"
               class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg mb-2" />
        <div class="flex flex-wrap gap-2 mb-3">
          <button type="button" @click="$emit('preset-1h')"
                  class="px-2 py-1 text-xs border border-slate-300 rounded-full hover:bg-slate-50">In 1 hour</button>
          <button type="button" @click="$emit('preset-tonight')"
                  class="px-2 py-1 text-xs border border-slate-300 rounded-full hover:bg-slate-50">Tonight 8pm</button>
          <button type="button" @click="$emit('preset-tomorrow')"
                  class="px-2 py-1 text-xs border border-slate-300 rounded-full hover:bg-slate-50">Tomorrow 9am</button>
        </div>

        <InlineAlert :message="error" class="mb-2" />

        <div class="flex gap-2">
          <button @click="$emit('submit')" :disabled="busy"
                  class="flex-1 px-3 py-1.5 text-sm font-medium bg-teal-700 text-white rounded-lg disabled:opacity-50">
            {{ busy ? 'Scheduling…' : (isEdit ? 'Save' : 'Schedule') }}
          </button>
          <button @click="$emit('close')" :disabled="busy"
                  class="px-3 py-1.5 text-sm border border-slate-300 rounded-lg">Cancel</button>
        </div>
      </div>
    </div>
  `,
};

// List of the active chat's pending scheduled messages, with Edit / Cancel.
const ScheduleListModal = {
  props: {
    items: { type: Array, required: true },
    relativeLabel: { type: Function, required: true },
  },
  emits: ['edit', 'cancel', 'close'],
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="$emit('close')">
      <div class="w-full max-w-sm bg-white rounded-xl shadow-lg border border-slate-200 p-4 max-h-[85vh] flex flex-col">
        <div class="flex items-center justify-between mb-3 shrink-0">
          <h2 class="text-sm font-semibold">Scheduled messages</h2>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>
        <div class="flex-1 min-h-0 overflow-y-auto -mx-1 px-1 space-y-2">
          <div v-for="row in items" :key="row.id"
               class="rounded-lg border border-slate-200 p-3">
            <div class="text-sm text-slate-800 break-words">
              <span v-if="row.message_type !== 1">📎 </span>{{ row.content || (row.message_type !== 1 ? (row.media_name || 'Attachment') : '(no text)') }}
            </div>
            <div class="mt-1 text-xs text-slate-400">{{ relativeLabel(row.scheduled_for) }}</div>
            <div class="mt-2 flex gap-2">
              <button @click="$emit('edit', row)"
                      class="px-2 py-1 text-xs border border-slate-300 rounded-lg hover:bg-slate-50">Edit</button>
              <button @click="$emit('cancel', row)"
                      class="px-2 py-1 text-xs text-red-600 border border-red-200 rounded-lg hover:bg-red-50">Cancel</button>
            </div>
          </div>
          <p v-if="!items.length" class="text-sm text-slate-400 text-center py-6">No scheduled messages.</p>
        </div>
      </div>
    </div>
  `,
};
