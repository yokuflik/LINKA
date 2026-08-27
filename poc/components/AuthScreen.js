// Auth screen: Sign up is the default view; a small "Log in" link at the
// bottom switches to the login form (and back). Each is a phone + OTP
// two-stage flow. Sign up additionally collects the profile fields
// (everything on the user record except the id) plus an optional profile
// photo; after a successful OTP verify the root app PATCHes the text fields
// and runs the avatar upload flow. All auth logic lives in the root app.
const AuthScreen = {
  props: {
    authMode: { type: String, required: true },     // 'login' | 'register'
    authStage: { type: String, required: true },     // 'phone' | 'otp'
    phoneNumber: { type: String, required: true },
    otpCode: { type: String, required: true },
    profileDraft: { type: Object, required: true },   // { display_name, about_text }
    avatarPreviewUrl: { default: null },              // object URL for the picked file
    avatarError: { type: String, default: '' },
    authError: { type: String, required: true },
    authBusy: { type: Boolean, required: true },
  },
  emits: [
    'update:authMode', 'update:phoneNumber', 'update:otpCode', 'update:profileDraft',
    'pick-avatar', 'clear-avatar',
    'request-otp', 'verify-otp', 'back-to-phone',
  ],
  computed: {
    isRegister() { return this.authMode === 'register'; },
  },
  methods: {
    setProfileField(key, value) {
      this.$emit('update:profileDraft', { ...this.profileDraft, [key]: value });
    },
    onAvatarChange(event) {
      const file = event.target.files && event.target.files[0];
      if (file) this.$emit('pick-avatar', file);
      event.target.value = ''; // allow re-picking the same file
    },
  },
  template: `
    <div class="h-full flex items-center justify-center p-4">
      <div class="w-full max-w-sm bg-white rounded-xl shadow-sm border border-slate-200 p-6">
        <h1 class="text-xl font-semibold mb-1">Linka</h1>
        <p class="text-sm text-slate-500 mb-5">{{ isRegister ? 'Create your account' : 'Welcome back' }}</p>

        <template v-if="authStage === 'phone'">
          <template v-if="isRegister">
            <div class="flex flex-col items-center mb-4">
              <div class="relative">
                <img v-if="avatarPreviewUrl" :src="avatarPreviewUrl"
                     class="w-20 h-20 rounded-full object-cover border border-slate-200" />
                <div v-else class="w-20 h-20 rounded-full bg-slate-100 border border-slate-200 flex items-center justify-center text-2xl text-slate-400">＋</div>
                <label class="absolute -bottom-1 -right-1 w-7 h-7 rounded-full bg-teal-700 text-white flex items-center justify-center text-sm cursor-pointer">
                  ✎
                  <input type="file" accept="image/jpeg,image/png,image/webp" class="hidden" @change="onAvatarChange" />
                </label>
              </div>
              <button v-if="avatarPreviewUrl" type="button" @click="$emit('clear-avatar')"
                      class="mt-1 text-xs text-slate-400">Remove photo</button>
              <p v-else class="mt-1 text-xs text-slate-400">Profile photo (optional)</p>
              <p v-if="avatarError" class="mt-1 text-xs text-red-600">{{ avatarError }}</p>
            </div>
          </template>

          <label class="block text-xs font-medium text-slate-500 mb-1">Phone number</label>
          <input :value="phoneNumber" @input="$emit('update:phoneNumber', $event.target.value)"
                 placeholder="+972---------"
                 class="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg"
                 @keyup.enter="$emit('request-otp')" />

          <template v-if="isRegister">
            <label class="block text-xs font-medium text-slate-500 mb-1">Display name</label>
            <input :value="profileDraft.display_name"
                   @input="setProfileField('display_name', $event.target.value)"
                   placeholder="Jane Doe"
                   class="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg" />

            <label class="block text-xs font-medium text-slate-500 mb-1">About</label>
            <textarea :value="profileDraft.about_text"
                      @input="setProfileField('about_text', $event.target.value)"
                      rows="2" placeholder="Hey there! I am using Linka."
                      class="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg text-sm"></textarea>
          </template>

          <button @click="$emit('request-otp')" :disabled="authBusy || !phoneNumber"
                  class="w-full py-2 bg-teal-700 text-white rounded-lg font-medium disabled:opacity-50">
            {{ authBusy ? 'Sending…' : 'Send code' }}
          </button>
        </template>

        <template v-else>
          <p class="text-xs text-slate-500 mb-3">No SMS provider is wired up yet — check the <span class="font-mono">server</span> console/log for the code.</p>
          <label class="block text-xs font-medium text-slate-500 mb-1">OTP code</label>
          <input :value="otpCode" @input="$emit('update:otpCode', $event.target.value)"
                 placeholder="000000"
                 class="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg font-mono tracking-widest"
                 @keyup.enter="$emit('verify-otp')" />
          <button @click="$emit('verify-otp')" :disabled="authBusy || !otpCode"
                  class="w-full py-2 bg-teal-700 text-white rounded-lg font-medium disabled:opacity-50">
            {{ authBusy ? 'Verifying…' : (isRegister ? 'Verify & create account' : 'Verify & log in') }}
          </button>
          <button @click="$emit('back-to-phone')" class="w-full mt-2 py-1 text-sm text-slate-500">← back</button>
        </template>

        <p v-if="authError" class="mt-3 text-sm text-red-600">{{ authError }}</p>

        <div class="mt-5 pt-4 border-t border-slate-100 text-center text-sm text-slate-500">
          <template v-if="isRegister">
            Already have an account?
            <button @click="$emit('update:authMode', 'login')" class="font-medium text-teal-700">Log in</button>
          </template>
          <template v-else>
            New here?
            <button @click="$emit('update:authMode', 'register')" class="font-medium text-teal-700">Sign up</button>
          </template>
        </div>
      </div>
    </div>
  `,
};
