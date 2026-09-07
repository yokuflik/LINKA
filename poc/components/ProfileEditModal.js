// Shared edit form for a profile that has an avatar + a name + an "about"
// line - used for both the current user (from the top bar) and a group
// (from the members modal). The parent owns all state/logic (useProfileEdit);
// this only renders the form and emits picker + save/close intents.
//
// The avatar picker mirrors AuthScreen's sign-up picker: a picked file is
// previewed locally and only uploaded when the parent's save() runs.
const ProfileEditModal = {
  props: {
    heading: { type: String, required: true },
    // The generic name field (label + form key). Omit both for the user
    // profile, which identifies by username instead - see showUsername.
    nameLabel: { type: String, default: '' },
    aboutLabel: { type: String, required: true },
    form: { type: Object, required: true },           // { title?, about_text, username? }
    nameKey: { type: String, default: '' },           // 'title' for groups, '' for the user
    // When set, a "Username" field is shown under the name (user profile only,
    // not groups). usernameCheck: { status:''|'checking'|'ok'|'bad', reason }.
    showUsername: { type: Boolean, default: false },
    usernameCheck: { type: Object, default: () => ({ status: '', reason: '' }) },
    currentAvatarUrl: { default: null },
    avatarName: { default: '' },
    previewUrl: { default: null },
    cleared: { type: Boolean, default: false },
    pickerError: { type: String, default: '' },
    busy: { type: Boolean, required: true },
    error: { type: String, default: '' },
  },
  emits: ['update:form', 'pick-avatar', 'clear-avatar', 'save', 'close', 'check-username'],
  computed: {
    shownAvatarUrl() {
      if (this.previewUrl) return this.previewUrl;
      if (this.cleared) return null;
      return this.currentAvatarUrl;
    },
    usernameHint() {
      const c = this.usernameCheck || {};
      if (c.status === 'checking') return { text: 'Checking…', cls: 'text-slate-400' };
      if (c.status === 'ok') return { text: 'Available', cls: 'text-green-600' };
      if (c.status === 'bad') {
        const map = {
          too_short: 'Too short (min 3)', too_long: 'Too long (max 32)',
          bad_chars: 'Letters, digits and _ only', must_start_letter: 'Must start with a letter',
          reserved: 'That handle is reserved', taken: 'Already taken',
          grace_hold: 'Recently released — not available yet', cooldown: 'On change cooldown',
        };
        return { text: map[c.reason] || 'Not available', cls: 'text-red-600' };
      }
      return { text: 'You can change this any time.', cls: 'text-slate-400' };
    },
  },
  methods: {
    setField(key, value) {
      this.$emit('update:form', { ...this.form, [key]: value });
      if (key === 'username') this.$emit('check-username');
    },
    onAvatarChange(event) {
      const file = event.target.files && event.target.files[0];
      if (file) this.$emit('pick-avatar', file);
      event.target.value = '';
    },
  },
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="$emit('close')">
      <div class="w-full max-w-sm bg-white rounded-xl shadow-lg border border-slate-200 p-4">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold">{{ heading }}</h2>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <div class="flex flex-col items-center mb-4">
          <div class="relative">
            <Avatar :url="shownAvatarUrl" :enlargeable="false" :name="avatarName" :colorKey="avatarName" sizeClass="w-20 h-20 text-2xl" />
            <label class="absolute -bottom-1 -right-1 w-7 h-7 rounded-full bg-teal-700 text-white flex items-center justify-center text-sm cursor-pointer">
              ✎
              <input type="file" accept="image/jpeg,image/png,image/webp" class="hidden" @change="onAvatarChange" />
            </label>
          </div>
          <button v-if="shownAvatarUrl" type="button" @click="$emit('clear-avatar')"
                  class="mt-1 text-xs text-slate-400">Remove photo</button>
          <p v-if="pickerError" class="mt-1 text-xs text-red-600">{{ pickerError }}</p>
        </div>

        <template v-if="nameKey">
          <label class="block text-xs font-medium text-slate-500 mb-1">{{ nameLabel }}</label>
          <input :value="form[nameKey]" @input="setField(nameKey, $event.target.value)"
                 class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg mb-3" />
        </template>

        <template v-if="showUsername">
          <label class="block text-xs font-medium text-slate-500 mb-1">Username</label>
          <input :value="form.username"
                 @input="setField('username', $event.target.value.toLowerCase())"
                 placeholder="jane_doe"
                 class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg font-mono" />
          <p class="mb-3 mt-1 text-xs" :class="usernameHint.cls">{{ usernameHint.text }}</p>
        </template>

        <label class="block text-xs font-medium text-slate-500 mb-1">{{ aboutLabel }}</label>
        <textarea :value="form.about_text" @input="setField('about_text', $event.target.value)" rows="2"
                  class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg mb-3 resize-none"></textarea>

        <p v-if="error" class="mb-2 text-xs text-red-600">{{ error }}</p>

        <div class="flex gap-2">
          <button @click="$emit('save')" :disabled="busy || (showUsername && usernameCheck.status === 'bad')"
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
