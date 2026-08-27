// Bottom bar: message text input + send button. Emits "typing" on every
// keystroke (not just on send) - the root throttles the actual WS send.
const MessageInput = {
  props: {
    messageInput: { type: String, required: true },
  },
  emits: ['update:messageInput', 'send-message', 'typing'],
  template: `
    <div class="p-3 border-t border-slate-200 bg-white flex gap-2">
      <input :value="messageInput"
             @input="$emit('update:messageInput', $event.target.value); $emit('typing')"
             @keyup.enter="$emit('send-message')" placeholder="Message…"
             class="flex-1 px-3 py-2 border border-slate-300 rounded-lg" />
      <button @click="$emit('send-message')" class="px-4 py-2 bg-teal-700 text-white rounded-lg font-medium">Send</button>
    </div>
  `,
};
