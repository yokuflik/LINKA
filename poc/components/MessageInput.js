// Bottom bar: an optional reply-preview strip (WhatsApp-style, showing what
// you're replying to with a cancel button) above the message text input +
// send button. Emits "typing" on every keystroke (not just on send) - the
// root throttles the actual WS send. onInput is a real method (not a
// chained inline-statement handler) so both emits reliably fire on every
// keystroke, not just the first one.
const MessageInput = {
  props: {
    messageInput: { type: String, required: true },
    replyingToMessage: { default: null }, // the message object being replied to, or null
    senderLabel: { type: Function, required: true },
  },
  emits: ['update:messageInput', 'send-message', 'typing', 'cancel-reply'],
  methods: {
    onInput(event) {
      this.$emit('update:messageInput', event.target.value);
      this.$emit('typing');
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
      <div class="p-3 flex gap-2">
        <input :value="messageInput" @input="onInput"
               @keyup.enter="$emit('send-message')" placeholder="Message…"
               class="flex-1 px-3 py-2 border border-slate-300 rounded-lg" />
        <button @click="$emit('send-message')" class="px-4 py-2 bg-teal-700 text-white rounded-lg font-medium">Send</button>
      </div>
    </div>
  `,
};
