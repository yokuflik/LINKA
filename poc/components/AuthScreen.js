// Auth screen (ADR 0017): one flow only - phone -> OTP. There is no
// Sign up / Log in toggle: any verified phone is find-or-create on the
// backend. Brand-new accounts land on a third "welcome" step that collects
// an about line / avatar and lets the user keep or replace the
// server-assigned username. All auth logic lives in the root app / useAuth.
const AuthScreen = {
  props: {
    authStage: { type: String, required: true },     // 'phone' | 'otp' | 'welcome'
    otpCode: { type: String, required: true },
    profileDraft: { type: Object, required: true },   // { about_text, username }
    usernameCheck: { type: Object, default: () => ({ status: '', reason: '' }) },
    // Phone input (usePhoneInput / ADR 0009)
    phoneRawInput: { type: String, default: '' },
    phoneCountry: { type: Object, required: true },   // { iso2, name, dial, flag }
    phoneCountries: { type: Array, default: () => [] },
    phoneIsValid: { type: Boolean, default: false },
    phoneIsWhitelisted: { type: Boolean, default: false },
    phoneE164: { type: String, default: '' },
    avatarPreviewUrl: { default: null },              // object URL for the picked file
    avatarError: { type: String, default: '' },
    authError: { type: String, required: true },
    authBusy: { type: Boolean, required: true },
    // Non-empty while apiFetch is retrying an auth call after a network
    // failure / rate-limit: a live "retrying in Ns…" line.
    connMessage: { type: String, default: '' },
  },
  emits: [
    'update:otpCode', 'update:profileDraft',
    'update:phoneRawInput', 'update:phoneCountry',
    'pick-avatar', 'clear-avatar',
    'request-otp', 'verify-otp', 'back-to-phone',
    'check-username', 'submit-welcome', 'skip-welcome',
  ],
  data() {
    return {
      resendIn: 0,        // seconds left before "resend code" is allowed again
      _resendTimer: null,
      countryOpen: false,  // custom country dropdown expanded?
      countryFilter: '',
    };
  },
  computed: {
    canSubmitPhone() { return this.phoneIsValid || this.phoneIsWhitelisted; },
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
      return { text: '', cls: 'text-slate-400' };
    },
    filteredCountries() {
      const q = this.countryFilter.trim().toLowerCase();
      if (!q) return this.phoneCountries;
      return this.phoneCountries.filter(
        (c) => c.name.toLowerCase().includes(q) || c.dial.includes(q.replace(/^\+/, ''))
      );
    },
  },
  watch: {
    // Start the 60s cooldown whenever we land on the code-entry step.
    authStage(stage) {
      if (stage === 'otp') this.startResendCooldown();
      else this.clearResendTimer();
    },
  },
  mounted() { document.addEventListener('click', this.onDocClick, true); },
  beforeUnmount() {
    this.clearResendTimer();
    document.removeEventListener('click', this.onDocClick, true);
  },
  methods: {
    startResendCooldown() {
      this.clearResendTimer();
      this.resendIn = 60;
      this._resendTimer = setInterval(() => {
        this.resendIn -= 1;
        if (this.resendIn <= 0) this.clearResendTimer();
      }, 1000);
    },
    clearResendTimer() {
      if (this._resendTimer) clearInterval(this._resendTimer);
      this._resendTimer = null;
    },
    resendCode() {
      if (this.resendIn > 0 || this.authBusy) return;
      this.$emit('request-otp');
      this.startResendCooldown();
    },
    setProfileField(key, value) {
      this.$emit('update:profileDraft', { ...this.profileDraft, [key]: value });
      if (key === 'username') this.$emit('check-username');
    },
    toggleCountry() {
      this.countryOpen = !this.countryOpen;
      this.countryFilter = '';
      if (this.countryOpen) this.$nextTick(() => this.$refs.countrySearch && this.$refs.countrySearch.focus());
    },
    pickCountry(c) {
      this.$emit('update:phoneCountry', c);
      this.countryOpen = false;
    },
    onDocClick(e) {
      if (this.countryOpen && this.$el && !this.$el.contains(e.target)) this.countryOpen = false;
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
        <div class="flex flex-col items-center text-center mb-5">
          <img src="assets/maskable_icon_x192.png" alt="Linka"
               class="w-16 h-16 sm:w-20 sm:h-20 rounded-2xl shadow-sm select-none" draggable="false" />
          <h1 class="mt-3 text-xl font-semibold">Linka</h1>
        </div>
        <p class="text-sm text-slate-500 mb-5 text-center">
          {{ authStage === 'welcome' ? 'Set up your profile' : 'Sign in with your phone' }}
        </p>

        <template v-if="authStage === 'phone'">
          <label class="block text-xs font-medium text-slate-500 mb-1">Phone number</label>
          <div class="relative mb-1">
            <div class="flex w-full border border-slate-300 rounded-lg overflow-hidden focus-within:ring-1 focus-within:ring-teal-600 focus-within:border-teal-600">
              <button type="button" @click="toggleCountry" :title="phoneCountry.name"
                      class="shrink-0 flex items-center gap-1 px-2 py-2 bg-slate-50 border-r border-slate-300 text-sm">
                <span>{{ phoneCountry.flag }}</span>
                <span class="text-slate-500">+{{ phoneCountry.dial }}</span>
                <span class="text-slate-400 text-[10px]">▾</span>
              </button>
              <input :value="phoneRawInput" @input="$emit('update:phoneRawInput', $event.target.value)"
                     inputmode="tel" placeholder="Phone number"
                     class="flex-1 min-w-0 px-3 py-2 outline-none"
                     @keyup.enter="canSubmitPhone && $emit('request-otp')" />
            </div>

            <div v-if="countryOpen"
                 class="absolute z-20 mt-1 w-full max-h-64 overflow-y-auto bg-white border border-slate-200 rounded-lg shadow-lg">
              <div class="sticky top-0 bg-white p-2 border-b border-slate-100">
                <input ref="countrySearch" v-model="countryFilter" placeholder="Search country…"
                       class="w-full px-2 py-1 text-sm border border-slate-200 rounded" />
              </div>
              <button v-for="c in filteredCountries" :key="c.iso2" type="button"
                      @click="pickCountry(c)"
                      class="w-full flex items-center gap-2 px-3 py-2 text-sm text-left hover:bg-slate-50"
                      :class="c.iso2 === phoneCountry.iso2 ? 'bg-teal-50' : ''">
                <span>{{ c.flag }}</span>
                <span class="flex-1 truncate">{{ c.name }}</span>
                <span class="text-slate-400">+{{ c.dial }}</span>
              </button>
              <p v-if="!filteredCountries.length" class="px-3 py-2 text-sm text-slate-400">No match</p>
            </div>
          </div>
          <p class="mb-3 text-xs h-4"
             :class="phoneRawInput && !canSubmitPhone ? 'text-red-600' : 'text-slate-400'">
            <template v-if="phoneRawInput && !canSubmitPhone">Enter a valid phone number</template>
            <template v-else-if="phoneIsWhitelisted">Dev test number — verification skipped.</template>
          </p>

          <button @click="$emit('request-otp')" :disabled="authBusy || !canSubmitPhone"
                  class="w-full py-2 bg-teal-700 text-white rounded-lg font-medium disabled:opacity-50">
            {{ authBusy ? 'Sending…' : 'Send code' }}
          </button>
          <div id="recaptcha-container"></div>
        </template>

        <template v-else-if="authStage === 'otp'">
          <p v-if="phoneIsWhitelisted" class="text-xs text-slate-500 mb-3">
            Dev test number — verification is skipped, enter any code.
          </p>
          <p v-else class="text-xs text-slate-500 mb-3">
            We sent a 6-digit code by SMS to <span class="font-mono">{{ phoneE164 }}</span>.
          </p>
          <label class="block text-xs font-medium text-slate-500 mb-1">OTP code</label>
          <input :value="otpCode" @input="$emit('update:otpCode', $event.target.value)"
                 placeholder="000000"
                 class="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg font-mono tracking-widest"
                 @keyup.enter="$emit('verify-otp')" />
          <button @click="$emit('verify-otp')" :disabled="authBusy || !otpCode"
                  class="w-full py-2 bg-teal-700 text-white rounded-lg font-medium disabled:opacity-50">
            {{ authBusy ? 'Verifying…' : 'Verify & continue' }}
          </button>
          <button v-if="!phoneIsWhitelisted" type="button"
                  @click="resendCode" :disabled="resendIn > 0 || authBusy"
                  class="w-full mt-2 py-1 text-sm text-teal-700 disabled:text-slate-400">
            {{ resendIn > 0 ? 'Resend code in ' + resendIn + 's' : "Didn't get a code? Resend" }}
          </button>
          <button @click="$emit('back-to-phone')" class="w-full mt-1 py-1 text-sm text-slate-500">← back</button>
        </template>

        <template v-else>
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
            <InlineAlert :message="avatarError" class="mt-1" />
          </div>

          <label class="block text-xs font-medium text-slate-500 mb-1">Username</label>
          <input :value="profileDraft.username"
                 @input="setProfileField('username', $event.target.value.toLowerCase())"
                 placeholder="jane_doe"
                 class="w-full px-3 py-2 border border-slate-300 rounded-lg font-mono" />
          <p class="mb-3 mt-1 text-xs h-4" :class="usernameHint.cls">
            {{ usernameHint.text || 'You can change this any time.' }}
          </p>

          <label class="block text-xs font-medium text-slate-500 mb-1">About</label>
          <textarea :value="profileDraft.about_text"
                    @input="setProfileField('about_text', $event.target.value)"
                    rows="2" placeholder="Hey there! I am using Linka."
                    class="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg text-sm"></textarea>

          <button @click="$emit('submit-welcome')" :disabled="authBusy || usernameCheck.status === 'bad'"
                  class="w-full py-2 bg-teal-700 text-white rounded-lg font-medium disabled:opacity-50">
            {{ authBusy ? 'Saving…' : 'Continue' }}
          </button>
          <button @click="$emit('skip-welcome')" :disabled="authBusy"
                  class="w-full mt-2 py-1 text-sm text-slate-500">Skip for now</button>
        </template>

        <InlineAlert :message="authError" class="mt-3" />
        <InlineAlert :message="connMessage" variant="warning" class="mt-3" />
      </div>
    </div>
  `,
};
