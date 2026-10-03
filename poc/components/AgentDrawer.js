// AI agent drawer (ADR 0045, AGENT_DRAWER_UI_PLAN.md Step 6) - replaces the
// old centered AgentConfigModal. Slides in from the side; backdrop click or
// the close button dismiss it. First drawer/off-canvas pattern in this
// codebase (every other overlay here is a centered fixed-inset modal).
const AgentDrawer = {
  props: {
    open: { type: Boolean, required: true },
    view: { type: String, required: true }, // 'chat' | 'settings'
    agent: { type: Object, default: null }, // null = not activated yet
    form: { type: Object, default: null },
    busy: { type: Boolean, required: true },
    error: { type: String, default: '' },
    currentUser: { type: Object, required: true },
    chats: { type: Array, required: true },
    chatDisplayName: { type: Function, required: true },
    messages: { type: Array, required: true },
    highlightedId: { default: null },
    messagesLoading: { type: Boolean, default: false },
    messagesHasMore: { type: Boolean, default: false },
    messagesLoadingOlder: { type: Boolean, default: false },
    thinkingStatus: { default: null },
    // a file picked via AgentChatView's [+] menu, staged (not yet sent) so
    // the owner can add a caption first: same shape as MessageInput's prop.
    stagedAttachment: { default: null },
    // Token usage windows (ADR 0059) - {window_5h, window_7d} | null.
    usage: { type: Object, default: null },
    usageBlocked: { type: Boolean, default: false },
    promptDirty: { type: Boolean, required: true },
    chatKeywordsDirty: { type: Object, required: true },
    // Knowledge base (ADR 0046 decision 4) - own upload/delete calls, not
    // part of the diff-then-PATCH agent config.
    knowledgeDocuments: { type: Array, default: () => [] },
    knowledgeUploadBusy: { type: Boolean, default: false },
    knowledgeError: { type: String, default: '' },
    resetBusy: { type: Boolean, default: false },
    // Media-rendering helpers threaded through to AgentChatView so image/
    // video/voice/file bubbles render identically to the main chat pane.
    imageOrientation: { type: Function, required: true },
    thumbHashToDataUrl: { type: Function, required: true },
    thumbHashToAspect: { type: Function, required: true },
    uploadProgress: { type: Object, default: () => ({}) },
  },
  emits: [
    'close', 'activate', 'toggle-enabled', 'open-settings', 'open-search', 'back-to-chat', 'send-chat-message', 'send-attachment', 'load-older-messages', 'pick-knowledge-file', 'stage-attachment', 'clear-attachment',
    'prompt-input', 'save-prompt', 'cancel-prompt',
    'set-restriction',
    'add-chat-trigger', 'remove-chat-trigger', 'set-time-window', 'set-any-message',
    'chat-keywords-input', 'save-chat-keywords', 'cancel-chat-keywords',
    'upload-knowledge-file', 'delete-knowledge-document',
    'reset-agent', 'voice-played',
  ],
  template: `
    <div v-if="open" class="fixed inset-x-0 top-14 z-50 pointer-events-none max-md:h-[calc(100dvh-3.5rem)] md:bottom-0">
      <div class="absolute inset-0 bg-black/30 md:hidden pointer-events-auto" @click="$emit('close')"></div>
      <div class="absolute inset-y-0 left-0 right-0 md:left-72 bg-white border-l border-slate-200 shadow-xl flex flex-col pointer-events-auto">

        <!-- Not activated yet -->
        <div v-if="!form" class="p-4 flex flex-col h-full">
          <div class="flex items-center justify-between mb-3">
            <h2 class="text-sm font-semibold">Your AI Agent</h2>
            <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
          </div>
          <p class="text-sm text-slate-600 mb-3">
            Activate an autonomous agent that can send and read messages on your behalf,
            gated by rules you control.
          </p>
          <InlineAlert :message="error" class="mb-2" />
          <button @click="$emit('activate')" :disabled="busy"
                  class="w-full px-3 py-1.5 text-sm font-medium bg-teal-700 text-white rounded-lg disabled:opacity-50">
            {{ busy ? 'Activating…' : 'Activate agent' }}
          </button>
        </div>

        <!-- Activated -->
        <template v-else>
          <div class="shrink-0 flex items-center justify-between gap-2 px-3 py-2.5 border-b border-slate-200">
            <div class="flex items-center gap-2">
              <button v-if="view === 'settings'" @click="$emit('back-to-chat')"
                      class="text-slate-400 hover:text-slate-600" title="Back to chat">
                <svg viewBox="0 0 24 24" class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2"
                     stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
              </button>
              <!-- Mobile-only: return to the chat list (same affordance as a
                   normal chat's ChatHeader back arrow). Desktop keeps the
                   sidebar visible alongside the drawer, so no button there. -->
              <button v-if="view === 'chat'" @click="$emit('close')" class="md:hidden -ms-1 p-1 text-slate-500 hover:text-slate-800" aria-label="Back">
                <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2"
                     stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
              </button>
              <span class="text-sm font-semibold">{{ view === 'settings' ? 'Agent settings' : 'Your AI Agent' }}</span>
            </div>
            <div class="flex items-center gap-3">
              <!-- Token usage (ADR 0059) - ring fill = 5h window %; the
                   popover it opens is the ONLY place either usage window is
                   shown, per explicit user requirement. -->
              <UsageProgressBar :usage="usage" />
              <!-- Search within the agent's own chat - same modal/behavior as
                   a normal chat's ChatHeader search button. Chat view only,
                   same scoping as the settings-gear icon below. -->
              <button v-if="view === 'chat'" @click="$emit('open-search')"
                      class="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-slate-100 text-slate-500 hover:text-slate-700 shrink-0"
                      title="Search in this chat">
                <svg viewBox="0 0 24 24" class="w-4.5 h-4.5" fill="none" stroke="currentColor" stroke-width="1.8"
                     stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="11" cy="11" r="7" />
                  <path d="m20 20-3.2-3.2" />
                </svg>
              </button>
              <!-- Reset (ADR 0050) - sits right next to the enable/disable
                   toggle, visible in both the chat and settings views. -->
              <button @click="$emit('reset-agent')" :disabled="resetBusy"
                      class="px-2 py-1 text-xs font-medium border border-slate-300 rounded-lg text-slate-500 hover:bg-rose-50 hover:text-rose-600 hover:border-rose-300 disabled:opacity-50 shrink-0"
                      title="Reset agent to default">
                Reset agent
              </button>
              <!-- Master toggle -->
              <button type="button" role="switch" :aria-checked="form.is_enabled"
                      @click="$emit('toggle-enabled', !form.is_enabled)"
                      class="relative w-9 h-5 rounded-full transition-colors shrink-0"
                      :class="form.is_enabled ? 'bg-teal-600' : 'bg-slate-300'"
                      title="Agent enabled">
                <span class="absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform"
                      :class="form.is_enabled ? 'translate-x-4' : 'translate-x-0'"></span>
              </button>
              <button v-if="view === 'chat'" @click="$emit('open-settings')"
                      class="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-slate-100 text-slate-500 hover:text-slate-700"
                      title="Agent settings">
                <svg viewBox="0 0 24 24" class="w-4.5 h-4.5" fill="none" stroke="currentColor" stroke-width="1.8"
                     stroke-linecap="round" stroke-linejoin="round">
                  <line x1="4" y1="6" x2="20" y2="6"/><circle cx="9" cy="6" r="2" fill="currentColor" stroke="none"/>
                  <line x1="4" y1="12" x2="20" y2="12"/><circle cx="15" cy="12" r="2" fill="currentColor" stroke="none"/>
                  <line x1="4" y1="18" x2="20" y2="18"/><circle cx="11" cy="18" r="2" fill="currentColor" stroke="none"/>
                </svg>
              </button>
            </div>
          </div>

          <div class="flex-1 min-h-0" :class="!form.is_enabled ? 'opacity-50 pointer-events-none' : ''">
            <AgentChatView v-if="view === 'chat'"
                           :currentUser="currentUser" :messages="messages" :highlightedId="highlightedId" :loading="messagesLoading"
                           :hasMore="messagesHasMore" :loadingOlder="messagesLoadingOlder"
                           :thinkingStatus="thinkingStatus" :usageBlocked="usageBlocked" :usage="usage"
                           :stagedAttachment="stagedAttachment"
                           :imageOrientation="imageOrientation" :thumbHashToDataUrl="thumbHashToDataUrl" :thumbHashToAspect="thumbHashToAspect"
                           :uploadProgress="uploadProgress"
                           @send="(text) => $emit('send-chat-message', text)"
                           @send-attachment="(text) => $emit('send-attachment', text)"
                           @load-older="$emit('load-older-messages')"
                           @pick-knowledge-file="(file) => $emit('pick-knowledge-file', file)"
                           @stage-attachment="(file, forceKind) => $emit('stage-attachment', file, forceKind)"
                           @clear-attachment="$emit('clear-attachment')"
                           @voice-played="(m) => $emit('voice-played', m)" />
            <AgentSettingsView v-else
                               :form="form" :busy="busy" :error="error"
                               @set-restriction="(k, v) => $emit('set-restriction', k, v)" />
          </div>
        </template>
      </div>
    </div>
  `,
};
