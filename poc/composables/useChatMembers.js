// Chat domain — MEMBERS & NAMES.
//
// Everything that turns ids into human-readable strings: the
// /chats/{id}/members resolvers, the name/label helpers, the system-message
// parsing + personalization, the member-list computeds and role gates, and
// the activeChat* label/avatar/pane computeds (which fall back to draftChat).
//
// Merge order: after useChatStore, before useChatList / useChatOpen (both call
// resolvePrivateChatTitle / resolveChatMemberPhones through ctx.*).
//
// Needs from ctx (call-time): apiFetch, logError, currentUser, userById,
// chats, activeChatId, draftChat, groupChatMembers, privateChatTitles,
// privateChatOtherUserId, MAX_VISIBLE_MEMBERS, ROLE_LABELS,
// chatAvatarUrl, chatAvatarName, chatAvatarColorKey, userAvatarUrl.
//
// Global `useChatMembers(ctx)` factory.
function useChatMembers(ctx) {
  const { computed } = Vue;

  // The one place the peer-name fallback lives (ADR 0024): an optional
  // free-form display_name wins, then the unique username, then the phone.
  function peerName(user) {
    if (!user) return '';
    return user.display_name || user.username || user.phone_number || '';
  }
  // Secondary "@username" label - disabled by product choice: when a peer has
  // a display_name we show only that, never the underlying handle.
  function peerHandle(_user) {
    return '';
  }

  // `force` re-fetches even when the title is already cached - used to pick up
  // the other person's freshly changed display name / profile photo (there's
  // no server push for a profile edit, so we re-pull on chat open / tab focus).
  async function resolvePrivateChatTitle(chatId, { force = false } = {}) {
    if (ctx.privateChatTitles.value[chatId] && !force) return;
    try {
      const members = await ctx.apiFetch(`/chats/${chatId}/members`);
      const other = members.find((m) => m.user.id !== ctx.currentUser.value.id);
      if (other) {
        ctx.privateChatTitles.value[chatId] = peerName(other.user);
        ctx.privateChatOtherUserId.value[chatId] = other.user.id;
        // Cache the whole UserOut so the avatar URL needs no second lookup.
        ctx.userById.value[other.user.id] = other.user;
      }
    } catch (err) {
      ctx.logError('failed to resolve private chat title for', chatId, err);
    }
  }

  async function resolveChatMemberPhones(chatId) {
    try {
      const members = await ctx.apiFetch(`/chats/${chatId}/members`);
      for (const m of members) ctx.userById.value[m.user.id] = m.user;
      ctx.groupChatMembers.value[chatId] = members;
    } catch (err) {
      ctx.logError('failed to resolve member phone numbers for', chatId, err);
    }
  }

  function chatDisplayName(chat) {
    if (chat.is_group) return chat.title || 'Untitled group';
    const name = ctx.privateChatTitles.value[chat.id];
    return name ? `Chat with ${name}` : `Private chat #${chat.id}`;
  }

  // "@username" for a 1:1 chat whose peer has a display_name (ADR 0024), else ''.
  function chatSubLabel(chat) {
    if (chat.is_group) return '';
    const otherId = ctx.privateChatOtherUserId.value[chat.id];
    return otherId != null ? peerHandle(ctx.userById.value[otherId]) : '';
  }

  function senderLabel(senderId) {
    if (senderId == null) return 'system';
    const user = ctx.userById.value[senderId];
    if (!user) return senderId;
    return peerName(user);
  }

  function userLabelById(userId) {
    const user = ctx.userById.value[userId];
    if (!user) return userId;
    return peerName(user);
  }

  // Same as senderLabel, but says "You" for the current user - matches
  // WhatsApp's reply-quote convention. Reply UI only.
  function replySenderLabel(senderId) {
    if (senderId === ctx.currentUser.value.id) return 'You';
    return senderLabel(senderId);
  }

  // System messages are plain text EXCEPT the "role_changed" kind (see
  // chat_service.change_member_role), sent as JSON because it's only meant for
  // the two people involved - so the filtering has to happen client-side.
  function parseSystemMessage(content) {
    if (typeof content !== 'string' || content[0] !== '{') return null;
    try {
      const data = JSON.parse(content);
      return data && typeof data === 'object' ? data : null;
    } catch (_) {
      return null;
    }
  }

  // sender_id == null marks a system message; only that subset is checked for
  // the JSON "role_changed" shape.
  function shouldShowSystemMessage(m) {
    if (m.sender_id != null) return true;
    const data = parseSystemMessage(m.content);
    if (!data || data.kind !== 'role_changed') return true;
    const me = ctx.currentUser.value.id;
    return data.actor_id === me || data.target_id === me;
  }

  // All the ways the current user's name can appear inside a plain-text system
  // message the server already rendered.
  function currentUserNameVariants() {
    const u = ctx.currentUser.value;
    if (!u) return [];
    const variants = [u.display_name, u.username, u.phone_number];
    return variants.filter((v) => typeof v === 'string' && v.length > 0);
  }

  // Frontend-only personalization: rewrite a fixed 3rd-person name that is
  // actually me to "you" / "You". Pure string rewrite, no backend change.
  function personalizeSystemMessage(text) {
    if (typeof text !== 'string' || !text) return text;
    let out = text;
    for (const name of currentUserNameVariants()) {
      const esc = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      out = out.replace(new RegExp('^' + esc + '\\b'), 'You');
      out = out.replace(new RegExp('\\b' + esc + '\\b', 'g'), 'you');
    }
    return out;
  }

  function systemMessageText(m) {
    const data = parseSystemMessage(m.content);
    if (!data) return personalizeSystemMessage(m.content);
    if (data.kind === 'role_changed') {
      const actorName = data.actor_id === ctx.currentUser.value.id ? 'You' : userLabelById(data.actor_id);
      const targetName = data.target_id === ctx.currentUser.value.id ? 'you' : userLabelById(data.target_id);
      return data.new_role === 2
        ? `${actorName} made ${targetName} an admin`
        : `${actorName} removed ${targetName} as admin`;
    }
    return m.content;
  }

  const activeChatItem = computed(() => ctx.chats.value.find((c) => c.chat.id === ctx.activeChatId.value) || null);

  // The pane is showing something when there's a real active chat OR an
  // uncommitted draft private chat.
  const activePaneVisible = computed(() => !!ctx.activeChatId.value || !!ctx.draftChat.value);

  function draftUser() {
    const u = ctx.draftChat.value && ctx.userById.value[ctx.draftChat.value.otherUserId];
    return u || (ctx.draftChat.value ? ctx.draftChat.value.user : null);
  }

  const activeChatLabel = computed(() => {
    if (ctx.draftChat.value) {
      const u = draftUser();
      return `Chat with ${u ? peerName(u) : ctx.draftChat.value.phone}`;
    }
    return activeChatItem.value ? chatDisplayName(activeChatItem.value.chat) : '';
  });
  const activeChatIsGroup = computed(() => !ctx.draftChat.value && !!(activeChatItem.value && activeChatItem.value.chat.is_group));
  const activeChatAvatarUrl = computed(() => {
    if (ctx.draftChat.value) { const u = draftUser(); return u ? ctx.userAvatarUrl(u) : null; }
    return activeChatItem.value ? ctx.chatAvatarUrl(activeChatItem.value.chat) : null;
  });
  const activeChatAvatarName = computed(() => {
    if (ctx.draftChat.value) { const u = draftUser(); return u ? peerName(u) : ctx.draftChat.value.phone; }
    return activeChatItem.value ? ctx.chatAvatarName(activeChatItem.value.chat) : '';
  });
  const activeChatAvatarColorKey = computed(() => {
    if (ctx.draftChat.value) return String(ctx.draftChat.value.otherUserId);
    return activeChatItem.value ? ctx.chatAvatarColorKey(activeChatItem.value.chat) : '';
  });
  const activeChatAvatarPreview = computed(() => {
    if (ctx.draftChat.value) { const u = draftUser(); return u ? ctx.userAvatarPreview(u) : null; }
    return activeChatItem.value ? ctx.chatAvatarPreview(activeChatItem.value.chat) : null;
  });

  // Secondary "@username" line under a 1:1 chat header/title - only when the
  // peer has a display_name masking their handle (ADR 0024).
  const activeChatSubLabel = computed(() => {
    if (activeChatIsGroup.value) return '';
    if (ctx.draftChat.value) return peerHandle(draftUser());
    const otherId = activeChatItem.value
      ? ctx.privateChatOtherUserId.value[activeChatItem.value.chat.id]
      : null;
    return otherId != null ? peerHandle(ctx.userById.value[otherId]) : '';
  });

  const activeChatMembers = computed(() => ctx.groupChatMembers.value[ctx.activeChatId.value] || []);
  const visibleActiveChatMembers = computed(() => activeChatMembers.value.slice(0, ctx.MAX_VISIBLE_MEMBERS));
  const hiddenActiveChatMemberCount = computed(() => Math.max(0, activeChatMembers.value.length - ctx.MAX_VISIBLE_MEMBERS));

  function memberDisplayName(member) {
    return peerName(member.user);
  }

  const currentUserRoleInActiveChat = computed(() => {
    const me = activeChatMembers.value.find((m) => m.user.id === ctx.currentUser.value.id);
    return me ? me.role : null;
  });
  const canManageActiveChatMembers = computed(() => (currentUserRoleInActiveChat.value || 0) >= 2);
  const canChangeActiveChatRoles = computed(() => currentUserRoleInActiveChat.value === 3);
  const otherActiveChatMembers = computed(() =>
    activeChatMembers.value.filter((m) => m.user.id !== ctx.currentUser.value.id)
  );

  return {
    resolvePrivateChatTitle, resolveChatMemberPhones,
    chatDisplayName, chatSubLabel, senderLabel, userLabelById, replySenderLabel,
    peerName, peerHandle,
    parseSystemMessage, shouldShowSystemMessage, systemMessageText,
    currentUserNameVariants, personalizeSystemMessage,
    activeChatItem, activePaneVisible, draftUser,
    activeChatLabel, activeChatSubLabel, activeChatIsGroup,
    activeChatAvatarUrl, activeChatAvatarName, activeChatAvatarColorKey, activeChatAvatarPreview,
    activeChatMembers, visibleActiveChatMembers, hiddenActiveChatMemberCount,
    memberDisplayName,
    currentUserRoleInActiveChat, canManageActiveChatMembers, canChangeActiveChatRoles,
    otherActiveChatMembers,
  };
}
