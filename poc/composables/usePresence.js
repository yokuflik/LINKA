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

  // user_id -> { status: 'online'|'offline', last_seen_at: ISO|null },
  // populated by the initial presence_status pull and kept current by live
  // presence_update pushes.
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
    if (ctx.draftChat && ctx.draftChat.value) {
      subscribeToPresence(ctx.draftChat.value.otherUserId);
    } else if (!ctx.activeChatIsGroup.value && ctx.activeChatId.value) {
      subscribeToPresenceForChat(ctx.activeChatId.value);
    }
  }

  // Called on the heartbeat: re-send subscribe_presence for the open private
  // chat so the server re-runs the privacy.online gate. Bypasses the
  // "already subscribed" short-circuit in subscribeToPresence - that's the
  // whole point here.
  function refreshPresenceSubscription() {
    if (ctx.draftChat && ctx.draftChat.value) {
      ctx.sendRaw({ type: 'subscribe_presence', user_id: ctx.draftChat.value.otherUserId });
      return;
    }
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

  // 'Last seen' is only ever shown when the user is NOT online: 'online' if
  // connected, otherwise 'last seen <relative time>' when we have a stamp,
  // otherwise nothing.
  function formatLastSeen(iso) {
    const then = new Date(iso);
    if (isNaN(then)) return '';
    const secs = Math.round((Date.now() - then) / 1000);
    if (secs < 60) return 'last seen just now';
    const mins = Math.round(secs / 60);
    if (mins < 60) return `last seen ${mins} min ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `last seen ${hours} hr ago`;
    const days = Math.round(hours / 24);
    if (days < 7) return `last seen ${days} day${days > 1 ? 's' : ''} ago`;
    return `last seen on ${then.toLocaleDateString()}`;
  }

  function presenceLabelFor(userId) {
    const info = presenceByUserId.value[userId];
    if (!info) return '';
    if (info.status === 'online') return 'online';
    return info.last_seen_at ? formatLastSeen(info.last_seen_at) : '';
  }

  const activeChatPresenceLabel = computed(() => {
    // Draft (uncommitted) private chat: presence still shows, gated by the
    // target's privacy.online == "everyone".
    if (ctx.draftChat && ctx.draftChat.value) return presenceLabelFor(ctx.draftChat.value.otherUserId);
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
