// Bottom bar: an optional reply-preview strip (WhatsApp-style, showing what
// you're replying to with a cancel button) above the row of [+] media button
// + message text input + send button. Emits "typing" on every keystroke (not
// just on send) - the root throttles the actual WS send. onInput is a real
// method (not a chained inline-statement handler) so both emits reliably fire
// on every keystroke, not just the first one.
//
// The [+] button opens a small WhatsApp-style attach menu. Only "Photos &
// Videos" is active for now (opens a native file picker filtered to image/
// video types); "Documents" is rendered disabled - wired later.
const MessageInput = {
  emits: ['update:messageInput', 'send-message', 'typing', 'cancel-reply', 'pick-media', 'start-recording', 'stop-recording'],
  props: {
    messageInput: { type: String, required: true },
    replyingToMessage: { default: null },
    senderLabel: { type: Function, required: true },
    // true while a mic recording is in progress (root owns MediaRecorder)
    isRecording: { type: Boolean, default: false },
    // seconds elapsed in the current recording, shown WhatsApp-style
    recordingSeconds: { type: Number, default: 0 },
  },
  data() {
    return { attachMenuOpen: false };
  },
  methods: {
    onInput(event) {
      this.$emit('update:messageInput', event.target.value);
      this.$emit('typing');
    },
    toggleAttachMenu() {
      this.attachMenuOpen = !this.attachMenuOpen;
    },
    closeAttachMenu() {
      this.attachMenuOpen = false;
    },
    openImagePicker() {
      this.closeAttachMenu();
      this.$refs.mediaFileInput.value = '';
      this.$refs.mediaFileInput.click();
    },
    onMediaFileChosen(event) {
      const file = event.target.files && event.target.files[0];
      if (file) this.$emit('pick-media', file);
    },
    // Single toggle button: first press starts recording, second press stops
    // and sends (root owns the MediaRecorder + upload).
    toggleRecording() {
      this.closeAttachMenu();
      if (this.isRecording) this.$emit('stop-recording');
      else this.$emit('start-recording');
    },
  },
  computed: {
    recordingClock() {
      const s = Math.max(0, Math.floor(this.recordingSeconds));
      return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    },
  },
  template: `
    <div class="border-t border-slate-200 bg-white">
      <div v-if="replyingToMessage" class="px-3 pt-2 flex items-start gap-2">
        <div class="flex-1 min-w-0 pl-2 border-l-4 border-teal-600 text-xs">
          <div class="font-semibold text-teal-700">{{ senderLabel(replyingToMessage.sender_id) }}</div>
          <div class="truncate text-slate-500">{{ replyingToMessage.content }}</div>
        </div>
        <button @click="$emit('cancel-reply')" class="text-slate-400 hover:text-slate-600 text-lg leading-none px-1">&times;</button>
      </div>
      <div class="p-3 flex items-center gap-2">
        <div class="relative shrink-0">
          <button type="button" @click="toggleAttachMenu"
                  class="w-9 h-9 rounded-full bg-slate-100 hover:bg-slate-200 text-slate-600 text-xl leading-none flex items-center justify-center"
                  :class="attachMenuOpen ? 'bg-slate-200' : ''"
                  title="Attach">+</button>
          <div v-if="attachMenuOpen"
               class="absolute bottom-11 left-0 z-20 w-52 bg-white border border-slate-200 rounded-lg shadow-lg py-1 text-sm">
            <button type="button" @click="openImagePicker"
                    class="w-full text-left px-3 py-2 hover:bg-slate-50 flex items-center gap-2">
              <span>🖼️</span><span>Photos &amp; Videos</span>
            </button>
            <button type="button" disabled
                    class="w-full text-left px-3 py-2 flex items-center gap-2 text-slate-400 cursor-not-allowed">
              <span>📄</span><span>Documents</span><span class="ml-auto text-xs">🔒</span>
            </button>
          </div>
        </div>
        <template v-if="isRecording">
          <div class="flex-1 min-w-0 flex items-center gap-2 px-3 py-2 text-sm text-red-600">
            <span class="w-2.5 h-2.5 rounded-full bg-red-600 animate-pulse"></span>
            <span>Recording…</span>
            <span class="ml-auto tabular-nums text-slate-500">{{ recordingClock }}</span>
          </div>
        </template>
        <template v-else>
          <input :value="messageInput" @input="onInput" @focus="closeAttachMenu"
                 @keyup.enter="$emit('send-message')" placeholder="Message…"
                 class="flex-1 min-w-0 px-3 py-2 border border-slate-300 rounded-lg" />
          <button v-if="messageInput.trim()" @click="$emit('send-message')"
                  class="shrink-0 px-4 py-2 bg-teal-700 text-white rounded-lg font-medium">Send</button>
        </template>
        <!-- Mic toggle: 1st press records, 2nd press stops & sends. Plain
             black line-art mic (WhatsApp-style), no button background. -->
        <button type="button" @click="toggleRecording"
                class="shrink-0 w-9 h-9 flex items-center justify-center text-slate-700 hover:text-slate-900"
                :title="isRecording ? 'Stop & send' : 'Record voice message'">
          <svg v-if="!isRecording" viewBox="0 0 24 24" class="w-6 h-6" fill="none"
               stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="2" width="6" height="12" rx="3" />
            <path d="M5 11a7 7 0 0 0 14 0" />
            <line x1="12" y1="18" x2="12" y2="22" />
            <line x1="8" y1="22" x2="16" y2="22" />
          </svg>
          <svg v-else viewBox="0 0 24 24" class="w-6 h-6" fill="currentColor">
            <rect x="6" y="6" width="12" height="12" rx="2" />
          </svg>
        </button>
        <input ref="mediaFileInput" type="file" class="hidden"
               accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/webm,video/quicktime"
               @change="onMediaFileChosen" />
      </div>
    </div>
  `,
};
