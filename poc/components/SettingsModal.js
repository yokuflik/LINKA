// Privacy settings, opened from the ⚙ gear in the top bar. Kept separate
// from ProfileEditModal (name/about/avatar) - the gear is settings only,
// the name/avatar button is profile only. Parent owns state (useSettings).
//
// Two controls, both on the single ADR-0002/0003 backend settings blob:
//   - privacy.online     : who may see the online indicator AND last-seen
//   - privacy.read_receipts : blue ticks, symmetric, 1:1 chats only
const SettingsModal = {
  props: {
    form: { type: Object, required: true },          // { privacy_online, privacy_read_receipts }
    onlineVisibilityOptions: { type: Array, required: true },
    busy: { type: Boolean, required: true },
    error: { type: String, default: '' },
  },
  emits: ['update:form', 'save', 'close'],
  methods: {
    setField(key, value) {
      this.$emit('update:form', { ...this.form, [key]: value });
    },
  },
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="$emit('close')">
      <div class="w-full max-w-sm bg-white rounded-xl shadow-lg border border-slate-200 p-4">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold">Settings</h2>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <label class="block text-xs font-medium text-slate-500 mb-1">Who can see when I'm online</label>
        <select :value="form.privacy_online" @change="setField('privacy_online', $event.target.value)"
                class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg bg-white mb-1">
          <option v-for="opt in onlineVisibilityOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
        </select>
        <p class="mb-3 text-xs text-slate-400">Also controls who can see your "last seen".</p>

        <label class="flex items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" :checked="form.privacy_read_receipts"
                 @change="setField('privacy_read_receipts', $event.target.checked)" />
          Read receipts
        </label>
        <p class="mt-1 mb-3 text-xs text-slate-400">If off, you won't send read receipts and won't see others' — in 1:1 chats only.</p>

        <p v-if="error" class="mb-2 text-xs text-red-600">{{ error }}</p>

        <div class="flex gap-2">
          <button @click="$emit('save')" :disabled="busy"
                  class="flex-1 px-3 py-1.5 text-sm font-medium bg-teal-700 text-white rounded-lg disabled:opacity-50">
            {{ busy ? 'Saving…' : 'Save' }}
          </button>
          <button @click="$emit('close')" :disabled="busy"
                  class="px-3 py-1.5 text-sm border border-slate-300 rounded-lg">Cancel</button>
        </div>
      </div>
    </div>
  `,
};
