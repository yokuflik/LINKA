// Forwarding a message to one or more other conversations (WhatsApp-style).
// Global `useForward(ctx)` factory (no build step, loaded via <script src>).
//
// Frontend-only (ADR 0020): a forward is just a fresh `send_message` frame
// carrying the same content / media into each picked chat. For media we reuse
// the original object by pulling its storage key out of the presigned
// `media_url` and passing it as `media.key` - the backend re-refs the existing
// media_blob (ADR 0010), no re-upload. No "Forwarded" label (needs a backend
// field).
//
// Needs from ctx (call-time): apiFetch, friendlyError, showToast, log, logError,
// chats, chatDisplayName, privateChatOtherUserId, privateChatTitles, userById,
// selectChat, loadChats, sendRaw, wsIsOpen, activeChatId, messages, currentUser,
// bumpChatPreview, previewText, scrollMessagesToBottom, isPinnedToBottom.
function useForward(ctx) {
  const { ref, computed, nextTick } = Vue;

  const showForwardModal = ref(false);
  const forwardSource = ref(null);       // the message being forwarded, or null
  const forwardBusy = ref(false);
  const forwardError = ref('');

  // Picked targets: existing chats by id, plus searched people (not yet in a
  // chat) by user id -> UserOut.
  const forwardSelectedChatIds = ref(new Set());
  const forwardSelectedUsers = ref(new Map());

  // --- People search (exact username / phone, same routing as New chat) -----
  const forwardQuery = ref('');
  const forwardUserBusy = ref(false);
  const forwardUserResult = ref(null);   // a UserOut, or null
  let forwardUserSeq = 0;
  let forwardUserTimer = null;
  const FORWARD_SEARCH_DEBOUNCE_MS = 1500;

  // Pull the media object key out of a presigned GET URL so a forward can
  // re-reference the same stored bytes. Key shape is `{h2}/{kind}/{id}{ext}`
  // (2-hex leading prefix); drop a leading bucket segment for path-style
  // endpoints (dev MinIO). Returns null for a blob:/local/absent URL.
  function mediaKeyFromUrl(url) {
    if (!url || !/^https?:/i.test(url)) return null;
    try {
      const segs = new URL(url).pathname.replace(/^\/+/, '').split('/').filter(Boolean);
      if (segs.length >= 4 && !/^[0-9a-f]{2}$/i.test(segs[0])) segs.shift();
      return segs.length ? segs.join('/') : null;
    } catch (_) {
      return null;
    }
  }

  // A message is forwardable if it's a real, non-deleted, non-system message;
  // for a media message we must also be able to recover its storage key.
  function canForwardMessage(m) {
    if (!m || m.id == null || m.deleted_at != null || m.type === 6) return false;
    if (m.type === 1) return true;
    return !!mediaKeyFromUrl(m.media_url_remote || m.media_url);
  }

  function resetForwardPicker() {
    if (forwardUserTimer) { clearTimeout(forwardUserTimer); forwardUserTimer = null; }
    forwardQuery.value = '';
    forwardUserBusy.value = false;
    forwardUserResult.value = null;
    forwardError.value = '';
    forwardSelectedChatIds.value = new Set();
    forwardSelectedUsers.value = new Map();
  }

  function openForwardModal(message) {
    if (!canForwardMessage(message)) {
      ctx.showToast("This message can't be forwarded.");
      return;
    }
    resetForwardPicker();
    forwardSource.value = message;
    showForwardModal.value = true;
    ctx.closeMessageContextMenu();
  }

  function closeForwardModal() {
    showForwardModal.value = false;
    forwardSource.value = null;
  }

  // All of the user's conversations (groups + private peers), in sidebar
  // order; filtered by a client-side substring match on the display name and,
  // for a private chat, the peer's phone digits. Empty query -> everything.
  const forwardChatMatches = computed(() => {
    const q = forwardQuery.value.trim().toLowerCase().replace(/^@/, '');
    const qDigits = q.replace(/\D/g, '');
    return ctx.chats.value.filter((item) => {
      const chat = item.chat;
      if (!q) return true;
      if ((ctx.chatDisplayName(chat) || '').toLowerCase().includes(q)) return true;
      if (chat.is_group) return false;
      const otherId = ctx.privateChatOtherUserId.value[chat.id];
      const phone = otherId && ctx.userById.value[otherId] && ctx.userById.value[otherId].phone_number;
      const titlePhone = ctx.privateChatTitles.value[chat.id];
      const hay = `${phone || ''} ${titlePhone || ''}`.replace(/\D/g, '');
      return qDigits.length >= 2 && hay.includes(qDigits);
    });
  });

  const forwardSelectedCount = computed(
    () => forwardSelectedChatIds.value.size + forwardSelectedUsers.value.size
  );

  function isChatSelected(chatId) {
    return forwardSelectedChatIds.value.has(chatId);
  }
  function isUserSelected(userId) {
    return forwardSelectedUsers.value.has(userId);
  }

  function toggleForwardChat(chatId) {
    const next = new Set(forwardSelectedChatIds.value);
    next.has(chatId) ? next.delete(chatId) : next.add(chatId);
    forwardSelectedChatIds.value = next;
  }

  // Toggle a searched person. If a private chat with them already exists, treat
  // it as a chat selection instead so we don't create a duplicate.
  function toggleForwardUser(user) {
    const existing = ctx.chats.value.find(
      (c) => !c.chat.is_group && ctx.privateChatOtherUserId.value[c.chat.id] === user.id
    );
    if (existing) { toggleForwardChat(existing.chat.id); return; }
    const next = new Map(forwardSelectedUsers.value);
    next.has(user.id) ? next.delete(user.id) : next.set(user.id, user);
    forwardSelectedUsers.value = next;
  }

  function onForwardSearchInput() {
    forwardError.value = '';
    forwardUserResult.value = null;
    if (forwardUserTimer) clearTimeout(forwardUserTimer);
    forwardUserSeq++;
    forwardUserBusy.value = false;
    if (!forwardQuery.value.trim()) return;
    forwardUserTimer = setTimeout(() => {
      forwardUserTimer = null;
      runForwardSearch();
    }, FORWARD_SEARCH_DEBOUNCE_MS);
  }

  async function runForwardSearch() {
    if (forwardUserTimer) { clearTimeout(forwardUserTimer); forwardUserTimer = null; }
    const raw = forwardQuery.value.trim();
    forwardUserResult.value = null;
    if (!raw) return;
    const seq = ++forwardUserSeq;
    forwardUserBusy.value = true;
    try {
      const looksLikePhone = /^\+?\d[\d\s-]*$/.test(raw);
      const path = looksLikePhone
        ? `/users/by-phone?phone_number=${encodeURIComponent(raw.replace(/[\s-]/g, ''))}`
        : `/users/by-username?username=${encodeURIComponent(raw.replace(/^@/, ''))}`;
      const user = await ctx.apiFetch(path);
      if (seq !== forwardUserSeq) return;
      ctx.userById.value[user.id] = user;
      forwardUserResult.value = user;
    } catch (err) {
      if (seq !== forwardUserSeq) return;
      forwardError.value = err.status === 404
        ? 'No user found — check the exact username or phone number.'
        : ctx.friendlyError(err, "We couldn't run that search. Please try again.");
    } finally {
      if (seq === forwardUserSeq) forwardUserBusy.value = false;
    }
  }

  // Build the send_message frame that re-sends `m` into `chatId`.
  function buildForwardFrame(m, chatId) {
    const frame = {
      type: 'send_message',
      chat_id: chatId,
      client_message_id: crypto.randomUUID(),
      message_type: m.type || 1,
    };
    if (m.type === 1) {
      frame.content = m.content || '';
      return frame;
    }
    const key = mediaKeyFromUrl(m.media_url_remote || m.media_url);
    frame.media = { key };
    if (m.media_name) frame.media.name = m.media_name;
    if (m.media_duration_seconds != null) frame.media.duration_seconds = m.media_duration_seconds;
    if (m.media_blur_hash) frame.media.blur_hash = m.media_blur_hash;
    if (m.content) frame.content = m.content;
    return frame;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function confirmForward() {
    const m = forwardSource.value;
    if (!m || forwardBusy.value || forwardSelectedCount.value === 0) return;
    if (!ctx.wsIsOpen()) {
      forwardError.value = "You're offline — reconnect to forward this message.";
      return;
    }
    forwardBusy.value = true;
    forwardError.value = '';
    try {
      const targetChatIds = [...forwardSelectedChatIds.value];

      // Create a private chat for each picked person first, then treat it like
      // any other target.
      for (const user of forwardSelectedUsers.value.values()) {
        try {
          const chat = await ctx.apiFetch('/chats/private', {
            method: 'POST',
            body: JSON.stringify({ other_user_id: user.id }),
          });
          ctx.privateChatTitles.value[chat.id] = user.username || user.phone_number;
          ctx.privateChatOtherUserId.value[chat.id] = user.id;
          targetChatIds.push(chat.id);
        } catch (err) {
          ctx.logError('forward: could not create chat with', user.id, err);
        }
      }
      if (!targetChatIds.length) throw new Error('no deliverable targets');
      await ctx.loadChats();

      // Pace the sends so a fan-out to many chats stays under the WS
      // send limiter (3/1s). The active chat gets an optimistic bubble;
      // other chats reconcile silently via their new_message echo.
      for (let i = 0; i < targetChatIds.length; i++) {
        const chatId = targetChatIds[i];
        const frame = buildForwardFrame(m, chatId);
        if (chatId === ctx.activeChatId.value) {
          ctx.messages.value.push({
            id: null, client_message_id: frame.client_message_id, chat_id: chatId,
            sender_id: ctx.currentUser.value.id, type: frame.message_type,
            content: frame.content || '', created_at: new Date().toISOString(),
            is_edited: false, edited_at: null, status: 'SENT',
            reply_to_message_id: null, pending: true, send_failed: false,
            media_url: m.media_url_remote || m.media_url || null,
            media_name: m.media_name || null,
            media_blur_hash: m.media_blur_hash || null,
            media_duration_seconds: m.media_duration_seconds || null,
          });
          nextTick(ctx.scrollMessagesToBottom);
        }
        ctx.sendRaw(frame);
        if (i < targetChatIds.length - 1) await sleep(500);
      }

      const n = targetChatIds.length;
      closeForwardModal();
      ctx.showToast(n === 1 ? 'Message forwarded' : `Forwarded to ${n} chats`);
      if (n === 1 && targetChatIds[0] !== ctx.activeChatId.value) {
        await ctx.selectChat(targetChatIds[0]);
      }
    } catch (err) {
      forwardError.value = ctx.friendlyError(err, "We couldn't forward that message. Please try again.");
    } finally {
      forwardBusy.value = false;
    }
  }

  return {
    showForwardModal, forwardSource, forwardBusy, forwardError,
    forwardQuery, forwardUserBusy, forwardUserResult,
    forwardChatMatches, forwardSelectedCount,
    canForwardMessage, openForwardModal, closeForwardModal,
    onForwardSearchInput, runForwardSearch,
    isChatSelected, isUserSelected, toggleForwardChat, toggleForwardUser,
    confirmForward,
  };
}
