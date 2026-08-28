// Group members modal: member list w/ per-member options popup, leave/
// owner-transfer flow, and the add-member form. All actions are emitted up
// to the root, which still owns every bit of the underlying state/logic.
const MembersModal = {
  props: {
    activeChatLabel: { type: String, required: true },
    activeChatMembers: { type: Array, required: true },
    memberDisplayName: { type: Function, required: true },
    userAvatarUrl: { type: Function, required: true },
    roleLabel: { type: Function, required: true },
    hasMemberOptions: { type: Function, required: true },
    memberOptionsFor: { default: null },
    canChangeActiveChatRoles: { type: Boolean, required: true },
    canRemoveMember: { type: Function, required: true },
    showOwnerTransferPicker: { type: Boolean, required: true },
    leaveGroupBusy: { type: Boolean, required: true },
    ownerTransferTargetId: { required: true },
    otherActiveChatMembers: { type: Array, required: true },
    canManageActiveChatMembers: { type: Boolean, required: true },
    addMemberPhone: { type: String, required: true },
    membersModalBusy: { type: Boolean, required: true },
    membersModalError: { type: String, required: true },
  },
  emits: [
    'close', 'open-member-options', 'member-option-make-or-remove-admin', 'member-option-remove-from-group',
    'start-leave-group', 'confirm-leave-with-transfer', 'cancel-owner-transfer-picker', 'edit-group-info',
    'update:ownerTransferTargetId', 'update:addMemberPhone', 'add-member-to-active-group',
  ],
  template: `
    <div class="fixed inset-0 bg-black/30 flex items-center justify-center z-40" @click.self="$emit('close')">
      <div class="w-full max-w-sm bg-white rounded-xl shadow-lg border border-slate-200 p-4">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold">{{ activeChatLabel }} — Members</h2>
          <button @click="$emit('close')" class="text-slate-400 hover:text-slate-600 text-lg leading-none">&times;</button>
        </div>

        <div class="max-h-64 overflow-y-auto divide-y divide-slate-100">
          <div v-for="member in activeChatMembers" :key="member.user.id" class="relative">
            <div class="py-2 flex items-center justify-between gap-2"
                 :class="hasMemberOptions(member) ? 'cursor-pointer hover:bg-slate-50 -mx-1 px-1 rounded' : ''"
                 @click="$emit('open-member-options', member)">
              <span class="flex items-center gap-2 min-w-0">
                <Avatar :url="userAvatarUrl(member.user)" :name="memberDisplayName(member)"
                        :colorKey="member.user.id" sizeClass="w-7 h-7 text-xs" />
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

        <div v-if="canManageActiveChatMembers" class="mt-3 pt-3 border-t border-slate-200">
          <label class="block text-xs font-medium text-slate-500 mb-1">Add member by phone number</label>
          <div class="flex gap-2">
            <input :value="addMemberPhone" @input="$emit('update:addMemberPhone', $event.target.value)"
                   placeholder="+972501234567" @keyup.enter="$emit('add-member-to-active-group')"
                   class="flex-1 px-2 py-1.5 text-sm border border-slate-300 rounded-lg font-mono" />
            <button @click="$emit('add-member-to-active-group')" :disabled="membersModalBusy"
                    class="px-3 py-1.5 text-sm bg-teal-700 text-white rounded-lg disabled:opacity-50">Add</button>
          </div>
          <p v-if="membersModalError" class="mt-2 text-xs text-red-600">{{ membersModalError }}</p>
        </div>
      </div>
    </div>
  `,
};
