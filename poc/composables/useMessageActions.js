// Message-level actions and compose paths: the right-click context menu, the
// "Details" (receipt log) modal, reply-to-message compose, image/video
// orientation probing, and sending text / media / voice messages over the WS.
// Global `useMessageActions(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, log, logError, wsStatus, wsIsOpen, sendRaw,
// activeChatId, messages, messageInput, messagesError, MEDIA_MAX_BYTES,
// previewText, replySenderLabel, groupChatMembers, resolveChatMemberPhones,
// isPinnedToBottom, scrollMessagesToBottom, notifyRecording.
function useMessageActions(ctx) {
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

  // Deleting a message. Only the sender may delete their own message; the
  // backend re-checks ownership regardless. Soft delete server-side - the
  // bubble stays in place and re-renders as "This message was deleted"
  // (see the message_deleted handler in useWsRouter).
  function canDeleteMessage(m) {
    return !!m && m.sender_id != null && m.deleted_at == null
      && ctx.currentUser.value && m.sender_id === ctx.currentUser.value.id;
  }

  function deleteMessage(message) {
    closeMessageContextMenu();
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
  // (see sendMessage below). The message_edited echo (useWsRouter) sets
  // is_edited / content for other devices; we also update optimistically.
  // ---------------------------------------------------------------
  const editingMessage = ref(null); // the message object being edited, or null

  function canEditMessage(m) {
    return !!m && m.type === 1 && m.sender_id != null && m.deleted_at == null
      && ctx.currentUser.value && m.sender_id === ctx.currentUser.value.id;
  }

  function startEditMessage(message) {
    closeMessageContextMenu();
    if (!canEditMessage(message)) return;
    replyingToMessage.value = null; // edit and reply-compose are mutually exclusive
    editingMessage.value = message;
    ctx.messageInput.value = message.content || '';
  }

  function cancelEdit() {
    editingMessage.value = null;
    ctx.messageInput.value = '';
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
  // Reply-to-message (WhatsApp-style) - the backend already fully supports
  // reply_to_message_id end to end (persisted, returned in history, and on
  // the live new_message event); this is just the compose-time UI for it.
  // ---------------------------------------------------------------
  const replyingToMessage = ref(null); // the message object being replied to, or null

  function startReplyTo(message) {
    if (editingMessage.value) cancelEdit(); // edit and reply-compose are mutually exclusive
    replyingToMessage.value = message;
    closeMessageContextMenu();
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
    if (editingMessage.value) {
      const target = editingMessage.value;
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
      editingMessage.value = null;
      ctx.messageInput.value = '';
      return;
    }

    const payload = {
      type: 'send_message',
      chat_id: ctx.activeChatId.value,
      client_message_id: crypto.randomUUID(),
      content,
      message_type: 1,
    };
    if (replyingToMessage.value) payload.reply_to_message_id = replyingToMessage.value.id;
    ctx.log('WS →', payload);
    ctx.sendRaw(payload);
    ctx.messageInput.value = '';
    replyingToMessage.value = null;
    // Jump to the bottom right away so the composer stays pinned to the
    // newest message; the WS echo will land there a moment later.
    nextTick(ctx.scrollMessagesToBottom);
  }

  // Media messages. Same direct-to-storage model as avatars: ask the app
  // for a presigned PUT ticket, PUT the bytes straight at MinIO, then send
  // a send_message WS frame carrying the object key (message_type 2/3/4/5).
  const MEDIA_IMAGE_MIME = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];
  const MEDIA_VIDEO_MIME = ['video/mp4', 'video/webm', 'video/quicktime'];
  // Must match config.ALLOWED_UPLOAD_MIME['file'] on the backend - the
  // upload-ticket call 400s otherwise.
  const MEDIA_FILE_MIME = [
    'application/pdf', 'text/plain', 'application/zip', 'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  ];
  const MEDIA_MESSAGE_TYPE = { image: 2, video: 3, audio: 4, file: 5 };
  const mediaUploadBusy = ref(false);

  // `forceKind` ('file') is passed when the pick came from the Documents menu
  // entry rather than Photos & Videos, so a picked image is still sent as a
  // downloadable document, not an inline photo.
  function mediaKindForMime(mime, forceKind) {
    if (forceKind === 'file') return 'file';
    if (MEDIA_IMAGE_MIME.includes(mime)) return 'image';
    if (MEDIA_VIDEO_MIME.includes(mime)) return 'video';
    return null;
  }

  async function sendMediaMessage(file, forceKind) {
    ctx.messagesError.value = '';
    if (!ctx.activeChatId.value) return;
    const kind = mediaKindForMime(file.type, forceKind);
    if (!kind) {
      ctx.messagesError.value = 'Unsupported file type: ' + (file.type || 'unknown');
      return;
    }
    if (kind === 'file' && !MEDIA_FILE_MIME.includes(file.type)) {
      ctx.messagesError.value = 'Unsupported document type: ' + (file.type || 'unknown')
        + '. Allowed: PDF, TXT, ZIP, DOC(X), XLS(X).';
      return;
    }
    if (file.size <= 0) { ctx.messagesError.value = 'That file looks empty.'; return; }
    // Downscale/recompress an oversize photo in-browser (not documents/video).
    if (kind === 'image' && file.size > ctx.MEDIA_MAX_BYTES.image) {
      file = await ctx.shrinkImageToFit(file, ctx.MEDIA_MAX_BYTES.image, { maxDim: 1600 });
    }
    if (file.size > ctx.MEDIA_MAX_BYTES[kind]) {
      const label = kind === 'image' ? 'Images' : kind === 'video' ? 'Videos' : 'Files';
      ctx.messagesError.value = label
        + ' must be ' + (ctx.MEDIA_MAX_BYTES[kind] / 1024 / 1024) + ' MB or smaller.';
      return;
    }
    if (!ctx.wsIsOpen()) {
      ctx.messagesError.value = 'Cannot send - WebSocket is not connected.';
      return;
    }
    mediaUploadBusy.value = true;
    try {
      const ticket = await ctx.apiFetch(`/chats/${ctx.activeChatId.value}/messages/upload-ticket`, {
        method: 'POST',
        body: JSON.stringify({ kind, mime_type: file.type, size_bytes: file.size }),
      });
      const putResp = await fetch(ticket.upload_url, {
        method: 'PUT',
        headers: ticket.required_headers || { 'Content-Type': file.type },
        body: file,
      });
      if (!putResp.ok) throw new Error('upload failed (' + putResp.status + ')');

      const payload = {
        type: 'send_message',
        chat_id: ctx.activeChatId.value,
        client_message_id: crypto.randomUUID(),
        message_type: MEDIA_MESSAGE_TYPE[kind],
        media: { key: ticket.storage_key, name: file.name },
      };
      const caption = ctx.messageInput.value.trim();
      if (caption) payload.content = caption;
      if (replyingToMessage.value) payload.reply_to_message_id = replyingToMessage.value.id;
      ctx.log('WS →', payload);
      ctx.sendRaw(payload);
      ctx.messageInput.value = '';
      replyingToMessage.value = null;
    } catch (err) {
      ctx.logError('media send failed', err);
      ctx.messagesError.value = 'Could not send that file: ' + (err.message || err);
    } finally {
      mediaUploadBusy.value = false;
    }
  }

  // Voice recording. First mic press starts a MediaRecorder; second press
  // stops it, uploads the blob as an audio (message_type 4) media message
  // via the same direct-to-storage path as sendMediaMessage. MediaRecorder
  // emits audio/webm by default (in config.ALLOWED_UPLOAD_MIME['audio']).
  const isRecording = ref(false);
  const recordingSeconds = ref(0);
  let mediaRecorder = null;
  let recordedChunks = [];
  let recordingStream = null;
  let recordingTimer = null;

  async function startRecording() {
    ctx.messagesError.value = '';
    if (!ctx.activeChatId.value) return;
    if (isRecording.value) return;
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      ctx.messagesError.value = 'Voice recording is not supported in this browser.';
      return;
    }
    try {
      recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      ctx.messagesError.value = 'Microphone access denied.';
      return;
    }
    recordedChunks = [];
    const mime = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '';
    mediaRecorder = new MediaRecorder(recordingStream, mime ? { mimeType: mime } : undefined);
    mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size) recordedChunks.push(e.data); };
    mediaRecorder.onstop = onRecordingStopped;
    mediaRecorder.start();
    isRecording.value = true;
    recordingSeconds.value = 0;
    ctx.notifyRecording(); // tell the chat immediately, then on every tick
    recordingTimer = setInterval(() => {
      recordingSeconds.value += 1;
      ctx.notifyRecording();
    }, 1000);
  }

  function stopRecording() {
    if (!isRecording.value || !mediaRecorder) return;
    if (recordingTimer) { clearInterval(recordingTimer); recordingTimer = null; }
    mediaRecorder.stop(); // fires onRecordingStopped
  }

  async function onRecordingStopped() {
    isRecording.value = false;
    if (recordingStream) { recordingStream.getTracks().forEach((t) => t.stop()); recordingStream = null; }
    const duration = recordingSeconds.value;
    const type = mediaRecorder && mediaRecorder.mimeType ? mediaRecorder.mimeType.split(';')[0] : 'audio/webm';
    mediaRecorder = null;
    const blob = new Blob(recordedChunks, { type });
    recordedChunks = [];
    if (blob.size <= 0) { ctx.messagesError.value = 'Recording was empty.'; return; }
    if (blob.size > ctx.MEDIA_MAX_BYTES.audio) {
      ctx.messagesError.value = 'Voice message must be ' + (ctx.MEDIA_MAX_BYTES.audio / 1024 / 1024) + ' MB or smaller.';
      return;
    }
    if (!ctx.wsIsOpen()) {
      ctx.messagesError.value = 'Cannot send - WebSocket is not connected.';
      return;
    }
    const ext = type === 'audio/webm' ? 'webm' : (type === 'audio/mp4' ? 'm4a' : 'ogg');
    const name = 'voice-' + Date.now() + '.' + ext;
    mediaUploadBusy.value = true;
    try {
      const ticket = await ctx.apiFetch(`/chats/${ctx.activeChatId.value}/messages/upload-ticket`, {
        method: 'POST',
        body: JSON.stringify({ kind: 'audio', mime_type: type, size_bytes: blob.size }),
      });
      const putResp = await fetch(ticket.upload_url, {
        method: 'PUT',
        headers: ticket.required_headers || { 'Content-Type': type },
        body: blob,
      });
      if (!putResp.ok) throw new Error('upload failed (' + putResp.status + ')');
      const payload = {
        type: 'send_message',
        chat_id: ctx.activeChatId.value,
        client_message_id: crypto.randomUUID(),
        message_type: MEDIA_MESSAGE_TYPE.audio,
        media: { key: ticket.storage_key, name, duration_seconds: duration },
      };
      if (replyingToMessage.value) payload.reply_to_message_id = replyingToMessage.value.id;
      ctx.log('WS →', payload);
      ctx.sendRaw(payload);
      replyingToMessage.value = null;
    } catch (err) {
      ctx.logError('voice send failed', err);
      ctx.messagesError.value = 'Could not send that recording: ' + (err.message || err);
    } finally {
      mediaUploadBusy.value = false;
    }
  }

  return {
    contextMenuMessage, contextMenuPosition, openMessageContextMenu, closeMessageContextMenu,
    canDeleteMessage, deleteMessage,
    canEditMessage, editingMessage, startEditMessage, cancelEdit,
    canShowMessageDetails, detailsModalMessage, messageReceipts, messageReceiptsLoading,
    messageReceiptsError, messageDetailsSnippet, loadMessageReceipts,
    openMessageDetails, closeMessageDetails,
    replyingToMessage, startReplyTo, cancelReply, quotedPreviewFor,
    imageOrientation, probeMediaOrientation, probeLoadedImageOrientations,
    sendMessage, sendMediaMessage, mediaUploadBusy,
    isRecording, recordingSeconds, startRecording, stopRecording,
  };
}
