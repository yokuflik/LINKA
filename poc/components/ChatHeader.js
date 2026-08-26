// Active chat's header row: title (click opens members modal for groups),
// group member preview line, or 1:1 presence label.
const ChatHeader = {
  props: {
    activeChatLabel: { type: String, required: true },
    activeChatIsGroup: { type: Boolean, required: true },
    visibleActiveChatMembers: { type: Array, required: true },
    hiddenActiveChatMemberCount: { type: Number, required: true },
    memberDisplayName: { type: Function, required: true },
    activeChatPresenceLabel: { type: String, required: true },
  },
  emits: ['open-members-modal'],
  template: `
    <div class="px-4 py-2 border-b border-slate-200 bg-white">
      <div class="text-sm font-medium"
           :class="activeChatIsGroup ? 'cursor-pointer hover:underline' : ''"
           @click="activeChatIsGroup && $emit('open-members-modal')">{{ activeChatLabel }}</div>
      <div v-if="activeChatIsGroup" class="mt-0.5 text-xs text-slate-500 truncate">
        <span v-for="(member, index) in visibleActiveChatMembers" :key="member.user.id">
          {{ memberDisplayName(member) }}<span v-if="index < visibleActiveChatMembers.length - 1 || hiddenActiveChatMemberCount > 0">, </span>
        </span>
        <span v-if="hiddenActiveChatMemberCount > 0">&bull;&bull;&bull;</span>
      </div>
      <div v-else-if="activeChatPresenceLabel" class="mt-0.5 text-xs text-teal-600 truncate">
        {{ activeChatPresenceLabel }}
      </div>
    </div>
  `,
};
