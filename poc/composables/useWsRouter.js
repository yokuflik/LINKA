// WebSocket message router + the unread-count badge state it drives.
// Global `useWsRouter(ctx)` factory. handleWsMessage is the single entry point
// for every frame the socket receives (useWebsocket's ws.onmessage calls
// ctx.handleWsMessage) - it dispatches acks/errors, presence replies, and the
// chat-channel fan-out events (new_message, typing, edits, receipts, membership).
//
// It reaches into a lot of still-inline state via ctx.* (call-time, so merge
// order doesn't matter): messages, activeChatId, isPinnedToBottom,
// scrollMessagesToBottom, probeMediaOrientation, bumpChatPreview, previewText,
// groupChatMembers, resolveChatMemberPhones, userById, updateChatPreviewIfLast,
// detailsModalMessage, loadMessageReceipts, refreshMessageStatuses, showToast,
// loadChats, showMembersModal, chats. From other composables: presenceByUserId,
// clearUserTyping, noteUserTyping, sendReceipt, currentUser, log, logError.
function useWsRouter(ctx) {
  const { ref, nextTick } = Vue;

  // Unread count badge (WhatsApp-style number on each sidebar chat).
  // Seeded from the server on every loadChats() call - GET /chats returns each
  // chat's real unread_count (chat_service.get_chat_list), so a fresh
  // login/reload shows the true count, not just what arrived this session.
  // Then kept current live: incremented here for a real message (not a system
  // message, not our own) arriving for a chat that isn't the active one, and
  // reset to 0 the moment that chat is opened (see selectChat / clearUnreadCount).
  const unreadCountByChatId = ref({});

  function bumpUnreadCount(chatId) {
    unreadCountByChatId.value = {
      ...unreadCountByChatId.value,
      [chatId]: (unreadCountByChatId.value[chatId] || 0) + 1,
    };
  }

  function clearUnreadCount(chatId) {
    if (!unreadCountByChatId.value[chatId]) return;
    const next = { ...unreadCountByChatId.value };
    delete next[chatId];
    unreadCountByChatId.value = next;
  }

  function handleWsMessage(msg) {
    const {
      log, logError, currentUser, presenceByUserId,
      clearUserTyping, noteUserTyping, sendReceipt,
    } = ctx;

    // Acks/errors for actions *you* took use "type"; events fanned out from
    // the server (yours or anyone else's) use "event".
    if (msg.type === 'error') { logError('server error:', msg.code, '-', msg.message); return; }
    if (msg.type === 'ack') { log('ack:', msg.for, msg); return; }
    if (msg.type === 'heartbeat_ack') { return; }

    // The send worker couldn't persist a queued message (bad media, no longer
    // a participant, too long). Flag the optimistic bubble so the user sees it
    // failed rather than hanging on 🕓 forever.
    if (msg.event === 'message_failed') {
      const m = ctx.messages.value.find((x) => x.client_message_id === msg.client_message_id);
      if (m) { m.pending = false; m.send_failed = true; }
      logError('message failed to send:', msg.reason);
      return;
    }

    // A duplicate stream entry for a client_message_id already written - just
    // reconcile the optimistic bubble, no new bubble to render.
    if (msg.event === 'message_already_sent') {
      const m = ctx.messages.value.find((x) => x.client_message_id === msg.client_message_id);
      if (m) { m.id = msg.message_id; m.pending = false; m.send_failed = false; }
      return;
    }

    if (msg.type === 'presence_status' || msg.type === 'presence_update') {
      // "Last seen" is intentionally not tracked here - only the current
      // online/offline status (see presenceLabelFor).
      presenceByUserId.value[msg.user_id] = { status: msg.status };
      return;
    }

    if (msg.event === 'new_message') {
      // A message from someone means they're no longer typing - drop their
      // typing indicator now instead of leaving it to expire 5s later.
      if (msg.sender_id != null) clearUserTyping(msg.chat_id, msg.sender_id);
      // Reconcile an optimistic bubble (our own send) instead of adding a
      // duplicate. Match by client_message_id, which the server echoes on the
      // event for exactly this purpose (never persisted on the message).
      const optimistic = msg.client_message_id
        ? ctx.messages.value.find((x) => x.client_message_id === msg.client_message_id)
        : null;
      if (optimistic) {
        optimistic.id = msg.message_id;
        optimistic.created_at = msg.created_at;
        optimistic.status = msg.status;
        optimistic.type = msg.type;
        optimistic.content = msg.content;
        optimistic.reply_to_message_id = msg.reply_to_message_id;
        optimistic.media_url = msg.media_url;
        optimistic.media_mime = msg.media_mime;
        optimistic.media_size = msg.media_size;
        optimistic.media_name = msg.media_name;
        optimistic.media_duration_seconds = msg.media_duration_seconds;
        optimistic.pending = false;
        optimistic.send_failed = false;
      } else if (msg.chat_id === ctx.activeChatId.value) {
        // Decide before pushing: was the user already at the bottom?
        const wasPinned = ctx.isPinnedToBottom();
        ctx.messages.value.push({
          id: msg.message_id, chat_id: msg.chat_id, sender_id: msg.sender_id,
          type: msg.type, content: msg.content, created_at: msg.created_at, is_edited: false, edited_at: null,
          status: msg.status, reply_to_message_id: msg.reply_to_message_id,
          media_url: msg.media_url, media_mime: msg.media_mime, media_size: msg.media_size,
          media_name: msg.media_name, media_duration_seconds: msg.media_duration_seconds,
        });
        if (msg.type === 2 && msg.media_url) ctx.probeMediaOrientation(msg.media_url, 'image');
        else if (msg.type === 3 && msg.media_url) ctx.probeMediaOrientation(msg.media_url, 'video');
        // Always follow your own message down; for someone else's, only if
        // the user was already reading the latest (not scrolled up).
        if (msg.sender_id === currentUser.value.id || wasPinned) {
          nextTick(ctx.scrollMessagesToBottom);
        }
      }
      // System messages ("X joined the group", or a private "role_changed"
      // notice - see shouldShowSystemMessage) must never become the sidebar
      // preview: unlike the message stream, the chat list has no per-viewer
      // filtering, so a "role_changed" preview would leak to every
      // participant, not just the actor/target it's meant for.
      if (msg.sender_id != null) {
        ctx.bumpChatPreview(msg.chat_id, msg.message_id, msg.created_at, ctx.previewText(msg.content, msg.type), msg.status);
      }

      if (msg.sender_id == null) {
        // A system message (e.g. "X joined the group") means this chat's
        // membership just changed - re-pull /chats/{id}/members so the
        // header's participant row and the members modal update live for
        // everyone with this chat loaded, not just whoever triggered it.
        // Only bother for chats whose members were already fetched once.
        if (ctx.groupChatMembers.value[msg.chat_id]) ctx.resolveChatMemberPhones(msg.chat_id);
      }

      if (msg.sender_id !== currentUser.value.id) {
        // The message just reached this device over a live connection -
        // that's "delivered", regardless of which chat is open right now.
        sendReceipt('mark_delivered', msg.chat_id, msg.message_id);
        // "Read" only for the chat actually on screen - opening a chat
        // separately catches up on anything sent while it wasn't (see selectChat).
        if (msg.chat_id === ctx.activeChatId.value) sendReceipt('mark_read', msg.chat_id, msg.message_id);
      }

      // Unread badge: only real messages from someone else, and only for a
      // chat that isn't the one currently open.
      if (msg.sender_id != null && msg.sender_id !== currentUser.value.id && msg.chat_id !== ctx.activeChatId.value) {
        bumpUnreadCount(msg.chat_id);
      }
      return;
    }

    if (msg.event === 'typing') {
      // Never our own echo back (the server fans this out to the whole chat,
      // sender included, same as new_message).
      if (msg.user_id !== currentUser.value.id) {
        noteUserTyping(msg.chat_id, msg.user_id, msg.kind);
        // The sidebar shows a name (not just a count) for a lone typer in any
        // chat, including one never opened this session - userById only gets
        // populated by resolveChatMemberPhones, which normally only runs on
        // selectChat/openMembersModal. Lazily resolve here too.
        if (!ctx.userById.value[msg.user_id]) ctx.resolveChatMemberPhones(msg.chat_id);
      }
      return;
    }

    if (msg.event === 'message_edited') {
      const m = ctx.messages.value.find((x) => x.id === msg.message_id);
      if (m) { m.content = msg.content; m.is_edited = true; m.edited_at = msg.edited_at || new Date().toISOString(); }
      ctx.updateChatPreviewIfLast(msg.chat_id, msg.message_id, msg.content);
      return;
    }

    if (msg.event === 'message_deleted') {
      // Soft delete: keep the bubble in place, just flag it so it re-renders
      // as "This message was deleted" (WhatsApp-style tombstone).
      const m = ctx.messages.value.find((x) => x.id === msg.message_id);
      if (m) {
        m.deleted_at = new Date().toISOString();
        m.content = null;
        m.media_url = null;
      }
      // Sidebar preview: if this was the chat's last message, show a
      // "Message deleted" placeholder under the chat name instead of the
      // now-gone text.
      ctx.updateChatPreviewIfLast(msg.chat_id, msg.message_id, '🚫 Message deleted');
      return;
    }

    if (msg.event === 'delivery_receipt' || msg.event === 'read_receipt' || msg.event === 'played_receipt') {
      // If the message-info popup is open on this chat, re-pull it so the
      // per-person "read at / played at" list stays current live.
      if (ctx.detailsModalMessage.value && msg.chat_id === ctx.activeChatId.value) {
        ctx.loadMessageReceipts(msg.chat_id, ctx.detailsModalMessage.value.id);
      }
      if (msg.event === 'played_receipt') return;
      // Which of *my* sent messages this actually changed the tick on depends
      // on every other participant's own watermark, not just this one event -
      // simplest correct move is to ask the server, which already recomputed
      // it, rather than guess client-side.
      if (msg.chat_id === ctx.activeChatId.value) ctx.refreshMessageStatuses(msg.chat_id);

      // Cross-tab/cross-device unread sync: a read_receipt's user_id is
      // whoever just marked the chat read - if that's ME, one of my own other
      // tabs/devices just read this chat, so this tab's badge for it should
      // clear too, even though it isn't the active chat here.
      if (msg.event === 'read_receipt' && msg.user_id === currentUser.value.id) {
        clearUnreadCount(msg.chat_id);
      }
      return;
    }

    if (msg.event === 'added_to_chat') {
      // Fired for a chat that didn't exist when this WebSocket connected - the
      // server has already brought this connection's live subscription up to
      // date; refreshing here is just what makes the new chat show up.
      log('added to chat', msg.chat_id);
      ctx.showToast('New chat');
      ctx.loadChats();
      return;
    }

    if (msg.event === 'removed_from_chat') {
      // Mirror of added_to_chat: fired when someone else removes this user
      // from a group (or this user leaves from another device).
      log('removed from chat', msg.chat_id);
      ctx.chats.value = ctx.chats.value.filter((c) => c.chat.id !== msg.chat_id);
      if (ctx.activeChatId.value === msg.chat_id) {
        ctx.activeChatId.value = null;
        ctx.messages.value = [];
        ctx.showMembersModal.value = false;
      }
      // Only toast when someone else did the removing - self-initiated leaves
      // already updated this device instantly.
      if (msg.actor_id !== currentUser.value.id) {
        const name = msg.chat_title || 'a group';
        ctx.showToast(`You were removed from "${name}"`);
      }
      return;
    }

    log('unhandled WS message shape:', msg);
  }

  return {
    unreadCountByChatId, bumpUnreadCount, clearUnreadCount,
    handleWsMessage,
  };
}
