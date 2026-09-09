// Chat domain — LIST LOAD + PIN/MUTE MUTATIONS.
//
// Loads GET /chats, seeds the unread badge from the server count, and owns the
// optimistic pin/mute writes (flip locally, PUT/DELETE, roll back on failure).
// Multi-device sync for both is handled in useWsRouter via personal-channel
// echoes; this module only does the acting-tab half.
//
// Merge order: after useChatMembers (uses resolvePrivateChatTitle /
// resolveChatMemberPhones), before useChatOpen / useChatMenu.
//
// Needs from ctx (call-time): apiFetch, log, logError, chats, chatsError,
// activeChatId, sortChats, groupChatMembers, presetExpiryIso,
// resolvePrivateChatTitle, resolveChatMemberPhones,
// unreadCountByChatId, markAllChatsDelivered.
//
// Global `useChatList(ctx)` factory.
function useChatList(ctx) {
  async function loadChats() {
    ctx.chatsError.value = '';
    try {
      ctx.chats.value = await ctx.apiFetch('/chats?limit=50');
      ctx.sortChats();
      ctx.log('loaded', ctx.chats.value.length, 'chat(s)');
      await Promise.all(
        ctx.chats.value.filter((c) => !c.chat.is_group).map((c) => ctx.resolvePrivateChatTitle(c.chat.id, { force: true }))
      );
      // Re-pull members for any group whose list was already fetched, so a
      // participant's changed name/photo propagates to the sidebar and to the
      // open message pane (no server push exists for a profile edit).
      await Promise.all(
        Object.keys(ctx.groupChatMembers.value).map((id) => ctx.resolveChatMemberPhones(id))
      );

      // Seed the unread badge from the server's real count - replaced
      // wholesale, not merged (the server value is always at least as fresh
      // as anything counted live). The active chat is always forced to
      // 0/absent - it's being read right now.
      const nextUnread = {};
      for (const item of ctx.chats.value) {
        if (item.unread_count && item.chat.id !== ctx.activeChatId.value) {
          nextUnread[item.chat.id] = item.unread_count;
        }
      }
      ctx.unreadCountByChatId.value = nextUnread;

      // E2E (ADR 0034): GET /chats sends the server's literal
      // "🔒 Encrypted message" placeholder for an encrypted last message, plus
      // the opaque payload to decrypt it in `chat.last_message_enc`. Turn it
      // into a real preview line here (falling back to a cached copy, then to
      // the placeholder).
      if (ctx.decryptMessage) await decryptEncryptedPreviews();

      // Covers the race with connectWebSocket()'s onopen handler, which also
      // calls this - whichever finishes last has both ready.
      ctx.markAllChatsDelivered();
    } catch (err) {
      ctx.chatsError.value = ctx.friendlyError(err, "We couldn't load your chats. Please try again in a moment.");
      if (ctx.showErrorToast) ctx.showErrorToast(ctx.chatsError.value);
    }
  }

  // The server-stored placeholder for an encrypted last message (mirrors
  // modules/messaging/crud.ENCRYPTED_MESSAGE_PREVIEW).
  const ENCRYPTED_PREVIEW = '\u{1F512} Encrypted message';

  async function decryptEncryptedPreviews() {
    await Promise.all(ctx.chats.value.map(async (item) => {
      const chat = item.chat;
      if (chat.last_message_preview !== ENCRYPTED_PREVIEW || chat.last_message_id == null) return;

      // Preferred: the ciphertext + header the server denormalised (ADR 0034).
      const enc = chat.last_message_enc;
      if (enc && enc.ct && enc.header) {
        const text = await ctx.decryptMessage({
          is_encrypted: true, content: enc.ct, enc_header: enc.header,
        });
        if (text && text !== ENC_UNAVAILABLE) { chat.last_message_preview = ctx.previewText(text, 1); return; }
      }

      // Fallback: a copy of that message already in the local cache.
      const cached = ctx.loadChatMessages ? ctx.loadChatMessages(chat.id) : null;
      const row = cached && cached.find((m) => m.id === chat.last_message_id);
      if (row && (row._e2eDecrypted || !row.is_encrypted) && row.content) {
        chat.last_message_preview = ctx.previewText(row.content, row.type);
        return;
      }
      if (row && row.is_encrypted && (row.enc_ct || row.content) && row.enc_header) {
        const text = await ctx.decryptMessage({
          is_encrypted: true, content: row.enc_ct || row.content, enc_header: row.enc_header,
        });
        if (text && text !== ENC_UNAVAILABLE) { chat.last_message_preview = ctx.previewText(text, row.type); return; }
      }
      // Can't decrypt on this device - leave the server's placeholder as-is.
    }));
  }
  // Mirrors useE2E's DECRYPT_PLACEHOLDER - don't overwrite the tidy server
  // placeholder with the "can't be shown on this device" string.
  const ENC_UNAVAILABLE = "🔒 This message can't be shown on this device.";

  // Mute a chat for the current user until an absolute ISO timestamp.
  // Optimistic: set muted_until locally, roll back on failure. Multi-device
  // sync via the chat_mute_changed personal-channel echo (see useWsRouter).
  async function muteChatUntil(chatId, iso) {
    const item = ctx.chats.value.find((c) => c.chat.id === chatId);
    if (!item) return;
    const prev = item.muted_until;
    item.muted_until = iso;
    try {
      await ctx.apiFetch(`/chats/${chatId}/mute`, {
        method: 'PUT',
        body: JSON.stringify({ muted_until: iso }),
      });
    } catch (err) {
      ctx.logError('failed to mute', chatId, err);
      item.muted_until = prev;
    }
  }

  function muteChat(chatId, presetKey) {
    return muteChatUntil(chatId, ctx.presetExpiryIso(presetKey));
  }

  async function unmuteChat(chatId) {
    const item = ctx.chats.value.find((c) => c.chat.id === chatId);
    if (!item) return;
    const prev = item.muted_until;
    item.muted_until = null;
    try {
      await ctx.apiFetch(`/chats/${chatId}/mute`, { method: 'DELETE' });
    } catch (err) {
      ctx.logError('failed to unmute', chatId, err);
      item.muted_until = prev;
    }
  }

  // Pin/unpin a chat for the current user. Optimistic: flip the flag, re-sort,
  // roll back on failure. No server push - other devices catch up on reload.
  async function togglePinChat(chatId) {
    const item = ctx.chats.value.find((c) => c.chat.id === chatId);
    if (!item) return;
    const next = !item.pinned;
    item.pinned = next;
    ctx.sortChats();
    try {
      await ctx.apiFetch(`/chats/${chatId}/pin`, { method: next ? 'PUT' : 'DELETE' });
    } catch (err) {
      ctx.logError('failed to toggle pin for', chatId, err);
      item.pinned = !next;
      ctx.sortChats();
    }
  }

  return {
    loadChats,
    togglePinChat,
    muteChat, muteChatUntil, unmuteChat,
  };
}
