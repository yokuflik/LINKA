// Single-message view concerns: the right-click context menu, the "Details"
// (receipt log) modal, and image/video orientation probing.
// Global `useMessageMenu(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, log, logError, previewText, groupChatMembers,
// resolveChatMemberPhones, activeChatId, messages, isPinnedToBottom,
// scrollMessagesToBottom.
function useMessageMenu(ctx) {
  const { ref, computed, nextTick } = Vue;

  // ---------------------------------------------------------------
  // Message right-click menu - only "Reply" is wired up (see CLAUDE.md's
  // "not yet built" note for Forward/Edit/Delete, shown disabled in the
  // menu itself so its final shape is visible without pretending they work).
  // ---------------------------------------------------------------
  const contextMenuMessage = ref(null); // the message the menu is open for, or null
  const contextMenuRawPosition = ref({ x: 0, y: 0 });

  // Clamped so the menu (fixed w-40 = 160px, up to ~215px tall with the
  // optional "Details" row) never renders partly off-screen when
  // right-clicking near an edge.
  const contextMenuPosition = computed(() => ({
    x: Math.min(contextMenuRawPosition.value.x, window.innerWidth - 168),
    y: Math.min(contextMenuRawPosition.value.y, window.innerHeight - 224),
  }));

  function openMessageContextMenu({ message, event }) {
    contextMenuMessage.value = message;
    contextMenuRawPosition.value = { x: event.clientX, y: event.clientY };
  }

  function closeMessageContextMenu() {
    contextMenuMessage.value = null;
  }

  // ---------------------------------------------------------------
  // Message "Details" popup - the per-person delivered/read/played receipt
  // log for one message, from the message_receipt_log-backed endpoint. Any
  // participant may view any message's receipts (Telegram-style); the
  // backend enforces "participant only" regardless.
  // ---------------------------------------------------------------
  const detailsModalMessage = ref(null); // the message whose details are shown, or null
  const messageReceipts = ref(null);      // MessageReceiptsOut for that message, or null
  const messageReceiptsLoading = ref(false);
  const messageReceiptsError = ref('');

  // Any real, non-deleted message - in a group every member may see who
  // read/played a message, not just its sender.
  function canShowMessageDetails(m) {
    return !!m && m.sender_id != null && m.deleted_at == null;
  }

  // Pulls the per-person receipt breakdown for one message. Also re-run
  // live (see handleWsMessage) whenever a receipt event lands for the open chat.
  async function loadMessageReceipts(chatId, messageId) {
    messageReceiptsLoading.value = messageReceipts.value == null;
    messageReceiptsError.value = '';
    try {
      // Make sure names are resolvable for group members not yet in userById.
      if (ctx.groupChatMembers.value[chatId] == null) await ctx.resolveChatMemberPhones(chatId);
      const data = await ctx.apiFetch(`/chats/${chatId}/messages/${messageId}/receipts`);
      // Guard against a late response after the user closed / switched.
      if (detailsModalMessage.value && detailsModalMessage.value.id === messageId) {
        messageReceipts.value = data;
      }
    } catch (err) {
      ctx.logError('failed to load message receipts', chatId, messageId, err);
      messageReceiptsError.value = 'Could not load message info';
    } finally {
      messageReceiptsLoading.value = false;
    }
  }

  async function openMessageDetails(message) {
    detailsModalMessage.value = message;
    messageReceipts.value = null;
    messageReceiptsError.value = '';
    closeMessageContextMenu();
    if (ctx.activeChatId.value) await loadMessageReceipts(ctx.activeChatId.value, message.id);
  }

  function closeMessageDetails() {
    detailsModalMessage.value = null;
    messageReceipts.value = null;
    messageReceiptsError.value = '';
  }

  const messageDetailsSnippet = computed(() => {
    const m = detailsModalMessage.value;
    if (!m) return '';
    return ctx.previewText(m.content, m.type);
  });

  // ---------------------------------------------------------------
  // Image/video message orientation (option A: frontend-only, no backend).
  // We only ever render an image/video bubble in one of two fixed shapes -
  // 'landscape' (default, also covers square) or 'portrait'. The bubble
  // reserves that fixed box up front (see MessageList's aspect-ratio
  // container), so scrollHeight is correct before the media decodes and
  // the last-messages-are-photos case scrolls to the bottom properly. We
  // learn the real orientation by probing intrinsic dimensions once per
  // media_url (Image() for photos, a throwaway <video> for videos).
  // ---------------------------------------------------------------
  const imageOrientationByUrl = ref({});
  const probedMediaUrls = new Set();

  function imageOrientation(url) {
    return imageOrientationByUrl.value[url] || 'landscape';
  }

  function setOrientationFromDims(url, w, h) {
    const orient = h > w * 1.1 ? 'portrait' : 'landscape';
    imageOrientationByUrl.value = { ...imageOrientationByUrl.value, [url]: orient };
    // If the shape changed and the user is still at the bottom, re-pin -
    // the reserved box just resized.
    if (ctx.isPinnedToBottom()) nextTick(ctx.scrollMessagesToBottom);
  }

  function probeMediaOrientation(url, kind) {
    if (!url || probedMediaUrls.has(url)) return;
    probedMediaUrls.add(url);
    if (kind === 'video') {
      const probe = document.createElement('video');
      probe.preload = 'metadata';
      probe.onloadedmetadata = () => setOrientationFromDims(url, probe.videoWidth, probe.videoHeight);
      probe.src = url;
    } else {
      const probe = new Image();
      probe.onload = () => setOrientationFromDims(url, probe.naturalWidth, probe.naturalHeight);
      probe.src = url;
    }
  }

  // Kick off probes for every image/video message currently loaded (called
  // after history load and on each new_message).
  function probeLoadedImageOrientations() {
    for (const m of ctx.messages.value) {
      if (m.type === 2 && m.media_url) probeMediaOrientation(m.media_url, 'image');
      else if (m.type === 3 && m.media_url) probeMediaOrientation(m.media_url, 'video');
    }
  }

  return {
    contextMenuMessage, contextMenuPosition, openMessageContextMenu, closeMessageContextMenu,
    canShowMessageDetails, detailsModalMessage, messageReceipts, messageReceiptsLoading,
    messageReceiptsError, messageDetailsSnippet, loadMessageReceipts,
    openMessageDetails, closeMessageDetails,
    imageOrientation, probeMediaOrientation, probeLoadedImageOrientations,
  };
}
