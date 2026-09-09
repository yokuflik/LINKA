// Chat profile screen: opened by tapping the chat header title (private OR
// group). Shows the peer/group avatar (tap -> full-res lightbox), name, phone
// and about text, a Message + (blocked) Call action, and - for groups - the
// full member list with role management + Leave group (all delegated to
// useMembers, same handlers the old MembersModal used).
//
// Needs from ctx: activeChatId, activeChatIsGroup, showToast, selectChat,
// resolveChatMemberPhones, showOwnerTransferPicker.
// Frontend-only, no backend calls of its own.
function useChatProfile(ctx) {
  const { ref } = Vue;

  const showChatProfile = ref(false);

  async function openChatProfile() {
    if (!ctx.activeChatId.value) return;
    showChatProfile.value = true;
    if (ctx.activeChatIsGroup.value) {
      ctx.showOwnerTransferPicker.value = false;
      ctx.resetAddMemberSearch();
      await ctx.resolveChatMemberPhones(ctx.activeChatId.value);
    }
  }

  function closeChatProfile() {
    showChatProfile.value = false;
  }

  // "Message" just drops back to the conversation that's already open.
  function messageFromProfile() {
    showChatProfile.value = false;
  }

  // Voice/video calls aren't built yet - the button is visible but inert.
  function callFromProfile() {
    ctx.showToast('Calls aren’t available yet.', 'info');
  }

  return {
    showChatProfile, openChatProfile, closeChatProfile,
    messageFromProfile, callFromProfile,
  };
}
