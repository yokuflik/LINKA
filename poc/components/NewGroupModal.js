// New-group creation modal: name, description (about_text), an optional
// group photo (picked + previewed locally, uploaded after the chat is
// created), and a comma-separated list of member phone numbers. All the
// actual work (phone resolution, POST /chats/groups, avatar upload) still
// lives in the root - this only owns the form markup and emits 'create'
// with the collected values. Photo picker mirrors AuthScreen's sign-up
// avatar (same ✎ badge, "(optional)" hint, client-side limits).
const NewGroupModal = {
  props: {
    busy: { type: Boolean, default: false },
    error: { type: String, default: '' },
  },
  emits: ['close', 'create'],
  data() {
    return {
      title: '',
      about: '',
      memberPhones: '',
      photoFile: null,
      photoPreviewUrl: null,
      photoError: '',
    };
  },
  methods: {
    // Client-side limits mirror config.MAX_UPLOAD_BYTES_AVATAR (0.5 MB) and
    // the avatar MIME set, same as AuthScreen - instant feedback instead of
    // a 400 from the upload-ticket call.
    onPhotoChange(event) {
      const file = event.target.files && event.target.files[0];
      event.target.value = ''; // allow re-picking the same file
      if (!file) return;
      this.photoError = '';
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
        this.photoError = 'Use a JPEG, PNG or WebP image.';
        return;
      }
      if (file.size <= 0) { this.photoError = 'That file looks empty.'; return; }
      if (file.size > 512 * 1024) {
        this.photoError = 'Group photo must be 512 KB or smaller.';
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
        memberPhones: this.memberPhones,
        photoFile: this.photoFile,
      });
    },
  },
  beforeUnmount() {
    if (this.photoPreviewUrl) URL.revokeObjectURL(this.photoPreviewUrl);
  },
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-center justify-center z-40" @click.self="$emit('close')">
      <div class="bg-white rounded-xl shadow-xl w-80 max-h-[90vh] overflow-y-auto p-4">
        <div class="flex items-center justify-between mb-3">
          <h3 class="font-semibold text-slate-800">New group</h3>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

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

        <label class="block text-xs font-medium text-slate-500 mb-1">Members (phone numbers, comma-separated, optional)</label>
        <input v-model="memberPhones" placeholder="+972501234567, +972501234568"
               class="w-full mb-3 px-3 py-2 text-sm border border-slate-300 rounded-lg font-mono" />

        <p v-if="error" class="text-xs text-red-600 mb-2">{{ error }}</p>

        <button @click="submit" :disabled="busy"
                class="w-full py-2 text-sm bg-teal-700 text-white rounded-lg disabled:opacity-50">
          {{ busy ? 'Creating…' : 'Create group' }}
        </button>
      </div>
    </div>
  `,
};
