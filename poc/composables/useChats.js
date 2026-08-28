// Chat list + message pane: state, the /chats/{id}/members resolvers, the
// name/label + system-message helpers, the activeChat* computeds, the scroll /
// pin-to-bottom helpers, keyset pagination, and loadChats / selectChat.
// Global `useChats(ctx)` factory.
//
// Needs from ctx (call-time): apiFetch, log, logError, currentUser,
// contactDisplayName, chatAvatarUrl, chatAvatarName, chatAvatarColorKey,
// unreadCountByChatId, markAllChatsDelivered, clearUnreadCount,
// unsubscribeFromPresence, subscribeToPresenceForChat, sendReceipt,
// and still-inline: replyingToMessage, closeMessageContextMenu,
// probeLoadedImageOrientations.
function useChats(ctx) {
  const { ref, computed, nextTick } = Vue;

  // ---------------------------------------------------------------
  // Chats
  // ---------------------------------------------------------------
  const chats = ref([]);
  const activeChatId = ref(null);
  // A private chat the user has "opened" from the sidebar but not yet
  // committed: nothing is created server-side until the first message is
  // sent. Shape: { otherUserId, phone, user } or null. Leaving the pane
  // (selecting any real chat, or closing it) discards it silently.
  const draftChat = ref(null);
  const messages = ref([]);
  const messageInput = ref('');
  const chatsError = ref('');
  const messagesError = ref('');
  const messagesEl = ref(null);
  // Infinite-scroll-up pagination state.
  const hasMoreMessages = ref(false);
  const loadingOlderMessages = ref(false);
  const MESSAGE_PAGE_SIZE = 50;
  // How close to the top (in messages still above the viewport) we get before
  // pulling the next older page.
  const LOAD_OLDER_THRESHOLD = 20;

  // chat_id -> the other participant's phone number, for private chats.
  const privateChatTitles = ref({});
  // chat_id -> the other participant's user id, for private chats only - needed
  // (not just the phone number) to send subscribe_presence.
  const privateChatOtherUserId = ref({});
  // user_id -> that user's UserOut, so a bubble can show who sent it by name.
  const userById = ref({});
  // chat_id -> that chat's member list, straight from /chats/{id}/members.
  const groupChatMembers = ref({});

  // `force` re-fetches even when the title is already cached - used to pick up
  // the other person's freshly changed display name / profile photo (there's
  // no server push for a profile edit, so we re-pull on chat open / tab focus).
  async function resolvePrivateChatTitle(chatId, { force = false } = {}) {
    if (privateChatTitles.value[chatId] && !force) return;
    try {
      const members = await ctx.apiFetch(`/chats/${chatId}/members`);
      const other = members.find((m) => m.user.id !== ctx.currentUser.value.id);
      if (other) {
        privateChatTitles.value[chatId] = other.user.phone_number;
        privateChatOtherUserId.value[chatId] = other.user.id;
        // Cache the whole UserOut so the avatar URL needs no second lookup.
        userById.value[other.user.id] = other.user;
      }
    } catch (err) {
      ctx.logError('failed to resolve private chat title for', chatId, err);
    }
  }

  async function resolveChatMemberPhones(chatId) {
    try {
      const members = await ctx.apiFetch(`/chats/${chatId}/members`);
      for (const m of members) userById.value[m.user.id] = m.user;
      groupChatMembers.value[chatId] = members;
    } catch (err) {
      ctx.logError('failed to resolve member phone numbers for', chatId, err);
    }
  }

  function chatDisplayName(chat) {
    if (chat.is_group) return chat.title || 'Untitled group';
    const phone = privateChatTitles.value[chat.id];
    return phone ? `Chat with ${ctx.contactDisplayName(phone)}` : `Private chat #${chat.id}`;
  }

  function senderLabel(senderId) {
    if (senderId == null) return 'system';
    const user = userById.value[senderId];
    if (!user) return senderId;
    return ctx.contactDisplayName(user.phone_number) || user.display_name || user.phone_number;
  }

  function userLabelById(userId) {
    const user = userById.value[userId];
    if (!user) return userId;
    return ctx.contactDisplayName(user.phone_number) || user.display_name || user.phone_number;
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
    const variants = [u.display_name, u.phone_number, ctx.contactDisplayName(u.phone_number)];
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

  const activeChatItem = computed(() => chats.value.find((c) => c.chat.id === activeChatId.value) || null);

  // The pane is showing something when there's a real active chat OR an
  // uncommitted draft private chat.
  const activePaneVisible = computed(() => !!activeChatId.value || !!draftChat.value);

  function draftUser() {
    const u = draftChat.value && ctx.userById.value[draftChat.value.otherUserId];
    return u || (draftChat.value ? draftChat.value.user : null);
  }

  const activeChatLabel = computed(() => {
    if (draftChat.value) {
      const u = draftUser();
      return `Chat with ${ctx.contactDisplayName(u ? u.phone_number : draftChat.value.phone)}`;
    }
    return activeChatItem.value ? chatDisplayName(activeChatItem.value.chat) : '';
  });
  const activeChatIsGroup = computed(() => !draftChat.value && !!(activeChatItem.value && activeChatItem.value.chat.is_group));
  const activeChatAvatarUrl = computed(() => {
    if (draftChat.value) { const u = draftUser(); return u ? ctx.userAvatarUrl(u) : null; }
    return activeChatItem.value ? ctx.chatAvatarUrl(activeChatItem.value.chat) : null;
  });
  const activeChatAvatarName = computed(() => {
    if (draftChat.value) { const u = draftUser(); return ctx.contactDisplayName(u ? u.phone_number : draftChat.value.phone); }
    return activeChatItem.value ? ctx.chatAvatarName(activeChatItem.value.chat) : '';
  });
  const activeChatAvatarColorKey = computed(() => {
    if (draftChat.value) return String(draftChat.value.otherUserId);
    return activeChatItem.value ? ctx.chatAvatarColorKey(activeChatItem.value.chat) : '';
  });

  // How many member names fit in the header before the rest collapse to "...".
  const MAX_VISIBLE_MEMBERS = 8;
  const activeChatMembers = computed(() => groupChatMembers.value[activeChatId.value] || []);
  const visibleActiveChatMembers = computed(() => activeChatMembers.value.slice(0, MAX_VISIBLE_MEMBERS));
  const hiddenActiveChatMemberCount = computed(() => Math.max(0, activeChatMembers.value.length - MAX_VISIBLE_MEMBERS));

  function memberDisplayName(member) {
    return ctx.contactDisplayName(member.user.phone_number) || member.user.display_name || member.user.phone_number;
  }

  // 1=Member, 2=Admin, 3=Owner (see database/models/participant.py).
  const ROLE_LABELS = { 1: '', 2: 'Admin', 3: 'Owner' };
  function roleLabel(role) { return ROLE_LABELS[role] || ''; }

  const currentUserRoleInActiveChat = computed(() => {
    const me = activeChatMembers.value.find((m) => m.user.id === ctx.currentUser.value.id);
    return me ? me.role : null;
  });
  const canManageActiveChatMembers = computed(() => (currentUserRoleInActiveChat.value || 0) >= 2);
  const canChangeActiveChatRoles = computed(() => currentUserRoleInActiveChat.value === 3);
  const otherActiveChatMembers = computed(() =>
    activeChatMembers.value.filter((m) => m.user.id !== ctx.currentUser.value.id)
  );

  // MessageStatus from the server: 1=sent, 2=delivered, 3=read. Only ever
  // rendered for your own messages.
  function statusTickSymbol(status) { return status >= 2 ? '✓✓' : '✓'; }
  function statusTickClass(status) { return status === 3 ? 'text-sky-500' : 'text-slate-400'; }

  // messagesEl refs the MessageList component instance, which exposes its own
  // scrollable element as `messagesEl`.
  function messagesScrollEl() {
    return messagesEl.value && messagesEl.value.messagesEl;
  }

  // True when the pane is scrolled to (or very near) the bottom.
  function isPinnedToBottom() {
    const el = messagesScrollEl();
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  // Scroll to the bottom now, and keep re-pinning as late-sizing media
  // (images/videos) finishes loading.
  function scrollMessagesToBottom() {
    const el = messagesScrollEl();
    if (!el) return;
    const jump = () => { el.scrollTop = el.scrollHeight; };
    jump();
    requestAnimationFrame(jump);
    const media = el.querySelectorAll('img, video');
    media.forEach((node) => {
      const done = node.tagName === 'IMG' ? node.complete : node.readyState >= 1; // HAVE_METADATA
      if (done) return;
      const onSettled = () => {
        node.removeEventListener('load', onSettled);
        node.removeEventListener('loadedmetadata', onSettled);
        node.removeEventListener('error', onSettled);
        if (isPinnedToBottom()) jump();
      };
      node.addEventListener('load', onSettled);
      node.addEventListener('loadedmetadata', onSettled);
      node.addEventListener('error', onSettled);
    });
  }

  // Fetches the next older page (keyset: before_id = oldest loaded id) and
  // prepends it, preserving the scroll position so the view doesn't jump.
  async function loadOlderMessages() {
    if (loadingOlderMessages.value || !hasMoreMessages.value) return;
    if (!messages.value.length || !activeChatId.value) return;
    const chatId = activeChatId.value;
    const oldestId = messages.value[0].id;
    const el = messagesScrollEl();
    const prevScrollHeight = el ? el.scrollHeight : 0;
    loadingOlderMessages.value = true;
    try {
      const page = await ctx.apiFetch(`/chats/${chatId}/messages?limit=${MESSAGE_PAGE_SIZE}&before_id=${oldestId}`);
      if (activeChatId.value !== chatId) return; // user switched chats mid-flight
      hasMoreMessages.value = page.length === MESSAGE_PAGE_SIZE;
      if (page.length) {
        messages.value = page.slice().reverse().concat(messages.value);
        await nextTick();
        if (el) el.scrollTop = el.scrollHeight - prevScrollHeight;
      }
    } catch (err) {
      ctx.logError('failed to load older messages:', err.message);
    } finally {
      loadingOlderMessages.value = false;
    }
  }

  // MessageList reports how many message rows are scrolled above the viewport.
  function onMessagesScroll(rowsAboveViewport) {
    if (rowsAboveViewport <= LOAD_OLDER_THRESHOLD) loadOlderMessages();
  }

  async function loadChats() {
    chatsError.value = '';
    try {
      chats.value = await ctx.apiFetch('/chats?limit=50');
      ctx.log('loaded', chats.value.length, 'chat(s)');
      await Promise.all(
        chats.value.filter((c) => !c.chat.is_group).map((c) => resolvePrivateChatTitle(c.chat.id, { force: true }))
      );
      // Re-pull members for any group whose list was already fetched, so a
      // participant's changed name/photo propagates to the sidebar and to the
      // open message pane (no server push exists for a profile edit).
      await Promise.all(
        Object.keys(groupChatMembers.value).map((id) => resolveChatMemberPhones(id))
      );

      // Seed the unread badge from the server's real count - replaced
      // wholesale, not merged (the server value is always at least as fresh
      // as anything counted live). The active chat is always forced to
      // 0/absent - it's being read right now.
      const nextUnread = {};
      for (const item of chats.value) {
        if (item.unread_count && item.chat.id !== activeChatId.value) {
          nextUnread[item.chat.id] = item.unread_count;
        }
      }
      ctx.unreadCountByChatId.value = nextUnread;

      // Covers the race with connectWebSocket()'s onopen handler, which also
      // calls this - whichever finishes last has both ready.
      ctx.markAllChatsDelivered();
    } catch (err) {
      chatsError.value = err.message;
    }
  }

  // Open an uncommitted private chat. No server call - just enough state for
  // the pane, header and presence to render. Promoted to a real chat by the
  // first send (see useMessageSend), discarded by opening any real chat.
  function openDraftChat(otherUser) {
    ctx.unsubscribeFromPresence();
    activeChatId.value = null;
    messages.value = [];
    messagesError.value = '';
    hasMoreMessages.value = false;
    ctx.replyingToMessage.value = null;
    if (ctx.editingMessage.value) ctx.cancelEdit();
    ctx.closeMessageContextMenu();
    ctx.userById.value[otherUser.id] = otherUser;
    draftChat.value = { otherUserId: otherUser.id, phone: otherUser.phone_number, user: otherUser };
    // Presence by user id: the subscribe_presence gate is the target's
    // privacy.online setting, not a shared chat - so "everyone" resolves
    // even with no chat row yet ("contacts" won't, by design).
    ctx.subscribeToPresence(otherUser.id);
  }

  function discardDraftChat() {
    if (!draftChat.value) return;
    ctx.unsubscribeFromPresence();
    draftChat.value = null;
  }

  async function selectChat(chatId) {
    discardDraftChat();
    activeChatId.value = chatId;
    messages.value = [];
    messagesError.value = '';
    hasMoreMessages.value = false;
    loadingOlderMessages.value = false;
    ctx.clearUnreadCount(chatId);
    ctx.replyingToMessage.value = null;
    if (ctx.editingMessage.value) ctx.cancelEdit();
    ctx.closeMessageContextMenu();
    const item = chats.value.find((c) => c.chat.id === chatId);
    if (item && !item.chat.is_group) resolvePrivateChatTitle(chatId, { force: true });
    resolveChatMemberPhones(chatId);

    // Subscribe-on-demand presence: only ever one active subscription, scoped
    // to whichever private chat is open right now.
    ctx.unsubscribeFromPresence();
    if (item && !item.chat.is_group) ctx.subscribeToPresenceForChat(chatId);
    try {
      const history = await ctx.apiFetch(`/chats/${chatId}/messages?limit=${MESSAGE_PAGE_SIZE}`);
      // The API returns newest-first (keyset pagination); the UI wants oldest-first.
      messages.value = history.slice().reverse();
      hasMoreMessages.value = history.length === MESSAGE_PAGE_SIZE;
      // GET /chats can't tell us the last message was soft-deleted (its
      // last_message_preview column keeps the old text). The history page
      // does carry deleted_at, so reconcile the sidebar preview here.
      if (item && history.length) {
        const last = history.find((m) => m.id === item.chat.last_message_id);
        if (last && last.deleted_at) item.chat.last_message_preview = '🚫 Message deleted';
      }
      ctx.probeLoadedImageOrientations();
      await nextTick();
      scrollMessagesToBottom();

      // Opening a chat catches up on both receipts in one go, including
      // anything sent while this chat wasn't the active one.
      if (history.length) {
        ctx.sendReceipt('mark_delivered', chatId, history[0].id);
        ctx.sendReceipt('mark_read', chatId, history[0].id);
      }
    } catch (err) {
      messagesError.value = err.message;
    }
  }

  // A profile edit (name / photo) has no server push - the other clients only
  // learn about it by re-pulling. Refresh the open chat's cached users when the
  // tab regains focus, so switching back to it shows the current photo/name.
  function refreshActiveChatUsers() {
    if (document.visibilityState !== 'visible' || !activeChatId.value) return;
    const item = chats.value.find((c) => c.chat.id === activeChatId.value);
    if (!item) return;
    if (item.chat.is_group) resolveChatMemberPhones(activeChatId.value);
    else resolvePrivateChatTitle(activeChatId.value, { force: true });
  }
  document.addEventListener('visibilitychange', refreshActiveChatUsers);

  return {
    chats, activeChatId, draftChat, messages, messageInput, chatsError, messagesError, messagesEl,
    activePaneVisible, openDraftChat, discardDraftChat,
    hasMoreMessages, loadingOlderMessages,
    privateChatTitles, privateChatOtherUserId, userById, groupChatMembers,
    resolvePrivateChatTitle, resolveChatMemberPhones,
    chatDisplayName, senderLabel, userLabelById, replySenderLabel,
    shouldShowSystemMessage, systemMessageText,
    activeChatItem, activeChatLabel, activeChatIsGroup,
    activeChatAvatarUrl, activeChatAvatarName, activeChatAvatarColorKey,
    activeChatMembers, visibleActiveChatMembers, hiddenActiveChatMemberCount,
    memberDisplayName, roleLabel,
    currentUserRoleInActiveChat, canManageActiveChatMembers, canChangeActiveChatRoles,
    otherActiveChatMembers,
    statusTickSymbol, statusTickClass,
    messagesScrollEl, isPinnedToBottom, scrollMessagesToBottom,
    loadOlderMessages, onMessagesScroll,
    loadChats, selectChat,
  };
}
