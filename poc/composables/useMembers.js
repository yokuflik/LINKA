// Group members modal: the member list, add-member, admin toggle, the
// per-member options popup, and leaving / transferring ownership of a group.
// Global `useMembers(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, currentUser, activeChatId, activeChatLabel, chats,
// messages, resolveChatMemberPhones, currentUserRoleInActiveChat,
// canChangeActiveChatRoles, otherActiveChatMembers.
function useMembers(ctx) {
  const { ref, computed } = Vue;

  // ---------------------------------------------------------------
  // Group members modal
  // ---------------------------------------------------------------
  const showMembersModal = ref(false);
  const addMemberPhone = ref('');
  const membersModalError = ref('');
  const membersModalBusy = ref(false);

  async function openMembersModal() {
    if (!ctx.activeChatId.value) return;
    membersModalError.value = '';
    addMemberPhone.value = '';
    showOwnerTransferPicker.value = false;
    showMembersModal.value = true;
    await ctx.resolveChatMemberPhones(ctx.activeChatId.value);
  }

  // ---------------------------------------------------------------
  // Add-member search - identical UX to NewGroupModal step 1: a debounced,
  // seq-guarded exact-match lookup by username or phone (+digits ->
  // /users/by-phone, else /users/by-username), surfaced as one result row.
  // ---------------------------------------------------------------
  const addMemberSearchQuery = ref('');
  const addMemberSearchBusy = ref(false);
  const addMemberSearchError = ref('');
  const addMemberSearchResult = ref(null); // a UserOut, or null
  let addMemberSearchSeq = 0;
  let addMemberSearchTimer = null;

  function resetAddMemberSearch() {
    if (addMemberSearchTimer) { clearTimeout(addMemberSearchTimer); addMemberSearchTimer = null; }
    addMemberSearchQuery.value = '';
    addMemberSearchBusy.value = false;
    addMemberSearchError.value = '';
    addMemberSearchResult.value = null;
  }

  function onAddMemberSearchInput() {
    addMemberSearchError.value = '';
    addMemberSearchResult.value = null;
    if (addMemberSearchTimer) clearTimeout(addMemberSearchTimer);
    addMemberSearchSeq++;
    addMemberSearchBusy.value = false;
    if (!addMemberSearchQuery.value.trim()) return;
    addMemberSearchTimer = setTimeout(() => { addMemberSearchTimer = null; runAddMemberSearch(); }, 1500);
  }

  async function runAddMemberSearch() {
    if (addMemberSearchTimer) { clearTimeout(addMemberSearchTimer); addMemberSearchTimer = null; }
    const raw = addMemberSearchQuery.value.trim();
    addMemberSearchError.value = '';
    addMemberSearchResult.value = null;
    if (!raw) return;
    const seq = ++addMemberSearchSeq;
    addMemberSearchBusy.value = true;
    try {
      const looksLikePhone = /^\+?\d[\d\s-]*$/.test(raw);
      const path = looksLikePhone
        ? `/users/by-phone?phone_number=${encodeURIComponent(raw.replace(/[\s-]/g, ''))}`
        : `/users/by-username?username=${encodeURIComponent(raw.replace(/^@/, ''))}`;
      const user = await ctx.apiFetch(path);
      if (seq !== addMemberSearchSeq) return;
      ctx.userById.value[user.id] = user;
      addMemberSearchResult.value = user;
    } catch (err) {
      if (seq !== addMemberSearchSeq) return;
      if (err.status === 404) {
        const digits = raw.replace(/[\s-]/g, '').replace(/^\+/, '');
        const looksLikeFullPhone = /^\+?\d[\d\s-]*$/.test(raw) && digits.length >= 8 && digits.length <= 15;
        addMemberSearchError.value = looksLikeFullPhone
          ? 'This number is not registered with the service yet.'
          : 'No user found — check the exact username or phone number.';
      } else {
        addMemberSearchError.value = ctx.friendlyError(err, "We couldn't run that search. Please try again.");
      }
    } finally {
      if (seq === addMemberSearchSeq) addMemberSearchBusy.value = false;
    }
  }

  // Whether the current search hit is already a member of this group.
  const addMemberResultIsMember = computed(() => {
    const hit = addMemberSearchResult.value;
    if (!hit) return false;
    return ctx.activeChatMembers.value.some((m) => m.user.id === hit.id);
  });

  // Add the resolved user (from the search hit) to the active group.
  async function addMemberToActiveGroup(user) {
    const target = user || addMemberSearchResult.value;
    if (!target) return;
    membersModalError.value = '';
    membersModalBusy.value = true;
    try {
      await ctx.apiFetch(`/chats/${ctx.activeChatId.value}/members`, {
        method: 'POST',
        body: JSON.stringify({ user_id: target.id }),
      });
      resetAddMemberSearch();
      await ctx.resolveChatMemberPhones(ctx.activeChatId.value);
    } catch (err) {
      if (err.status === 409) membersModalError.value = 'User is already a member.';
      else membersModalError.value = ctx.friendlyError(err, "That didn't work. Please try again.");
    } finally {
      membersModalBusy.value = false;
    }
  }

  // Toggles a member between Admin (2) and plain Member (1) - Owner (3) is
  // never a toggle target here, only ever set at group creation.
  // PATCH /chats/{id}/members/{user_id} re-checks ROLE_OWNER server-side,
  // this is just the matching client-side gate (canChangeActiveChatRoles).
  //
  // The server (chat_service.change_member_role) sends a real system
  // message for this, fanned out to the whole chat like any other - but
  // as structured JSON content with a "role_changed" kind instead of
  // plain text, since this notice is only meant for the actor/target pair,
  // not everyone. See handleWsMessage's parsing and shouldShowSystemMessage().
  async function toggleMemberAdmin(member) {
    if (!ctx.canChangeActiveChatRoles.value || member.role === 3) return;
    const chatId = ctx.activeChatId.value;
    const newRole = member.role === 1 ? 2 : 1;
    membersModalError.value = '';
    try {
      await ctx.apiFetch(`/chats/${chatId}/members/${member.user.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ role: newRole }),
      });
      member.role = newRole;
    } catch (err) {
      if (err.status === 403) membersModalError.value = 'Only the group owner can change roles.';
      else membersModalError.value = ctx.friendlyError(err, "That didn't work. Please try again.");
    }
  }

  // ---------------------------------------------------------------
  // Per-member options popup (click a member's name in the modal) -
  // "Make admin"/"Remove as admin" wired to toggleMemberAdmin above;
  // "Remove from group" wired to memberOptionRemoveFromGroup below.
  // ---------------------------------------------------------------
  const memberOptionsFor = ref(null); // the member object the popup is currently open for, or null

  // Whether the options popup has anything to show for this member at all -
  // used both to gate opening it and to style the row as clickable.
  function hasMemberOptions(member) {
    const canToggleAdmin = ctx.canChangeActiveChatRoles.value && member.role !== 3;
    return canToggleAdmin || canRemoveMember(member);
  }

  function openMemberOptions(member) {
    if (!hasMemberOptions(member)) return;
    memberOptionsFor.value = memberOptionsFor.value === member ? null : member;
  }

  async function memberOptionMakeOrRemoveAdmin() {
    const member = memberOptionsFor.value;
    memberOptionsFor.value = null;
    if (member) await toggleMemberAdmin(member);
  }

  // Mirrors chat_service.remove_member's server-side rule: an admin may
  // only remove a plain member - not another admin, and not the owner.
  // Only the owner outranks an admin and can remove one.
  function canRemoveMember(member) {
    if (member.user.id === ctx.currentUser.value.id) return false;
    return (ctx.currentUserRoleInActiveChat.value || 0) > member.role;
  }

  async function memberOptionRemoveFromGroup() {
    const member = memberOptionsFor.value;
    memberOptionsFor.value = null;
    if (!member || !canRemoveMember(member)) return;
    membersModalError.value = '';
    try {
      await ctx.apiFetch(`/chats/${ctx.activeChatId.value}/members/${member.user.id}`, { method: 'DELETE' });
      await ctx.resolveChatMemberPhones(ctx.activeChatId.value);
    } catch (err) {
      if (err.status === 403) membersModalError.value = 'You cannot remove this member.';
      else membersModalError.value = ctx.friendlyError(err, "That didn't work. Please try again.");
    }
  }

  // ---------------------------------------------------------------
  // Leave group - available to every member (not just admins/owner).
  // Same DELETE /chats/{id}/members/{user_id} endpoint as removing
  // someone else, but chat_service.remove_member skips the role check
  // when actor_id === target_user_id.
  //
  // The owner can't just vanish while others remain - chat_service
  // requires a new_owner_id in that case (409 OwnershipTransferRequired
  // otherwise). startLeaveGroup is what decides which path to take;
  // leaveActiveGroup(newOwnerId) does the actual call either way. If the
  // owner is the last one left, there's nobody to transfer to and the
  // server deletes the whole chat instead - same call, no picker needed.
  // ---------------------------------------------------------------
  const leaveGroupBusy = ref(false);
  const showOwnerTransferPicker = ref(false);
  const ownerTransferTargetId = ref('');

  function startLeaveGroup() {
    if (!ctx.activeChatId.value) return;
    const others = ctx.otherActiveChatMembers.value;
    if (ctx.currentUserRoleInActiveChat.value === 3 && others.length) {
      ownerTransferTargetId.value = others[0].user.id;
      showOwnerTransferPicker.value = true;
      return;
    }
    leaveActiveGroup(null);
  }

  function cancelOwnerTransferPicker() {
    showOwnerTransferPicker.value = false;
  }

  async function confirmLeaveWithTransfer() {
    if (!ownerTransferTargetId.value) return;
    await leaveActiveGroup(ownerTransferTargetId.value);
  }

  async function leaveActiveGroup(newOwnerId) {
    if (!ctx.activeChatId.value || leaveGroupBusy.value) return;
    if (!newOwnerId && !confirm(`Leave "${ctx.activeChatLabel.value}"?`)) return;
    const chatId = ctx.activeChatId.value;
    leaveGroupBusy.value = true;
    membersModalError.value = '';
    try {
      const qs = newOwnerId ? `?new_owner_id=${newOwnerId}` : '';
      await ctx.apiFetch(`/chats/${chatId}/members/${ctx.currentUser.value.id}${qs}`, { method: 'DELETE' });
      showMembersModal.value = false;
      showOwnerTransferPicker.value = false;
      ctx.closeChatProfile?.();  // close the group-details slide-over on leave
      ctx.chats.value = ctx.chats.value.filter((c) => c.chat.id !== chatId);
      ctx.activeChatId.value = null;
      ctx.messages.value = [];
    } catch (err) {
      membersModalError.value = err.status === 409 ? 'Name a new owner first.' : ctx.friendlyError(err, "We couldn't complete that. Please try again.");
    } finally {
      leaveGroupBusy.value = false;
    }
  }

  return {
    showMembersModal, addMemberPhone, membersModalError, membersModalBusy,
    openMembersModal, addMemberToActiveGroup, toggleMemberAdmin,
    addMemberSearchQuery, addMemberSearchBusy, addMemberSearchError, addMemberSearchResult,
    addMemberResultIsMember, resetAddMemberSearch, onAddMemberSearchInput, runAddMemberSearch,
    memberOptionsFor, hasMemberOptions, openMemberOptions,
    memberOptionMakeOrRemoveAdmin, canRemoveMember, memberOptionRemoveFromGroup,
    leaveGroupBusy, showOwnerTransferPicker, ownerTransferTargetId,
    startLeaveGroup, cancelOwnerTransferPicker, confirmLeaveWithTransfer, leaveActiveGroup,
  };
}
