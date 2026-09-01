// Bottom bar: an optional reply-preview strip (WhatsApp-style, showing what
// you're replying to with a cancel button) - or an "Editing message" strip
// when editing an existing text message - above the row of [+] media button
// + message text input + send button. Emits "typing" on every keystroke (not
// just on send) - the root throttles the actual WS send. onInput is a real
// method (not a chained inline-statement handler) so both emits reliably fire
// on every keystroke, not just the first one.
//
// The [+] button opens a small WhatsApp-style attach menu: "Photos & Videos"
// (native picker filtered to image/video types -> pick-media) and "Documents"
// (unfiltered picker, any file type -> pick-document).
const MessageInput = {
  emits: ['update:messageInput', 'send-message', 'typing', 'cancel-reply', 'cancel-edit', 'pick-media', 'pick-document', 'start-recording', 'stop-recording', 'clear-attach-error'],
  props: {
    messageInput: { type: String, required: true },
    // error from the last attach attempt (e.g. file too large), shown as a
    // dismissible banner right above the composer
    attachError: { type: String, default: '' },
    replyingToMessage: { default: null },
    // the message currently being edited (composer prefilled), or null
    editingMessage: { default: null },
    senderLabel: { type: Function, required: true },
    // true while a mic recording is in progress (root owns MediaRecorder)
    isRecording: { type: Boolean, default: false },
    // seconds elapsed in the current recording, shown WhatsApp-style
    recordingSeconds: { type: Number, default: 0 },
    // live waveform bar heights (0..1) produced by useAudioWaveform.liveMeter
    liveWaveform: { type: Array, default: () => [] },
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
    // Opens the device camera directly (mobile) or a webcam capture dialog
    // (desktop) via the file input's `capture` hint, then reuses the normal
    // image pipeline.
    openCamera() {
      this.closeAttachMenu();
      this.$refs.cameraInput.value = '';
      this.$refs.cameraInput.click();
    },
    onCameraPhotoChosen(event) {
      const file = event.target.files && event.target.files[0];
      if (file) this.$emit('pick-media', file);
    },
    openDocumentPicker() {
      this.closeAttachMenu();
      this.$refs.documentFileInput.value = '';
      this.$refs.documentFileInput.click();
    },
    onDocumentFileChosen(event) {
      const file = event.target.files && event.target.files[0];
      if (file) this.$emit('pick-document', file);
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
      <div v-if="attachError"
           class="mx-3 mt-2 flex items-center gap-2 rounded-lg bg-red-50 border border-red-200 px-3 py-2 text-sm text-red-700">
        <span class="text-base leading-none">⚠️</span>
        <span class="flex-1">{{ attachError }}</span>
        <button @click="$emit('clear-attach-error')"
                class="text-red-400 hover:text-red-600 text-lg leading-none px-1">&times;</button>
      </div>
      <div v-if="editingMessage" class="px-3 pt-2 flex items-start gap-2">
        <div class="flex-1 min-w-0 pl-2 border-l-4 border-amber-500 text-xs">
          <div class="font-semibold text-amber-600">Editing message</div>
          <div class="truncate text-slate-500">{{ editingMessage.content }}</div>
        </div>
        <button @click="$emit('cancel-edit')" class="text-slate-400 hover:text-slate-600 text-lg leading-none px-1">&times;</button>
      </div>
      <div v-else-if="replyingToMessage" class="px-3 pt-2 flex items-start gap-2">
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
            <button type="button" @click="openDocumentPicker"
                    class="w-full text-left px-3 py-2 hover:bg-slate-50 flex items-center gap-2">
              <span>📄</span><span>Documents</span>
            </button>
          </div>
        </div>
        <template v-if="isRecording">
          <div class="flex-1 min-w-0 flex items-center gap-2 px-3 py-2 text-sm text-red-600">
            <span class="w-2.5 h-2.5 rounded-full bg-red-600 animate-pulse shrink-0"></span>
            <!-- Live waveform: newest sample scrolls in from the right -->
            <div class="flex-1 min-w-0 flex items-center gap-[2px] h-8 overflow-hidden">
              <span v-for="(bar, i) in liveWaveform" :key="i"
                    class="flex-1 rounded-full bg-red-400/80"
                    :style="{ height: Math.max(8, bar * 100) + '%' }"></span>
            </div>
            <span class="tabular-nums text-slate-500 shrink-0">{{ recordingClock }}</span>
          </div>
        </template>
        <template v-else>
          <input :value="messageInput" @input="onInput" @focus="closeAttachMenu"
                 @keyup.enter="$emit('send-message')" placeholder="Message…"
                 class="flex-1 min-w-0 px-3 py-2 border border-slate-300 rounded-lg" />
          <button v-if="messageInput.trim()" @click="$emit('send-message')"
                  class="shrink-0 px-4 py-2 bg-teal-700 text-white rounded-lg font-medium">Send</button>
        </template>
        <!-- Camera: plain black line-art icon (matches the mic), opens the
             device camera and sends the photo through the image pipeline. -->
        <button type="button" @click="openCamera" v-if="!isRecording"
                class="shrink-0 w-9 h-9 flex items-center justify-center text-slate-700 hover:text-slate-900"
                title="Take a photo">
          <svg viewBox="0 0 24 24" class="w-6 h-6" fill="none"
               stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 8h3l1.5-2h7L18 8h2a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z" />
            <circle cx="12" cy="13" r="3.2" />
          </svg>
        </button>
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
        <input ref="documentFileInput" type="file" class="hidden"
               @change="onDocumentFileChosen" />
        <!-- Not display:none: iOS Safari won't open the camera for a
             display:none file input triggered via .click(). Kept off-screen. -->
        <input ref="cameraInput" type="file"
               accept="image/*" capture="environment"
               @change="onCameraPhotoChosen"
               style="position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;left:-9999px" />
      </div>
    </div>
  `,
};
