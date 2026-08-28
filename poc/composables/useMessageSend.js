// Sending a plain text message over the WebSocket (per the real /ws
// protocol), the optimistic bubble, and reply-to-message compose state.
// Global `useMessageSend(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: log, logError, wsStatus, wsIsOpen, sendRaw, activeChatId,
// messages, messageInput, currentUser, updateChatPreviewIfLast,
// scrollMessagesToBottom, replySenderLabel, and (from useMessageEdit)
// editingMessage / cancelEdit - read call-time via ctx.
function useMessageSend(ctx) {
  const { ref, nextTick } = Vue;

  // ---------------------------------------------------------------
  // Reply-to-message (WhatsApp-style) - the backend already fully supports
  // reply_to_message_id end to end (persisted, returned in history, and on
  // the live new_message event); this is just the compose-time UI for it.
  // ---------------------------------------------------------------
  const replyingToMessage = ref(null); // the message object being replied to, or null

  function startReplyTo(message) {
    if (ctx.editingMessage.value) ctx.cancelEdit(); // edit and reply-compose are mutually exclusive
    replyingToMessage.value = message;
    ctx.closeMessageContextMenu();
  }

  function cancelReply() {
    replyingToMessage.value = null;
  }

  // Looks up the original message a reply (m) points to, purely from what's
  // already loaded in this chat's message list - no extra fetch for a
  // message that scrolled out of the loaded page/history. Falls back to a
  // generic placeholder (rather than rendering nothing) so a
  // reply-to-older-history message still visibly reads as a reply.
  function quotedPreviewFor(m) {
    if (m.reply_to_message_id == null) return null;
    const original = ctx.messages.value.find((x) => x.id === m.reply_to_message_id);
    if (!original) return { sender: '', snippet: 'Original message' };
    return {
      sender: original.sender_id != null ? ctx.replySenderLabel(original.sender_id) : 'System',
      snippet: (original.content || '').slice(0, 80),
    };
  }

  // ---------------------------------------------------------------
  // Sending a message (over the WebSocket, per the real /ws protocol)
  // ---------------------------------------------------------------
  function sendMessage() {
    const content = ctx.messageInput.value.trim();
    if (!content || !ctx.activeChatId.value) return;
    if (!ctx.wsIsOpen()) {
      ctx.logError('cannot send - WebSocket is not connected (status:', ctx.wsStatus.value, ')');
      return;
    }

    // Editing an existing message rather than sending a new one.
    if (ctx.editingMessage.value) {
      const target = ctx.editingMessage.value;
      if (content !== (target.content || '')) {
        const editPayload = {
          type: 'edit_message',
          chat_id: ctx.activeChatId.value,
          message_id: target.id,
          content,
        };
        ctx.log('WS →', editPayload);
        ctx.sendRaw(editPayload);
        // Reflect on this device right away; the message_edited echo is a no-op then.
        target.content = content;
        target.is_edited = true;
        target.edited_at = new Date().toISOString();
        ctx.updateChatPreviewIfLast(ctx.activeChatId.value, target.id, content);
      }
      ctx.editingMessage.value = null;
      ctx.messageInput.value = '';
      return;
    }

    const clientMessageId = crypto.randomUUID();
    const payload = {
      type: 'send_message',
      chat_id: ctx.activeChatId.value,
      client_message_id: clientMessageId,
      content,
      message_type: 1,
    };
    if (replyingToMessage.value) payload.reply_to_message_id = replyingToMessage.value.id;

    // The send path is async server-side now (queued, then persisted + fanned
    // out by a worker). Render the bubble immediately keyed by
    // client_message_id; useWsRouter reconciles it when the new_message echo
    // arrives, or marks it failed on message_failed.
    if (ctx.activeChatId.value === payload.chat_id) {
      ctx.messages.value.push({
        id: null, client_message_id: clientMessageId, chat_id: payload.chat_id,
        sender_id: ctx.currentUser.value.id, type: 1, content,
        created_at: new Date().toISOString(), is_edited: false, edited_at: null,
        status: 'SENT', reply_to_message_id: payload.reply_to_message_id || null,
        pending: true, send_failed: false,
      });
    }

    ctx.log('WS →', payload);
    ctx.sendRaw(payload);
    ctx.messageInput.value = '';
    replyingToMessage.value = null;
    // Jump to the bottom right away so the composer stays pinned to the
    // newest message; the WS echo will land there a moment later.
    nextTick(ctx.scrollMessagesToBottom);
  }

  return {
    sendMessage,
    replyingToMessage, startReplyTo, cancelReply, quotedPreviewFor,
  };
}
