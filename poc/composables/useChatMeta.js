// Chat-list / sidebar metadata helpers: toasts, the last-message preview line,
// per-message status refresh, timestamp formatting, the client-only contact-name
// lookup, and the avatar-URL resolvers. Global `useChatMeta(ctx)` factory.
//
// Needs from ctx (call-time): apiFetch, log, logError, currentUser, chats,
// messages, privateChatTitles, privateChatOtherUserId, userById.
function useChatMeta(ctx) {
  const { ref, computed } = Vue;

  // ---------------------------------------------------------------
  // Toasts - lightweight, auto-dismissing notifications
  // ---------------------------------------------------------------
  const toasts = ref([]);
  function showToast(text) {
    const id = crypto.randomUUID();
    toasts.value.push({ id, text });
    setTimeout(() => { toasts.value = toasts.value.filter((t) => t.id !== id); }, 4000);
  }

  // Mirrors the backend's crud_message.build_last_message_preview: a
  // caption-less media message shows a "kind" label, not an empty string, in
  // the sidebar. A media message WITH a caption shows the caption.
  function previewText(content, type) {
    if (content) return content;
    return { 2: '📷 Photo', 3: '🎥 Video', 4: '🎤 Voice message', 5: '📎 File' }[type] || content || '';
  }

  // Keeps the sidebar's preview line in sync with what the server already
  // wrote to chats.last_message_* when it persisted this message.
  function bumpChatPreview(chatId, messageId, lastMessageAt, content, status) {
    const item = ctx.chats.value.find((c) => c.chat.id === chatId);
    if (!item) return;
    item.chat.last_message_at = lastMessageAt;
    item.chat.last_message_id = messageId;
    item.chat.last_message_preview = content;
    item.chat.last_message_status = status;
    // Pin-aware: pinned chats stay on top regardless of activity (useChats.sortChats).
    ctx.sortChats();
  }

  // Re-pulls just the status field for whatever's currently loaded in the open
  // conversation, after a delivery/read receipt event. Only touches messages
  // the server's page still knows about; older history is left alone.
  //
  // Coalesced + generation-guarded: a burst of receipt events (e.g. the other
  // side reading 16 messages in a row) would otherwise fire 16 concurrent
  // GETs whose responses can land out of order, an early one clobbering the
  // final state. We debounce to one trailing fetch, and stamp each fetch with
  // a generation so a slow older response is discarded.
  let _statusRefreshTimer = null;
  let _statusRefreshGen = 0;
  function refreshMessageStatuses(chatId) {
    if (_statusRefreshTimer) clearTimeout(_statusRefreshTimer);
    _statusRefreshTimer = setTimeout(() => {
      _statusRefreshTimer = null;
      const gen = ++_statusRefreshGen;
      ctx.apiFetch(`/chats/${chatId}/messages?limit=50`)
        .then((page) => {
          if (gen !== _statusRefreshGen || chatId !== ctx.activeChatId.value) return;
          const statusById = new Map(page.map((m) => [m.id, m.status]));
          for (const m of ctx.messages.value) {
            if (statusById.has(m.id)) m.status = statusById.get(m.id);
          }
        })
        .catch((err) => ctx.logError('failed to refresh message statuses for', chatId, err));
    }, 250);
  }

  // Mirrors the server-side rule in crud_message: editing or deleting a
  // message only changes a chat's preview when it's the previewed one.
  function updateChatPreviewIfLast(chatId, messageId, content) {
    const item = ctx.chats.value.find((c) => c.chat.id === chatId);
    if (item && item.chat.last_message_id === messageId) item.chat.last_message_preview = content;
  }

  // WhatsApp-style timestamp: today is a time, yesterday is a word, anything
  // older is a date.
  function formatChatTime(iso) {
    if (!iso) return '';
    const at = new Date(iso);
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (at >= startOfToday) return at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (at >= startOfYesterday) return 'Yesterday';
    return at.toLocaleDateString([], { day: '2-digit', month: '2-digit', year: '2-digit' });
  }

  // Peer names come straight from the server: username, else the
  // phone number. No client-side contact book.

  // ---------------------------------------------------------------
  // Profile-picture avatars (WhatsApp-style small circles). The <Avatar>
  // component renders the image directly with native lazy loading; these
  // helpers just resolve which URL / name / color-key each place hands it.
  // `profile_pic_url` on a UserOut / ChatOut is already an absolute public URL.
  // ---------------------------------------------------------------
  function chatAvatarName(chat) {
    if (chat.is_group) return chat.title || '';
    return ctx.privateChatTitles.value[chat.id] || '';
  }
  function chatAvatarColorKey(chat) {
    return chat.is_group ? chat.id : (ctx.privateChatOtherUserId.value[chat.id] || chat.id);
  }
  function chatAvatarUrl(chat) {
    if (chat.is_group) return chat.profile_pic_url || null;
    const otherId = ctx.privateChatOtherUserId.value[chat.id];
    const u = otherId ? ctx.userById.value[otherId] : null;
    return u ? (u.profile_pic_url || null) : null;
  }
  function userAvatarUrl(user) {
    return user ? (user.profile_pic_url || null) : null;
  }
  function senderAvatarUrl(senderId) {
    return userAvatarUrl(ctx.userById.value[senderId]);
  }
  const currentUserAvatarUrl = computed(() => userAvatarUrl(ctx.currentUser.value));

  // ThumbHash blur placeholders for the same avatars (ADR 0015). The <Avatar>
  // component renders the blur only; the full image loads on tap (lightbox).
  function chatAvatarPreview(chat) {
    if (chat.is_group) return chat.profile_pic_preview || null;
    const otherId = ctx.privateChatOtherUserId.value[chat.id];
    const u = otherId ? ctx.userById.value[otherId] : null;
    return u ? (u.profile_pic_preview || null) : null;
  }
  function userAvatarPreview(user) {
    return user ? (user.profile_pic_preview || null) : null;
  }
  function senderAvatarPreview(senderId) {
    return userAvatarPreview(ctx.userById.value[senderId]);
  }
  const currentUserAvatarPreview = computed(() => userAvatarPreview(ctx.currentUser.value));

  return {
    toasts, showToast,
    previewText, bumpChatPreview, refreshMessageStatuses, updateChatPreviewIfLast,
    formatChatTime,
    chatAvatarName, chatAvatarColorKey, chatAvatarUrl,
    userAvatarUrl, senderAvatarUrl, currentUserAvatarUrl,
    chatAvatarPreview, userAvatarPreview, senderAvatarPreview, currentUserAvatarPreview,
  };
}
