// Settings body of the AI agent drawer (AGENT_DRAWER_UI_PLAN.md Step 8).
// Two scrollable sections, matching the strong visual separation the ADR
// mandates: system_prompt is a SOFT constraint (free text, never enforced by
// the server, Save/Cancel); restrictions are HARD (server-enforced,
// checkboxes commit immediately, text fields get Save/Cancel like the prompt).
const AgentSettingsView = {
  props: {
    form: { type: Object, required: true }, // agentForm - never null while this view is shown
    busy: { type: Boolean, required: true },
    error: { type: String, default: '' },
    promptDirty: { type: Boolean, required: true },
    hardTextDirty: { type: Boolean, required: true },
    chatKeywordsDirty: { type: Object, required: true }, // chat_id -> bool
    // BYOK (ADR 0046 decision 6) - write-only key input, server never echoes
    // the stored key back (form.has_custom_key is the only signal).
    byokDirty: { type: Boolean, required: true },
    byokKeyInput: { type: String, required: true },
    // Knowledge base (ADR 0046 decision 4) - own upload/delete calls, not
    // part of the diff-then-PATCH agent config.
    knowledgeDocuments: { type: Array, default: () => [] },
    knowledgeUploadBusy: { type: Boolean, default: false },
    knowledgeError: { type: String, default: '' },
  },
  emits: [
    'prompt-input', 'save-prompt', 'cancel-prompt',
    'set-restriction', 'max-messages-input', 'save-hard-text', 'cancel-hard-text',
    'add-chat-trigger', 'remove-chat-trigger', 'set-time-window',
    'chat-keywords-input', 'save-chat-keywords', 'cancel-chat-keywords',
    'upload-knowledge-file', 'delete-knowledge-document',
    'byok-key-input', 'save-byok-key', 'cancel-byok-key', 'clear-byok-key',
  ],
  data() {
    return { newTriggerChatId: '' };
  },
  methods: {
    submitNewTrigger() {
      const chatId = this.newTriggerChatId.trim();
      if (!chatId) return;
      this.$emit('add-chat-trigger', chatId);
      this.newTriggerChatId = '';
    },
    onKnowledgeFilePicked(event) {
      const file = event.target.files && event.target.files[0];
      event.target.value = '';
      if (file) this.$emit('upload-knowledge-file', file);
    },
  },
  template: `
    <div class="h-full overflow-y-auto p-3 space-y-4">
      <!-- SOFT section: free text, not enforced -->
      <div class="rounded-lg border-2 border-dashed border-sky-300 bg-sky-50/50 p-3">
        <div class="flex items-center gap-2 mb-1">
          <span class="text-xs font-semibold uppercase tracking-wide text-sky-700">Soft guidance</span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-sky-100 text-sky-700 border border-sky-300">not enforced</span>
        </div>
        <p class="text-xs text-slate-500 mb-2">
          Free-form instructions for the agent's behavior. This is a suggestion to the model,
          not a security boundary — it can be ignored or misinterpreted. Use the restrictions
          below for anything that must actually be blocked.
        </p>
        <textarea :value="form.system_prompt" @input="$emit('prompt-input', $event.target.value)"
                  rows="3" placeholder="e.g. Be friendly and brief. Don't discuss politics."
                  class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg"></textarea>
        <div v-if="promptDirty" class="flex gap-2 mt-2">
          <button @click="$emit('save-prompt')" :disabled="busy"
                  class="px-2.5 py-1 text-xs font-medium bg-teal-700 text-white rounded-lg disabled:opacity-50">Save</button>
          <button @click="$emit('cancel-prompt')" :disabled="busy"
                  class="px-2.5 py-1 text-xs border border-slate-300 rounded-lg">Cancel</button>
        </div>
      </div>

      <!-- HARD section: server-enforced -->
      <div class="rounded-lg border-2 border-solid border-rose-300 bg-rose-50/40 p-3">
        <div class="flex items-center gap-2 mb-1">
          <span class="text-xs font-semibold uppercase tracking-wide text-rose-700">Restrictions</span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-rose-100 text-rose-700 border border-rose-300">enforced by the server</span>
        </div>
        <p class="text-xs text-slate-500 mb-2">
          Hard limits checked before every action the agent takes. Checkboxes save instantly;
          text fields need Save. Cannot be bypassed by the soft guidance above or by the agent itself.
        </p>

        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can send messages</span>
          <input type="checkbox" :checked="form.restrictions.can_send_messages"
                 @change="$emit('set-restriction', 'can_send_messages', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can message groups</span>
          <input type="checkbox" :checked="form.restrictions.can_message_groups"
                 @change="$emit('set-restriction', 'can_message_groups', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can message private chats</span>
          <input type="checkbox" :checked="form.restrictions.can_message_private"
                 @change="$emit('set-restriction', 'can_message_private', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can start new private chats</span>
          <input type="checkbox" :checked="form.restrictions.can_message_new_private_contacts"
                 @change="$emit('set-restriction', 'can_message_new_private_contacts', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can leave groups</span>
          <input type="checkbox" :checked="form.restrictions.can_leave_groups"
                 @change="$emit('set-restriction', 'can_leave_groups', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Max messages / day</span>
          <input type="number" min="0" placeholder="No limit"
                 :value="form.restrictions.max_messages_per_day"
                 @input="$emit('max-messages-input', $event.target.value === '' ? null : Number($event.target.value))"
                 class="w-24 px-2 py-1 text-sm border border-slate-300 rounded-lg text-right" />
        </label>
        <div v-if="hardTextDirty" class="flex gap-2 mt-2">
          <button @click="$emit('save-hard-text')" :disabled="busy"
                  class="px-2.5 py-1 text-xs font-medium bg-teal-700 text-white rounded-lg disabled:opacity-50">Save</button>
          <button @click="$emit('cancel-hard-text')" :disabled="busy"
                  class="px-2.5 py-1 text-xs border border-slate-300 rounded-lg">Cancel</button>
        </div>
      </div>

      <!-- Triggers ("Gatekeeper") - checkboxes/add/remove commit immediately -->
      <div class="rounded-lg border border-slate-200 p-3">
        <span class="text-xs font-semibold uppercase tracking-wide text-slate-600">When the agent wakes up</span>

        <div class="mt-2">
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" :checked="form.triggers.on_time_window.enabled"
                   @change="$emit('set-time-window', 'enabled', $event.target.checked)" />
            Only during a daily time window
          </label>
          <div v-if="form.triggers.on_time_window.enabled" class="flex items-center gap-2 mt-1 ml-6">
            <input type="time" :value="form.triggers.on_time_window.start"
                   @change="$emit('set-time-window', 'start', $event.target.value)"
                   class="px-2 py-1 text-sm border border-slate-300 rounded-lg" />
            <span class="text-xs text-slate-400">to</span>
            <input type="time" :value="form.triggers.on_time_window.end"
                   @change="$emit('set-time-window', 'end', $event.target.value)"
                   class="px-2 py-1 text-sm border border-slate-300 rounded-lg" />
          </div>
        </div>

        <div class="mt-3">
          <p class="text-xs text-slate-500 mb-1">
            Specific chats to watch (leave keywords empty to wake on any message):
          </p>
          <div v-for="(cfg, chatId) in form.triggers.on_specific_chats" :key="chatId" class="mb-1.5">
            <div class="flex items-center gap-2">
              <span class="text-xs text-slate-500 shrink-0 w-24 truncate" :title="chatId">Chat {{ chatId }}</span>
              <input type="text" :value="(cfg.keywords || []).join(', ')"
                     @input="$emit('chat-keywords-input', chatId, $event.target.value)"
                     placeholder="keywords, comma-separated"
                     class="flex-1 min-w-0 px-2 py-1 text-xs border border-slate-300 rounded-lg" />
              <button @click="$emit('remove-chat-trigger', chatId)"
                      class="text-slate-400 hover:text-rose-600 text-sm px-1">&times;</button>
            </div>
            <div v-if="chatKeywordsDirty[chatId]" class="flex gap-2 mt-1 ml-[6.5rem]">
              <button @click="$emit('save-chat-keywords', chatId)" :disabled="busy"
                      class="px-2 py-0.5 text-[11px] font-medium bg-teal-700 text-white rounded-lg disabled:opacity-50">Save</button>
              <button @click="$emit('cancel-chat-keywords', chatId)" :disabled="busy"
                      class="px-2 py-0.5 text-[11px] border border-slate-300 rounded-lg">Cancel</button>
            </div>
          </div>
          <div class="flex items-center gap-2 mt-1">
            <input type="text" v-model="newTriggerChatId" placeholder="Chat ID to add"
                   class="flex-1 min-w-0 px-2 py-1 text-xs border border-slate-300 rounded-lg" />
            <button @click="submitNewTrigger"
                    class="px-2 py-1 text-xs border border-slate-300 rounded-lg">Add</button>
          </div>
        </div>
      </div>

      <!-- Knowledge base (ADR 0046 decision 4) -->
      <div class="rounded-lg border border-slate-200 p-3">
        <span class="text-xs font-semibold uppercase tracking-wide text-slate-600">Knowledge base</span>
        <p class="text-xs text-slate-500 mt-1 mb-2">
          Upload text, Markdown, or PDF documents the agent can search (search_knowledge tool).
          Only text is extracted — scanned/image-only PDFs will not be searchable.
        </p>

        <InlineAlert :message="knowledgeError" class="mb-2" />

        <div v-if="knowledgeDocuments.length" class="space-y-1 mb-2">
          <div v-for="doc in knowledgeDocuments" :key="doc.id"
               class="flex items-center justify-between gap-2 text-sm px-2 py-1 rounded-lg bg-slate-50 border border-slate-200">
            <span class="truncate" :title="doc.filename">{{ doc.filename }}</span>
            <button @click="$emit('delete-knowledge-document', doc.id)"
                    class="text-slate-400 hover:text-rose-600 text-sm px-1 shrink-0">&times;</button>
          </div>
        </div>

        <label class="block">
          <input type="file" accept=".txt,.md,.markdown,text/plain,text/markdown,application/pdf"
                 :disabled="knowledgeUploadBusy" @change="onKnowledgeFilePicked" class="hidden" />
          <span class="inline-block w-full text-center px-3 py-1.5 text-sm border border-slate-300 rounded-lg cursor-pointer hover:bg-slate-50"
                :class="{ 'opacity-50 pointer-events-none': knowledgeUploadBusy }">
            {{ knowledgeUploadBusy ? 'Uploading…' : 'Upload document' }}
          </span>
        </label>
      </div>

      <!-- BYOK (ADR 0046 decision 6) - hidden for now (2026-09-24): not
           available yet, coming soon. Backend still supports it, but the
           agent worker never reads a stored key (see invoke_worker.py). -->
      <div v-if="false" class="rounded-lg border border-slate-200 p-3">
        <span class="text-xs font-semibold uppercase tracking-wide text-slate-600">Your own Gemini key</span>
        <p class="text-xs text-slate-500 mt-1 mb-2">
          Optional. Using your own key skips the shared-key rate limit for Gemini calls; our own
          hourly activation and daily time budgets still apply. The key is encrypted at rest and
          never shown again after saving.
        </p>

        <div class="flex items-center gap-2 mb-2 text-sm">
          <span class="text-slate-500">Status:</span>
          <span v-if="form.has_custom_key" class="text-teal-700 font-medium">Custom key set</span>
          <span v-else class="text-slate-500">Using shared key</span>
          <button v-if="form.has_custom_key" @click="$emit('clear-byok-key')" :disabled="busy"
                  class="ml-auto px-2 py-0.5 text-[11px] border border-slate-300 rounded-lg disabled:opacity-50">Clear</button>
        </div>

        <input type="password" :value="byokKeyInput" @input="$emit('byok-key-input', $event.target.value)"
               autocomplete="off" placeholder="Paste a Gemini API key to replace it"
               class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg" />
        <div v-if="byokDirty" class="flex gap-2 mt-2">
          <button @click="$emit('save-byok-key')" :disabled="busy"
                  class="px-2.5 py-1 text-xs font-medium bg-teal-700 text-white rounded-lg disabled:opacity-50">Save</button>
          <button @click="$emit('cancel-byok-key')" :disabled="busy"
                  class="px-2.5 py-1 text-xs border border-slate-300 rounded-lg">Cancel</button>
        </div>
      </div>

      <InlineAlert :message="error" />
    </div>
  `,
};
