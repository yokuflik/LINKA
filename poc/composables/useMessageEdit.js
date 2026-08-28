// Editing and deleting a message. Only the sender may edit/delete, and the
// backend re-checks ownership regardless. The actual edit-send frame is
// fired from useMessageSend.sendMessage (the composer path); this module
// owns the edit/delete state and the optimistic local update on delete.
// Global `useMessageEdit(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: log, logError, wsStatus, wsIsOpen, sendRaw, activeChatId,
// currentUser, messageInput, updateChatPreviewIfLast, closeMessageContextMenu,
// and (from useMessageSend) replyingToMessage - read call-time via ctx.
function useMessageEdit(ctx) {
  const { ref } = Vue;

  // Deleting a message. Only the sender may delete their own message; the
  // backend re-checks ownership regardless. Soft delete server-side - the
  // bubble stays in place and re-renders as "This message was deleted"
  // (see the message_deleted handler in useWsRouter).
  function canDeleteMessage(m) {
    return !!m && m.sender_id != null && m.deleted_at == null
      && ctx.currentUser.value && m.sender_id === ctx.currentUser.value.id;
  }

  function deleteMessage(message) {
    ctx.closeMessageContextMenu();
    if (!canDeleteMessage(message) || !ctx.activeChatId.value) return;
    if (!ctx.wsIsOpen()) {
      ctx.logError('cannot delete - WebSocket is not connected (status:', ctx.wsStatus.value, ')');
      return;
    }
    const payload = {
      type: 'delete_message',
      chat_id: ctx.activeChatId.value,
      message_id: message.id,
    };
    ctx.log('WS →', payload);
    ctx.sendRaw(payload);
    // Update this device's own view right away rather than waiting for the
    // message_deleted echo: mark the bubble as a tombstone and, if it was the
    // chat's last message, show the "Message deleted" sidebar placeholder.
    message.deleted_at = new Date().toISOString();
    message.content = null;
    message.media_url = null;
    ctx.updateChatPreviewIfLast(ctx.activeChatId.value, message.id, '🚫 Message deleted');
  }

  // ---------------------------------------------------------------
  // Editing a message. Only the sender may edit, and only a plain text
  // message (type 1) that isn't deleted; the backend re-checks ownership.
  // While editing, the composer is prefilled and shows an "Editing" strip;
  // pressing Send fires an edit_message WS frame instead of send_message
  // (see sendMessage in useMessageSend). The message_edited echo (useWsRouter)
  // sets is_edited / content for other devices; we also update optimistically.
  // ---------------------------------------------------------------
  const editingMessage = ref(null); // the message object being edited, or null

  function canEditMessage(m) {
    return !!m && m.type === 1 && m.sender_id != null && m.deleted_at == null
      && ctx.currentUser.value && m.sender_id === ctx.currentUser.value.id;
  }

  function startEditMessage(message) {
    ctx.closeMessageContextMenu();
    if (!canEditMessage(message)) return;
    ctx.replyingToMessage.value = null; // edit and reply-compose are mutually exclusive
    editingMessage.value = message;
    ctx.messageInput.value = message.content || '';
  }

  function cancelEdit() {
    editingMessage.value = null;
    ctx.messageInput.value = '';
  }

  return {
    canDeleteMessage, deleteMessage,
    canEditMessage, editingMessage, startEditMessage, cancelEdit,
  };
}
