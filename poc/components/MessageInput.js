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
  props: {
    messageInput: { type: String, required: true },
    replyingToMessage: { default: null }, // the message object being replied to, or null
    senderLabel: { type: Function, required: true },
  },
  emits: ['update:messageInput', 'send-message', 'typing', 'cancel-reply', 'pick-media'],
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
        <input :value="messageInput" @input="onInput" @focus="closeAttachMenu"
               @keyup.enter="$emit('send-message')" placeholder="Message…"
               class="flex-1 min-w-0 px-3 py-2 border border-slate-300 rounded-lg" />
        <button @click="$emit('send-message')" class="shrink-0 px-4 py-2 bg-teal-700 text-white rounded-lg font-medium">Send</button>
        <input ref="mediaFileInput" type="file" class="hidden"
               accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/webm,video/quicktime"
               @change="onMediaFileChosen" />
      </div>
    </div>
  `,
};
