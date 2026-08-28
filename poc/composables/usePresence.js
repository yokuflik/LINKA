// Presence: subscribe-on-demand, 1:1 only, one active subscription at a time
// (see CLAUDE.md's presence section). Global `usePresence(ctx)` factory.
//
// Needs from ctx: sendRaw, activeChatId, activeChatIsGroup,
// privateChatOtherUserId, resolvePrivateChatTitle.
function usePresence(ctx) {
  const { ref, computed } = Vue;

  // Not a ref - nothing in the template reads it directly; mirrors the
  // backend's one-active-subscription-at-a-time contract.
  let presenceSubscribedUserId = null;

  // user_id -> { status: 'online'|'offline' }, populated by the initial
  // presence_status pull and kept current by live presence_update pushes.
  const presenceByUserId = ref({});

  function subscribeToPresence(userId) {
    if (presenceSubscribedUserId === userId) return;
    if (presenceSubscribedUserId != null) unsubscribeFromPresence();
    presenceSubscribedUserId = userId;
    ctx.sendRaw({ type: 'subscribe_presence', user_id: userId });
  }

  function unsubscribeFromPresence() {
    if (presenceSubscribedUserId == null) return;
    ctx.sendRaw({ type: 'unsubscribe_presence', user_id: presenceSubscribedUserId });
    presenceSubscribedUserId = null;
  }

  // Resolves the other participant's id if not cached, then subscribes -
  // guarded on activeChatId still matching when the awaited resolution
  // finishes, since the user may have switched chats in the meantime.
  async function subscribeToPresenceForChat(chatId) {
    if (!ctx.privateChatOtherUserId.value[chatId]) await ctx.resolvePrivateChatTitle(chatId);
    const otherUserId = ctx.privateChatOtherUserId.value[chatId];
    if (otherUserId && ctx.activeChatId.value === chatId) subscribeToPresence(otherUserId);
  }

  // Called from ws.onopen: the old socket's server-side subscription is gone,
  // so drop the local marker and re-subscribe for the currently open chat.
  function resubscribePresenceForActiveChat() {
    presenceSubscribedUserId = null;
    if (!ctx.activeChatIsGroup.value && ctx.activeChatId.value) {
      subscribeToPresenceForChat(ctx.activeChatId.value);
    }
  }

  // Called on the heartbeat: re-send subscribe_presence for the open private
  // chat so the server re-runs the privacy.online gate. Bypasses the
  // "already subscribed" short-circuit in subscribeToPresence - that's the
  // whole point here.
  function refreshPresenceSubscription() {
    if (ctx.activeChatIsGroup.value || !ctx.activeChatId.value) return;
    const otherUserId = ctx.privateChatOtherUserId.value[ctx.activeChatId.value];
    if (otherUserId) ctx.sendRaw({ type: 'subscribe_presence', user_id: otherUserId });
  }

  // Server says this watcher may no longer see that user's status (privacy
  // tightened, or the shared chat is gone). Clear the stale indicator.
  function onPresenceRevoked(targetUserId) {
    if (presenceSubscribedUserId === targetUserId) presenceSubscribedUserId = null;
    delete presenceByUserId.value[targetUserId];
  }

  function resetPresence() {
    presenceSubscribedUserId = null;
    presenceByUserId.value = {};
  }

  // "Last seen" is deliberately not shown - only ever "online", or nothing.
  function presenceLabelFor(userId) {
    const info = presenceByUserId.value[userId];
    return info && info.status === 'online' ? 'online' : '';
  }

  const activeChatPresenceLabel = computed(() => {
    if (ctx.activeChatIsGroup.value || !ctx.activeChatId.value) return '';
    const otherUserId = ctx.privateChatOtherUserId.value[ctx.activeChatId.value];
    return otherUserId ? presenceLabelFor(otherUserId) : '';
  });

  return {
    presenceByUserId,
    subscribeToPresence, unsubscribeFromPresence, subscribeToPresenceForChat,
    resubscribePresenceForActiveChat, refreshPresenceSubscription, onPresenceRevoked,
    resetPresence,
    presenceLabelFor, activeChatPresenceLabel,
  };
}
