// Chat domain — OPENING / CLOSING A CHAT + THE MESSAGE PANE.
//
// The heavy part of the old useChats: selectChat's cache-hit and network-fetch
// paths, buffered-live-message merge, scroll / pin-to-bottom helpers, keyset
// "load older" pagination, draft private chats, the focus/reconnect refresh
// hooks, and the write-through message-cache watch.
//
// Merge order: after useChatStore / useChatMembers / useChatList (calls
// resolvePrivateChatTitle, resolveChatMemberPhones through ctx.*).
//
// Needs from ctx (call-time): apiFetch, log, logError, currentUser, chats,
// activeChatId, draftChat, messages, messagesError, messagesConnectionError,
// messagesEl, hasMoreMessages, loadingOlderMessages, MESSAGE_PAGE_SIZE,
// LOAD_OLDER_THRESHOLD, userById,
// resolvePrivateChatTitle, resolveChatMemberPhones,
// loadChatMessages, saveChatMessages (useMessageCache),
// markStalePendingAsFailed (useOutbox),
// clearUnreadCount, unsubscribeFromPresence, subscribeToPresenceForChat,
// subscribeToPresence, sendReceipt, takeBufferedMessages,
// replyingToMessage, editingMessage, cancelEdit, closeMessageContextMenu,
// probeLoadedImageOrientations.
//
// Global `useChatOpen(ctx)` factory.
function useChatOpen(ctx) {
  const { nextTick, watch } = Vue;

  // messagesEl refs the MessageList component instance, which exposes its own
  // scrollable element as `messagesEl`.
  function messagesScrollEl() {
    return ctx.messagesEl.value && ctx.messagesEl.value.messagesEl;
  }

  // Guard against a spurious "load older" fire right after a chat opens. When a
  // freshly-rendered history page is still at scrollTop 0 (before
  // scrollMessagesToBottom lands, and again briefly while lazy media expands
  // the reserved boxes), the MessageList's onScroll reports rowsAbove = 0 <=
  // LOAD_OLDER_THRESHOLD and we'd immediately page in the previous 50 messages,
  // parking the view in the middle of the chat instead of at the bottom. Stays
  // false from selectChat until the initial bottom-scroll has settled.
  let initialScrollSettled = false;

  function markInitialScrollSettled() {
    // Two RAFs + a short timeout: past the requestAnimationFrame(jump) in
    // scrollMessagesToBottom and any synchronous media metadata reflow.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      setTimeout(() => { initialScrollSettled = true; }, 150);
    }));
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
    if (ctx.loadingOlderMessages.value || !ctx.hasMoreMessages.value) return;
    if (!ctx.messages.value.length || !ctx.activeChatId.value) return;
    const chatId = ctx.activeChatId.value;
    const oldestId = ctx.messages.value[0].id;
    const el = messagesScrollEl();
    const prevScrollHeight = el ? el.scrollHeight : 0;
    const prevScrollTop = el ? el.scrollTop : 0;
    ctx.loadingOlderMessages.value = true;
    ctx.olderMessagesRetrying.value = false;
    try {
      // apiFetch retries a dead connection forever (every 3s); keep the top
      // spinner up and flag "retrying" so the user sees it's actively trying.
      const page = await ctx.apiFetch(`/chats/${chatId}/messages?limit=${ctx.MESSAGE_PAGE_SIZE}&before_id=${oldestId}`, {
        onRetry: () => { if (ctx.activeChatId.value === chatId) ctx.olderMessagesRetrying.value = true; },
        retryCancelled: () => ctx.activeChatId.value !== chatId,
      });
      if (ctx.activeChatId.value !== chatId) return; // user switched chats mid-flight
      ctx.olderMessagesRetrying.value = false;
      ctx.hasMoreMessages.value = page.length === ctx.MESSAGE_PAGE_SIZE;
      if (page.length) {
        const older = page.slice().reverse();
        ctx.messages.value = older.concat(ctx.messages.value);
        await nextTick();
        // Keep the user looking at the same message: the newly-prepended block
        // grew scrollHeight by (new - prev); add that to where they were, don't
        // snap to the old/new boundary (which sits near the top of the fresh
        // page and immediately re-triggers onScroll -> paging the whole chat).
        if (el) el.scrollTop = prevScrollTop + (el.scrollHeight - prevScrollHeight);
      }
    } catch (err) {
      ctx.logError('failed to load older messages:', err.message);
    } finally {
      ctx.loadingOlderMessages.value = false;
      ctx.olderMessagesRetrying.value = false;
    }
  }

  // MessageList reports how many message rows are scrolled above the viewport.
  function onMessagesScroll(rowsAboveViewport) {
    if (!initialScrollSettled) return;
    if (rowsAboveViewport <= ctx.LOAD_OLDER_THRESHOLD) loadOlderMessages();
  }

  // Open an uncommitted private chat. No server call - just enough state for
  // the pane, header and presence to render. Promoted to a real chat by the
  // first send (see useMessageSend), discarded by opening any real chat.
  function openDraftChat(otherUser) {
    ctx.unsubscribeFromPresence();
    ctx.activeChatId.value = null;
    ctx.messages.value = [];
    ctx.messagesError.value = '';
    ctx.messagesConnectionError.value = false;
    ctx.messagesLoading.value = false;
    ctx.hasMoreMessages.value = false;
    ctx.replyingToMessage.value = null;
    if (ctx.editingMessage.value) ctx.cancelEdit();
    ctx.closeMessageContextMenu();
    ctx.userById.value[otherUser.id] = otherUser;
    ctx.draftChat.value = { otherUserId: otherUser.id, phone: otherUser.phone_number, user: otherUser };
    // Presence by user id: the subscribe_presence gate is the target's
    // privacy.online setting, not a shared chat - so "everyone" resolves
    // even with no chat row yet ("contacts" won't, by design).
    ctx.subscribeToPresence(otherUser.id);
  }

  function discardDraftChat() {
    if (!ctx.draftChat.value) return;
    ctx.unsubscribeFromPresence();
    ctx.draftChat.value = null;
  }

  // Mobile: the sidebar and the chat pane share the screen one at a time.
  // "Back" from the chat header drops the active chat so the list shows again.
  function closeActiveChat() {
    discardDraftChat();
    ctx.unsubscribeFromPresence();
    ctx.activeChatId.value = null;
    ctx.messages.value = [];
    ctx.replyingToMessage.value = null;
    if (ctx.editingMessage.value) ctx.cancelEdit();
    ctx.closeMessageContextMenu();
  }

  // Background freshness check after a cache-hit render. Fetches the newest
  // page and appends any message ids the cached snapshot is missing (i.e.
  // messages received while this client had no live socket). Preserves the
  // user's scroll position unless they're pinned to the bottom.
  async function revalidateFromCache(chatId) {
    let history;
    try {
      history = await ctx.apiFetch(`/chats/${chatId}/messages?limit=${ctx.MESSAGE_PAGE_SIZE}`, {
        retryCancelled: () => ctx.activeChatId.value !== chatId,
      });
    } catch (err) {
      ctx.logError('cache revalidation failed for', chatId, err.message);
      return;
    }
    if (ctx.activeChatId.value !== chatId || !Array.isArray(history)) return;
    const seen = new Set(ctx.messages.value.map((m) => m.id).filter((id) => id != null));
    const missing = history.slice().reverse().filter((m) => m.id != null && !seen.has(m.id));
    if (!missing.length) return;
    const pinned = isPinnedToBottom();
    ctx.messages.value = ctx.messages.value.concat(missing).sort((a, b) => {
      if (a.id == null) return 1;
      if (b.id == null) return -1;
      return String(a.id).localeCompare(String(b.id));
    });
    ctx.saveChatMessages(chatId, ctx.messages.value);
    await nextTick();
    if (pinned) scrollMessagesToBottom();
    const newestId = ctx.messages.value[ctx.messages.value.length - 1].id;
    if (newestId != null) {
      ctx.sendReceipt('mark_delivered', chatId, newestId);
      markActiveChatReadIfVisible(chatId, newestId);
    }
  }

  async function selectChat(chatId) {
    discardDraftChat();
    initialScrollSettled = false;
    ctx.activeChatId.value = chatId;
    ctx.messages.value = [];
    ctx.messagesError.value = '';
    ctx.messagesConnectionError.value = false;
    ctx.messagesLoading.value = true;
    ctx.hasMoreMessages.value = false;
    ctx.loadingOlderMessages.value = false;
    ctx.olderMessagesRetrying.value = false;
    ctx.clearUnreadCount(chatId);
    ctx.replyingToMessage.value = null;
    if (ctx.editingMessage.value) ctx.cancelEdit();
    ctx.closeMessageContextMenu();
    const item = ctx.chats.value.find((c) => c.chat.id === chatId);
    if (item && !item.chat.is_group) ctx.resolvePrivateChatTitle(chatId, { force: true });
    ctx.resolveChatMemberPhones(chatId);

    // Subscribe-on-demand presence: only ever one active subscription, scoped
    // to whichever private chat is open right now.
    ctx.unsubscribeFromPresence();
    if (item && !item.chat.is_group) ctx.subscribeToPresenceForChat(chatId);

    // Fold in any live messages that arrived for this chat while it wasn't
    // open (useWsRouter buffered them). Dedupe by id, keep chronological
    // order. Snowflake ids are fixed-width numeric strings, so a lexical
    // compare is a time compare.
    function mergeBuffered() {
      const buffered = ctx.takeBufferedMessages ? ctx.takeBufferedMessages(chatId) : [];
      if (!buffered.length) return;
      const seen = new Set(ctx.messages.value.map((m) => m.id).filter((id) => id != null));
      for (const m of buffered) {
        if (m.id != null && !seen.has(m.id)) { ctx.messages.value.push(m); seen.add(m.id); }
      }
      ctx.messages.value.sort((a, b) => {
        if (a.id == null) return 1;
        if (b.id == null) return -1;
        return String(a.id).localeCompare(String(b.id));
      });
    }

    // Lazy cache: if we've visited this chat before, render its stored
    // newest page and skip the network entirely. Live WS events keep the
    // cache fresh while connected; "load older" still fetches from the API.
    const cached = ctx.loadChatMessages(chatId);
    if (cached && cached.length) {
      if (ctx.activeChatId.value !== chatId) return;
      ctx.messagesLoading.value = false;
      ctx.messages.value = cached.slice();
      // A cached bubble still marked pending is a message that was queued in
      // the outbox when the tab closed and never sent - show it as failed
      // (⚠️, retryable) rather than a clock that never resolves.
      if (ctx.markStalePendingAsFailed) ctx.markStalePendingAsFailed();
      if (ctx.activeChatId.value !== chatId) return;
      mergeBuffered();
      ctx.hasMoreMessages.value = cached.length >= ctx.MESSAGE_PAGE_SIZE;
      const item2 = ctx.chats.value.find((c) => c.chat.id === chatId);
      const newestId = ctx.messages.value[ctx.messages.value.length - 1].id;
      if (item2) {
        const last = cached.find((m) => m.id === item2.chat.last_message_id);
        if (last && last.deleted_at) item2.chat.last_message_preview = '🚫 Message deleted';
      }
      ctx.probeLoadedImageOrientations();
      await nextTick();
      scrollMessagesToBottom();
      markInitialScrollSettled();
      ctx.sendReceipt('mark_delivered', chatId, newestId);
      markActiveChatReadIfVisible(chatId, newestId);
      // Revalidate in the background: messages that arrived while this client
      // was disconnected (tab closed, only a push received) never reached the
      // write-through cache, so the snapshot above can be stale. Pull the
      // newest page, merge anything we don't already have, and re-fire the
      // receipts for the true newest id. Silent on failure - the cache render
      // already succeeded.
      revalidateFromCache(chatId);
      return;
    }

    try {
      // apiFetch retries a dead connection forever (every 3s). While it does,
      // flip messagesConnectionError so the pane shows an active "Waiting for
      // connection…" spinner rather than a stuck blank / "No messages here".
      const history = await ctx.apiFetch(`/chats/${chatId}/messages?limit=${ctx.MESSAGE_PAGE_SIZE}`, {
        onRetry: () => { if (ctx.activeChatId.value === chatId) ctx.messagesConnectionError.value = true; },
        retryCancelled: () => ctx.activeChatId.value !== chatId,
      });
      if (ctx.activeChatId.value !== chatId) return;
      ctx.messagesConnectionError.value = false;
      ctx.messagesLoading.value = false;
      // Live messages pushed onto messages.value while this fetch was in
      // flight (activeChatId is set before the await, so new_message for this
      // chat renders live) would be wiped by the history assignment - keep
      // them to fold back in.
      const liveDuringFetch = ctx.messages.value.filter((m) => m.id != null);
      // The API returns newest-first (keyset pagination); the UI wants oldest-first.
      ctx.messages.value = history.slice().reverse();
      ctx.hasMoreMessages.value = history.length === ctx.MESSAGE_PAGE_SIZE;
      // Fold in live messages that landed during / before this fetch - the
      // server's send-worker read-after-write lag means the GET can miss the
      // most recent ones even though we already received them over the socket.
      const seenIds = new Set(ctx.messages.value.map((m) => m.id));
      for (const m of liveDuringFetch) {
        if (!seenIds.has(m.id)) { ctx.messages.value.push(m); seenIds.add(m.id); }
      }
      mergeBuffered();
      ctx.saveChatMessages(chatId, ctx.messages.value);
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
      markInitialScrollSettled();

      // Opening a chat catches up on both receipts in one go, including
      // anything sent while this chat wasn't the active one. Use the merged
      // newest id, not history[0], so buffered live messages count too.
      if (ctx.messages.value.length) {
        const newestId = ctx.messages.value[ctx.messages.value.length - 1].id;
        if (newestId != null) {
          ctx.sendReceipt('mark_delivered', chatId, newestId);
          markActiveChatReadIfVisible(chatId, newestId);
        }
      }
    } catch (err) {
      if (ctx.activeChatId.value !== chatId) return;
      // A real API error (4xx/5xx - network failures retry inside apiFetch and
      // never land here). Show the "waiting for connection" state in the pane
      // instead of leaking a raw fetch error into the composer's attach-error
      // banner. A reconnect + tab focus re-runs selectChat via the WS router,
      // so this is self-healing.
      ctx.messagesConnectionError.value = true;
      ctx.messagesLoading.value = false;
      ctx.logError('failed to load chat history for', chatId, err.message);
    }
  }

  // Called from useWebsocket's ws.onopen: if the open chat never loaded its
  // history (server was down / offline when it was selected), re-run selectChat
  // now that the socket is back so it fills itself in without a manual re-tap.
  function reloadActiveChatIfUnloaded() {
    if (ctx.activeChatId.value && ctx.messagesConnectionError.value && !ctx.messages.value.length) {
      selectChat(ctx.activeChatId.value);
    }
  }

  // ---------------------------------------------------------------
  // Read receipts only count when the window is actually on screen
  //   `document.visibilityState === 'visible'` is true only when this tab is
  //   the foreground tab AND the window isn't minimised AND (on mobile) the
  //   screen is on and the browser is in front. A backgrounded tab, another
  //   tab, a minimised window or a locked phone all read as 'hidden'.
  //   We additionally require window focus so a visible-but-unfocused window
  //   (split screen, another app on top on desktop) doesn't mark as read.
  // ---------------------------------------------------------------
  // Coarse "is a touch device" check - on mobile there's only ever one visible
  // window, and document.hasFocus() is unreliable (often false in iOS Safari
  // even when the page is clearly in front), so visibility alone is the signal.
  const IS_TOUCH = (('ontouchstart' in window) || navigator.maxTouchPoints > 0);
  function windowIsActive() {
    if (document.visibilityState !== 'visible') return false;
    if (IS_TOUCH) return true;
    // Desktop: also require focus so a background window (another app on top,
    // split screen) doesn't mark messages as read.
    return document.hasFocus();
  }

  // Send `mark_read` for the newest message of the open chat, but only if the
  // window is actually being looked at. Safe to call often (server watermark).
  function markActiveChatReadIfVisible(chatId, newestId) {
    const id = chatId || ctx.activeChatId.value;
    if (!id) return;
    let msgId = newestId;
    if (msgId == null) {
      const last = ctx.messages.value[ctx.messages.value.length - 1];
      msgId = last && last.id;
    }
    if (msgId == null) return;
    if (!windowIsActive()) return;
    ctx.sendReceipt('mark_read', id, msgId);
  }

  // When the tab/window comes back to the foreground with a chat open, flush a
  // read receipt for whatever is now on screen (messages that arrived while it
  // was hidden were delivered-only).
  function flushReadOnActivate() {
    if (!windowIsActive() || !ctx.activeChatId.value) return;
    markActiveChatReadIfVisible(ctx.activeChatId.value);
  }
  document.addEventListener('visibilitychange', flushReadOnActivate);
  window.addEventListener('focus', flushReadOnActivate);

  // A profile edit (name / photo) has no server push - the other clients only
  // learn about it by re-pulling. Refresh the open chat's cached users when the
  // tab regains focus, so switching back to it shows the current photo/name.
  function refreshActiveChatUsers() {
    if (document.visibilityState !== 'visible' || !ctx.activeChatId.value) return;
    const item = ctx.chats.value.find((c) => c.chat.id === ctx.activeChatId.value);
    if (!item) return;
    if (item.chat.is_group) ctx.resolveChatMemberPhones(ctx.activeChatId.value);
    else ctx.resolvePrivateChatTitle(ctx.activeChatId.value, { force: true });
  }
  document.addEventListener('visibilitychange', refreshActiveChatUsers);

  // Write-through: any change to the open chat's message list (live event,
  // edit, delete, optimistic send) refreshes its cached newest page.
  watch(ctx.messages, (list) => {
    if (ctx.activeChatId.value && list && list.length) {
      ctx.saveChatMessages(ctx.activeChatId.value, list);
    }
  }, { deep: true });

  return {
    messagesScrollEl, isPinnedToBottom, scrollMessagesToBottom,
    loadOlderMessages, onMessagesScroll,
    openDraftChat, discardDraftChat, closeActiveChat,
    selectChat, reloadActiveChatIfUnloaded, refreshActiveChatUsers,
    markActiveChatReadIfVisible, windowIsActive,
  };
}
