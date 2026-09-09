// Chat profile screen (opened from the chat header title). A slide-over panel:
//   - big avatar (tap -> full-res lightbox via <Avatar>), name, phone, about
//   - "Message" + "Call" actions (Call is blocked/inert for now)
//   - for a GROUP: the member list with role management + Leave group,
//     identical behaviour to the old MembersModal (same emits, owned by
//     useMembers on the root).
const ChatProfileModal = {
  props: {
    isGroup: { type: Boolean, required: true },
    title: { type: String, required: true },
    peerUser: { default: null },          // UserOut for a 1:1 chat, else null
    aboutText: { default: '' },
    avatarUrl: { default: null },
    avatarPreview: { default: null },
    avatarName: { default: '' },
    avatarColorKey: { default: '' },

    // --- group member management (mirrors MembersModal) ---
    activeChatMembers: { type: Array, default: () => [] },
    memberDisplayName: { type: Function, default: () => '' },
    userAvatarUrl: { type: Function, default: () => null },
    userAvatarPreview: { type: Function, default: () => null },
    roleLabel: { type: Function, default: () => '' },
    hasMemberOptions: { type: Function, default: () => false },
    memberOptionsFor: { default: null },
    canChangeActiveChatRoles: { type: Boolean, default: false },
    canRemoveMember: { type: Function, default: () => false },
    showOwnerTransferPicker: { type: Boolean, default: false },
    leaveGroupBusy: { type: Boolean, default: false },
    ownerTransferTargetId: { default: '' },
    otherActiveChatMembers: { type: Array, default: () => [] },
    canManageActiveChatMembers: { type: Boolean, default: false },
    membersModalBusy: { type: Boolean, default: false },
    membersModalError: { type: String, default: '' },
    // add-member search (mirrors NewGroupModal step 1)
    addMemberSearchQuery: { type: String, default: '' },
    addMemberSearchBusy: { type: Boolean, default: false },
    addMemberSearchError: { type: String, default: '' },
    addMemberSearchResult: { default: null },
    addMemberResultIsMember: { type: Boolean, default: false },
    userLabel: { type: Function, default: () => '' },
  },
  emits: [
    'close', 'message', 'call',
    'open-member-options', 'member-option-make-or-remove-admin', 'member-option-remove-from-group',
    'start-leave-group', 'confirm-leave-with-transfer', 'cancel-owner-transfer-picker', 'edit-group-info',
    'update:ownerTransferTargetId',
    'update:addMemberSearchQuery', 'add-member-search-input', 'add-member-search', 'add-member',
  ],
  computed: {
    phoneNumber() {
      return this.peerUser && this.peerUser.phone_number ? this.peerUser.phone_number : '';
    },
  },
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-stretch justify-end z-40" @click.self="$emit('close')">
      <div class="w-full max-w-sm bg-white h-full shadow-xl flex flex-col">
        <div class="px-4 py-3 border-b border-slate-200 flex items-center gap-3 shrink-0">
          <button @click="$emit('close')" class="-ml-1 p-1 text-slate-500 hover:text-slate-800" aria-label="Back">
            <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
          </button>
          <span class="text-sm font-semibold">{{ isGroup ? 'Group info' : 'Contact info' }}</span>
        </div>

        <div class="flex-1 overflow-y-auto">
          <div class="flex flex-col items-center text-center px-4 py-6 border-b border-slate-100">
            <Avatar :url="avatarUrl" :preview="avatarPreview" :name="avatarName" :colorKey="avatarColorKey"
                    sizeClass="w-28 h-28 text-3xl" />
            <div class="mt-3 text-lg font-semibold">{{ title }}</div>
            <div v-if="phoneNumber" class="mt-0.5 text-sm text-slate-500 font-mono">{{ phoneNumber }}</div>
          </div>

          <div v-if="aboutText" class="px-4 py-4 border-b border-slate-100">
            <div class="text-xs font-medium text-slate-400 mb-1">About</div>
            <div class="text-sm whitespace-pre-wrap break-words">{{ aboutText }}</div>
          </div>

          <div class="px-4 py-4 flex gap-3 border-b border-slate-100">
            <button @click="$emit('message')"
                    class="flex-1 flex flex-col items-center gap-1 py-2 rounded-xl text-teal-700 hover:bg-teal-50">
              <svg viewBox="0 0 24 24" class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
              </svg>
              <span class="text-xs font-medium">Message</span>
            </button>
            <button @click="$emit('call')"
                    class="flex-1 flex flex-col items-center gap-1 py-2 rounded-xl text-slate-400 hover:bg-slate-50 cursor-not-allowed">
              <svg viewBox="0 0 24 24" class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.9.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/>
                <line x1="3" y1="3" x2="21" y2="21"/>
              </svg>
              <span class="text-xs font-medium">Call</span>
            </button>
          </div>

          <div v-if="isGroup" class="px-4 py-4">
            <div class="text-xs font-medium text-slate-400 mb-2">
              {{ activeChatMembers.length }} participant{{ activeChatMembers.length === 1 ? '' : 's' }}
            </div>

            <div class="divide-y divide-slate-100">
              <div v-for="member in activeChatMembers" :key="member.user.id" class="relative">
                <div class="py-2 flex items-center justify-between gap-2"
                     :class="hasMemberOptions(member) ? 'cursor-pointer hover:bg-slate-50 -mx-1 px-1 rounded' : ''"
                     @click="$emit('open-member-options', member)">
                  <span class="flex items-center gap-2 min-w-0">
                    <Avatar :url="userAvatarUrl(member.user)" :preview="userAvatarPreview(member.user)"
                            :name="memberDisplayName(member)" :colorKey="member.user.id" :enlargeable="false"
                            sizeClass="w-8 h-8 text-xs" />
                    <span class="text-sm truncate">{{ memberDisplayName(member) }}</span>
                  </span>
                  <span v-if="roleLabel(member.role)" class="shrink-0 ml-2 text-[10px] font-medium px-2 py-0.5 rounded-full bg-teal-100 text-teal-700">{{ roleLabel(member.role) }}</span>
                </div>

                <div v-if="memberOptionsFor === member" class="mb-2 border border-slate-200 rounded-lg overflow-hidden text-sm">
                  <button v-if="canChangeActiveChatRoles && member.role !== 3" @click="$emit('member-option-make-or-remove-admin')"
                          class="w-full text-left px-3 py-2 hover:bg-slate-50 border-b border-slate-100">
                    {{ member.role === 2 ? 'Remove as admin' : 'Make admin' }}
                  </button>
                  <button v-if="canRemoveMember(member)" @click="$emit('member-option-remove-from-group')"
                          class="w-full text-left px-3 py-2 hover:bg-slate-50 text-red-600">
                    Remove from group
                  </button>
                </div>
              </div>
              <p v-if="!activeChatMembers.length" class="py-3 text-sm text-slate-400">No members loaded.</p>
            </div>

            <div v-if="canManageActiveChatMembers && !showOwnerTransferPicker" class="mt-3 pt-3 border-t border-slate-200">
              <label class="block text-xs font-medium text-slate-500 mb-1">Add member</label>
              <div class="flex gap-2">
                <input :value="addMemberSearchQuery"
                       @input="$emit('update:addMemberSearchQuery', $event.target.value); $emit('add-member-search-input')"
                       @keyup.enter="$emit('add-member-search')"
                       placeholder="username or +972501234567"
                       class="flex-1 px-2 py-1.5 text-sm border border-slate-300 rounded-lg" />
                <button @click="$emit('add-member-search')" :disabled="addMemberSearchBusy || !addMemberSearchQuery.trim()"
                        class="px-3 py-1.5 text-sm bg-teal-700 text-white rounded-lg disabled:opacity-50">
                  {{ addMemberSearchBusy ? '…' : 'Search' }}
                </button>
              </div>
              <InlineAlert :message="addMemberSearchError" class="mt-2" />

              <button v-if="addMemberSearchResult" @click="!addMemberResultIsMember && $emit('add-member', addMemberSearchResult)"
                      :disabled="addMemberResultIsMember || membersModalBusy"
                      class="w-full mt-2 flex items-center gap-3 p-2 rounded-lg border border-slate-200 text-left"
                      :class="addMemberResultIsMember ? 'opacity-60 cursor-default' : 'hover:bg-slate-50'">
                <Avatar :url="userAvatarUrl(addMemberSearchResult)" :preview="userAvatarPreview(addMemberSearchResult)"
                        :name="userLabel(addMemberSearchResult)" :colorKey="String(addMemberSearchResult.id)"
                        :enlargeable="false" sizeClass="w-9 h-9 text-sm" />
                <span class="flex-1 min-w-0 text-sm truncate">{{ userLabel(addMemberSearchResult) }}</span>
                <span class="shrink-0 text-xs" :class="addMemberResultIsMember ? 'text-slate-400' : 'text-teal-600 font-medium'">
                  {{ addMemberResultIsMember ? 'Already in' : 'Add' }}
                </span>
              </button>

              <InlineAlert :message="membersModalError" class="mt-2" />
            </div>

            <div v-if="canManageActiveChatMembers && !showOwnerTransferPicker" class="mt-3 pt-3 border-t border-slate-200">
              <button @click="$emit('edit-group-info')"
                      class="w-full text-left px-3 py-2 text-sm font-medium text-teal-700 hover:bg-teal-50 rounded-lg">
                Edit group info
              </button>
            </div>

            <div v-if="!showOwnerTransferPicker" class="mt-3 pt-3 border-t border-slate-200">
              <button @click="$emit('start-leave-group')" :disabled="leaveGroupBusy"
                      class="w-full text-left px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-50 rounded-lg disabled:opacity-50">
                Leave group
              </button>
            </div>

            <div v-else class="mt-3 pt-3 border-t border-slate-200">
              <label class="block text-xs font-medium text-slate-500 mb-1">
                You're the owner - pick who takes over before leaving
              </label>
              <select :value="ownerTransferTargetId" @change="$emit('update:ownerTransferTargetId', $event.target.value)"
                      class="w-full px-2 py-1.5 text-sm border border-slate-300 rounded-lg mb-2">
                <option v-for="m in otherActiveChatMembers" :key="m.user.id" :value="m.user.id">
                  {{ memberDisplayName(m) }}
                </option>
              </select>
              <div class="flex gap-2">
                <button @click="$emit('confirm-leave-with-transfer')" :disabled="leaveGroupBusy"
                        class="flex-1 px-3 py-1.5 text-sm font-medium bg-red-600 text-white rounded-lg disabled:opacity-50">
                  Transfer &amp; leave
                </button>
                <button @click="$emit('cancel-owner-transfer-picker')" :disabled="leaveGroupBusy"
                        class="px-3 py-1.5 text-sm border border-slate-300 rounded-lg">
                  Cancel
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `,
};
