// Active chat's header row: title (click opens members modal for groups),
// group member preview line, and a status line - the typing indicator when
// someone's typing (takes priority for 1:1, since it's more current/urgent
// than "online"), otherwise the 1:1 presence label, otherwise nothing.
const ChatHeader = {
  props: {
    activeChatLabel: { type: String, required: true },
    // Secondary "@username" line for a 1:1 peer who has a display_name (ADR 0024).
    activeChatSubLabel: { type: String, default: '' },
    activeChatIsGroup: { type: Boolean, required: true },
    visibleActiveChatMembers: { type: Array, required: true },
    hiddenActiveChatMemberCount: { type: Number, required: true },
    memberDisplayName: { type: Function, required: true },
    activeChatPresenceLabel: { type: String, required: true },
    activeChatTypingLabel: { type: String, required: true },
    avatarUrl: { default: null },
    avatarPreview: { default: null },
    avatarName: { default: '' },
    avatarColorKey: { default: '' },
  },
  emits: ['open-chat-profile', 'open-chat-search', 'back'],
  template: `
    <div class="px-4 py-2 border-b border-slate-200 bg-white flex items-center gap-3">
      <button @click="$emit('back')" class="md:hidden -ml-1 p-1 text-slate-500 hover:text-slate-800" aria-label="Back">
        <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
      </button>
      <Avatar :url="avatarUrl" :preview="avatarPreview" :name="avatarName" :colorKey="avatarColorKey" sizeClass="w-9 h-9 text-sm" />
      <div class="min-w-0 flex-1 cursor-pointer" @click="$emit('open-chat-profile')">
      <div class="text-sm font-medium hover:underline">{{ activeChatLabel }}</div>
      <div v-if="activeChatSubLabel" class="text-xs text-slate-400 font-mono truncate">{{ activeChatSubLabel }}</div>
      <div v-if="activeChatIsGroup" class="mt-0.5 text-xs text-slate-500 truncate">
        <span v-for="(member, index) in visibleActiveChatMembers" :key="member.user.id">
          {{ memberDisplayName(member) }}<span v-if="index < visibleActiveChatMembers.length - 1 || hiddenActiveChatMemberCount > 0">, </span>
        </span>
        <span v-if="hiddenActiveChatMemberCount > 0">&bull;&bull;&bull;</span>
      </div>
      <div v-if="activeChatTypingLabel" class="mt-0.5 text-xs text-teal-600 truncate italic">
        {{ activeChatTypingLabel }}
      </div>
      <div v-else-if="!activeChatIsGroup && activeChatPresenceLabel" class="mt-0.5 text-xs text-teal-600 truncate">
        {{ activeChatPresenceLabel }}
      </div>
      </div>
      <button type="button" @click="$emit('open-chat-search')"
              class="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-slate-100 text-slate-500 hover:text-slate-700 shrink-0"
              title="Search in this chat">
        <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none"
             stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.2-3.2" />
        </svg>
      </button>
    </div>
  `,
};
