// New-group creation: a two-step wizard.
//
//   Step 1 "Add members" - a search row (exact-match user lookup by
//     username or phone, same logic as the new-chat search) over a
//     scrollable list. The list shows your private-chat peers first, in
//     sidebar order; an exact-match search hit is shown above them. Tap a
//     row to toggle-select; selected members appear as removable chips.
//   Step 2 "Group details" - the group photo, name and description, then
//     "Create group".
//
// All the real work (POST /chats/groups, avatar upload) still lives in the
// root (useNewChat.createGroupChat); this only owns the form markup and
// emits 'create' with { title, about, memberUsers, photoFile }.
const NewGroupModal = {
  props: {
    busy: { type: Boolean, default: false },
    error: { type: String, default: '' },
    // Step-1 member picker inputs (from useNewChat on the root).
    peers: { type: Array, default: () => [] },        // [UserOut] in chat order
    searchQuery: { type: String, required: true },    // v-model:searchQuery
    searchBusy: { type: Boolean, default: false },
    searchError: { type: String, default: '' },
    searchResult: { type: Object, default: null },    // a UserOut, or null
    userLabel: { type: Function, required: true },     // (user) -> display name
    userAvatarUrl: { type: Function, required: true },
    userAvatarPreview: { type: Function, required: true },
  },
  emits: ['close', 'create', 'update:searchQuery', 'input-search', 'search'],
  data() {
    return {
      step: 1,
      selected: [],           // [UserOut]
      title: '',
      about: '',
      photoFile: null,
      photoPreviewUrl: null,
      photoError: '',
    };
  },
  computed: {
    selectedIds() {
      return new Set(this.selected.map((u) => String(u.id)));
    },
    // Peers minus the current search hit (it's rendered separately on top).
    // While the user is searching, only peers whose name/phone match the
    // query are shown - the rest of the existing chats disappear.
    peerRows() {
      const hitId = this.searchResult ? String(this.searchResult.id) : null;
      const q = this.searchQuery.trim().toLowerCase().replace(/^@/, '');
      const qDigits = q.replace(/\D/g, '');
      return this.peers.filter((u) => {
        if (String(u.id) === hitId) return false;
        if (!q) return true;
        const name = (this.userLabel(u) || '').toLowerCase();
        if (name.includes(q)) return true;
        const phone = String(u.phone_number || '').replace(/\D/g, '');
        return qDigits.length >= 2 && phone.includes(qDigits);
      });
    },
  },
  methods: {
    onSearchInput(e) {
      this.$emit('update:searchQuery', e.target.value);
      this.$emit('input-search');
    },
    // Keep the caret in the search box after a debounced/manual search
    // re-renders the results list.
    refocusSearch() {
      this.$nextTick(() => {
        const el = this.$refs.searchInput;
        if (el && document.activeElement !== el) el.focus();
      });
    },
    isSelected(user) {
      return this.selectedIds.has(String(user.id));
    },
    toggle(user) {
      const id = String(user.id);
      const i = this.selected.findIndex((u) => String(u.id) === id);
      if (i >= 0) this.selected.splice(i, 1);
      else this.selected.push(user);
    },
    // --- step 2 photo picker (unchanged from the old modal) ----------------
    onPhotoChange(event) {
      const file = event.target.files && event.target.files[0];
      event.target.value = '';
      if (!file) return;
      this.photoError = '';
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
        this.photoError = 'Use a JPEG, PNG or WebP image.';
        return;
      }
      if (file.size <= 0) { this.photoError = 'That file looks empty.'; return; }
      if (file.size > 25 * 1024 * 1024) {
        this.photoError = 'Group photo is too large (max 25 MB before compression).';
        return;
      }
      this.clearPhoto();
      this.photoFile = file;
      this.photoPreviewUrl = URL.createObjectURL(file);
    },
    clearPhoto() {
      if (this.photoPreviewUrl) URL.revokeObjectURL(this.photoPreviewUrl);
      this.photoFile = null;
      this.photoPreviewUrl = null;
    },
    submit() {
      if (this.busy) return;
      this.$emit('create', {
        title: this.title,
        about: this.about,
        memberUsers: this.selected.slice(),
        photoFile: this.photoFile,
      });
    },
  },
  watch: {
    searchBusy() { if (this.step === 1) this.refocusSearch(); },
    searchResult() { if (this.step === 1) this.refocusSearch(); },
    searchError() { if (this.step === 1) this.refocusSearch(); },
  },
  beforeUnmount() {
    if (this.photoPreviewUrl) URL.revokeObjectURL(this.photoPreviewUrl);
  },
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-center justify-center z-40" @click.self="$emit('close')">
      <div class="bg-white rounded-xl shadow-xl w-full max-w-sm max-h-[90vh] p-4 flex flex-col">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold text-slate-800">
            {{ step === 1 ? 'Add members' : 'Group details' }}
          </h3>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <!-- ============ STEP 1: member picker ============ -->
        <template v-if="step === 1">
          <div v-if="selected.length" class="flex flex-wrap gap-1.5 mb-2">
            <span v-for="u in selected" :key="u.id"
                  class="inline-flex items-center gap-1 pl-2 pr-1 py-0.5 text-xs bg-teal-50 text-teal-800 rounded-full">
              {{ userLabel(u) }}
              <button @click="toggle(u)" class="text-teal-500 hover:text-teal-700 leading-none">&times;</button>
            </span>
          </div>

          <div class="flex gap-2 mb-1">
            <input ref="searchInput" :value="searchQuery" @input="onSearchInput" @keyup.enter="$emit('search')"
                   placeholder="username or +972501234567"
                   class="flex-1 px-2 py-1.5 text-sm border border-slate-300 rounded-lg" />
            <button @click="$emit('search')" :disabled="searchBusy || !searchQuery.trim()"
                    class="px-3 py-1.5 text-sm bg-teal-700 text-white rounded-lg disabled:opacity-50">
              {{ searchBusy ? '…' : 'Search' }}
            </button>
          </div>
          <p v-if="searchError" class="mb-1 text-xs text-red-600">{{ searchError }}</p>

          <div class="flex-1 min-h-0 overflow-y-auto -mx-1 px-1 mt-1">
            <button v-if="searchResult" @click="toggle(searchResult)"
                    class="w-full flex items-center gap-3 p-2 mb-1 rounded-lg border border-slate-200 hover:bg-slate-50 text-left">
              <Avatar :url="userAvatarUrl(searchResult)" :preview="userAvatarPreview(searchResult)"
                      :name="userLabel(searchResult)" :colorKey="String(searchResult.id)"
                      sizeClass="w-9 h-9 text-sm" :enlargeable="false" />
              <span class="flex-1 min-w-0 text-sm truncate">{{ userLabel(searchResult) }}</span>
              <span class="shrink-0 text-teal-600">{{ isSelected(searchResult) ? '✓' : '+' }}</span>
            </button>

            <p v-if="peerRows.length" class="text-[11px] uppercase tracking-wide text-slate-400 px-1 mt-2 mb-1">
              {{ searchQuery.trim() ? 'Matches from your chats' : 'Your chats' }}
            </p>
            <button v-for="u in peerRows" :key="u.id" @click="toggle(u)"
                    class="w-full flex items-center gap-3 p-2 rounded-lg hover:bg-slate-50 text-left">
              <Avatar :url="userAvatarUrl(u)" :preview="userAvatarPreview(u)"
                      :name="userLabel(u)" :colorKey="String(u.id)"
                      sizeClass="w-9 h-9 text-sm" :enlargeable="false" />
              <span class="flex-1 min-w-0 text-sm truncate">{{ userLabel(u) }}</span>
              <span class="shrink-0 text-teal-600">{{ isSelected(u) ? '✓' : '' }}</span>
            </button>
            <p v-if="!peerRows.length && !searchResult" class="p-3 text-sm text-slate-400">
              Search for someone by username or phone number.
            </p>
          </div>

          <div class="border-t border-slate-200 pt-3 mt-2 shrink-0">
            <button @click="step = 2"
                    class="w-full py-2 text-sm font-medium bg-teal-700 text-white rounded-lg">
              {{ selected.length }} selected · Continue
            </button>
          </div>
        </template>

        <!-- ============ STEP 2: group details ============ -->
        <template v-else>
          <div class="flex-1 min-h-0 overflow-y-auto -mx-1 px-1">
            <div class="flex flex-col items-center mb-4">
              <div class="relative">
                <img v-if="photoPreviewUrl" :src="photoPreviewUrl"
                     class="w-20 h-20 rounded-full object-cover border border-slate-200" />
                <div v-else class="w-20 h-20 rounded-full bg-slate-100 border border-slate-200 flex items-center justify-center text-2xl text-slate-400">＋</div>
                <label class="absolute -bottom-1 -right-1 w-7 h-7 rounded-full bg-teal-700 text-white flex items-center justify-center text-sm cursor-pointer">
                  ✎
                  <input type="file" accept="image/jpeg,image/png,image/webp" class="hidden" @change="onPhotoChange" />
                </label>
              </div>
              <button v-if="photoPreviewUrl" type="button" @click="clearPhoto"
                      class="mt-1 text-xs text-slate-400">Remove photo</button>
              <p v-else class="mt-1 text-xs text-slate-400">Group photo (optional)</p>
              <p v-if="photoError" class="mt-1 text-xs text-red-600">{{ photoError }}</p>
            </div>

            <label class="block text-xs font-medium text-slate-500 mb-1">Group name</label>
            <input v-model="title" placeholder="Group name" @keyup.enter="submit"
                   class="w-full mb-3 px-3 py-2 text-sm border border-slate-300 rounded-lg" />

            <label class="block text-xs font-medium text-slate-500 mb-1">Description (optional)</label>
            <textarea v-model="about" placeholder="What's this group about?" rows="2"
                      class="w-full mb-3 px-3 py-2 text-sm border border-slate-300 rounded-lg resize-none"></textarea>

            <p v-if="selected.length" class="text-xs text-slate-400 mb-2">
              {{ selected.length }} member{{ selected.length === 1 ? '' : 's' }}: {{ selected.map(userLabel).join(', ') }}
            </p>
          </div>

          <p v-if="error" class="text-xs text-red-600 mb-2 shrink-0">{{ error }}</p>

          <div class="border-t border-slate-200 pt-3 mt-2 shrink-0 flex gap-2">
            <button @click="step = 1" :disabled="busy"
                    class="px-4 py-2 text-sm border border-slate-300 rounded-lg disabled:opacity-50">Back</button>
            <button @click="submit" :disabled="busy"
                    class="flex-1 py-2 text-sm bg-teal-700 text-white rounded-lg disabled:opacity-50">
              {{ busy ? 'Creating…' : 'Create group' }}
            </button>
          </div>
        </template>
      </div>
    </div>
  `,
};
