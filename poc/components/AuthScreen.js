// Login screen: API base config + phone/OTP two-stage auth form.
// Pure presentational extraction from the original single-file template -
// all auth logic (requestOtp/verifyOtp/etc.) still lives in the root app.
const AuthScreen = {
  props: {
    apiBase: { type: String, required: true },
    authStage: { type: String, required: true },
    phoneNumber: { type: String, required: true },
    otpCode: { type: String, required: true },
    authError: { type: String, required: true },
    authBusy: { type: Boolean, required: true },
  },
  emits: [
    'update:apiBase', 'update:phoneNumber', 'update:otpCode',
    'request-otp', 'verify-otp', 'back-to-phone',
  ],
  template: `
    <div class="h-full flex items-center justify-center p-4">
      <div class="w-full max-w-sm bg-white rounded-xl shadow-sm border border-slate-200 p-6">
        <h1 class="text-xl font-semibold mb-1">Linka</h1>
        <p class="text-sm text-slate-500 mb-4">WebSocket PoC client</p>

        <label class="block text-xs font-medium text-slate-500 mb-1">API server</label>
        <input :value="apiBase" @input="$emit('update:apiBase', $event.target.value)"
               class="w-full mb-4 px-3 py-2 text-sm border border-slate-300 rounded-lg font-mono" />

        <template v-if="authStage === 'phone'">
          <label class="block text-xs font-medium text-slate-500 mb-1">Phone number</label>
          <input :value="phoneNumber" @input="$emit('update:phoneNumber', $event.target.value)"
                 placeholder="+972---------"
                 class="w-full mb-3 px-3 py-2 border border-slate-300 rounded-lg"
                 @keyup.enter="$emit('request-otp')" />
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
            {{ authBusy ? 'Verifying…' : 'Verify & log in' }}
          </button>
          <button @click="$emit('back-to-phone')" class="w-full mt-2 py-1 text-sm text-slate-500">← back</button>
        </template>

        <p v-if="authError" class="mt-3 text-sm text-red-600">{{ authError }}</p>
      </div>
    </div>
  `,
};
