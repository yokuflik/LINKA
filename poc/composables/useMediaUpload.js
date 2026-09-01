// Media & voice messages. Same direct-to-storage model as avatars: ask the
// app for a presigned PUT ticket, PUT the bytes straight at MinIO, then send
// a send_message WS frame carrying the object key (message_type 2/3/4/5).
// Global `useMediaUpload(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, log, logError, wsIsOpen, activeChatId,
// messageInput, messagesError, MEDIA_MAX_BYTES, shrinkImageToFit,
// notifyRecording, and (from useMessageSend) replyingToMessage - read
// call-time via ctx.
function useMediaUpload(ctx) {
  const { ref } = Vue;

  const MEDIA_IMAGE_MIME = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];
  const MEDIA_VIDEO_MIME = ['video/mp4', 'video/webm', 'video/quicktime'];
  // kind 'file' accepts any content type (backend ALLOWED_UPLOAD_MIME['file']
  // is the empty "allow any" sentinel); image/video stay locked to the sets
  // above since they render inline.
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
    if (!ctx.activeChatId.value) {
      if (ctx.draftChat.value) ctx.messagesError.value = 'Send a message first to start the chat.';
      return;
    }
    const kind = mediaKindForMime(file.type, forceKind);
    if (!kind) {
      ctx.messagesError.value = 'Unsupported file type: ' + (file.type || 'unknown');
      return;
    }
    // A generic document can carry any content type; the browser sometimes
    // reports none at all, so fall back to a neutral one the backend accepts.
    const mimeType = file.type || 'application/octet-stream';
    if (file.size <= 0) { ctx.messagesError.value = 'That file looks empty.'; return; }
    // Downscale/recompress an oversize photo in-browser (not documents/video).
    if (kind === 'image' && file.size > ctx.MEDIA_MAX_BYTES.image) {
      file = await ctx.shrinkImageToFit(file, ctx.MEDIA_MAX_BYTES.image, { maxDim: 1600 });
    }
    if (file.size > ctx.MEDIA_MAX_BYTES[kind]) {
      const label = kind === 'image' ? 'image' : kind === 'video' ? 'video' : 'file';
      const limitMb = ctx.MEDIA_MAX_BYTES[kind] / 1024 / 1024;
      const gotMb = (file.size / 1024 / 1024).toFixed(1);
      ctx.messagesError.value = 'This ' + label + ' is ' + gotMb + ' MB — the maximum is '
        + limitMb + ' MB. Please choose a smaller ' + label + '.';
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
        body: JSON.stringify({ kind, mime_type: mimeType, size_bytes: file.size }),
      });
      const putResp = await fetch(ticket.upload_url, {
        method: 'PUT',
        headers: ticket.required_headers || { 'Content-Type': mimeType },
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
      if (ctx.replyingToMessage.value) payload.reply_to_message_id = ctx.replyingToMessage.value.id;
      ctx.log('WS →', payload);
      ctx.sendRaw(payload);
      ctx.messageInput.value = '';
      ctx.replyingToMessage.value = null;
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
  // Live waveform bars (0..1) shown in the composer while recording. Reassigned
  // to the reactive ref returned by useAudioWaveform.liveMeter on each start,
  // reset to [] on stop. `liveWaveform` is itself a ref-of-ref so templates
  // read `liveWaveform.value` (the inner array).
  const liveWaveform = ref([]);
  let mediaRecorder = null;
  let recordedChunks = [];
  let recordingStream = null;
  let recordingTimer = null;
  let liveMeterHandle = null;
  let liveMeterUnwatch = null;

  async function startRecording() {
    ctx.messagesError.value = '';
    if (!ctx.activeChatId.value) {
      if (ctx.draftChat.value) ctx.messagesError.value = 'Send a message first to start the chat.';
      return;
    }
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
    // Pick a container the backend accepts (config.ALLOWED_UPLOAD_MIME['audio']).
    // Desktop Chrome/Firefox => audio/webm; iOS Safari (16.4+) => audio/mp4.
    const AUDIO_MIME_CANDIDATES = ['audio/webm', 'audio/mp4', 'audio/aac', 'audio/ogg'];
    const mime = AUDIO_MIME_CANDIDATES.find(
      (m) => window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)
    ) || '';
    ctx.log('recording audio as', mime || '(browser default)');
    mediaRecorder = new MediaRecorder(recordingStream, mime ? { mimeType: mime } : undefined);
    // Remember what we asked for - iOS sometimes reports an empty mimeType on stop.
    mediaRecorder._requestedMime = mime;
    mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size) recordedChunks.push(e.data); };
    mediaRecorder.onstop = onRecordingStopped;
    mediaRecorder.start();
    // Start the live analyser on the same mic stream and mirror its bar array
    // into liveWaveform so the composer template stays reactive.
    liveMeterHandle = ctx.liveMeter(recordingStream);
    liveWaveform.value = liveMeterHandle.bars.value;
    liveMeterUnwatch = Vue.watch(liveMeterHandle.bars, (v) => { liveWaveform.value = v; });
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

  function teardownLiveMeter() {
    if (liveMeterUnwatch) { liveMeterUnwatch(); liveMeterUnwatch = null; }
    if (liveMeterHandle) { liveMeterHandle.stop(); liveMeterHandle = null; }
    liveWaveform.value = [];
  }

  async function onRecordingStopped() {
    isRecording.value = false;
    teardownLiveMeter();
    if (recordingStream) { recordingStream.getTracks().forEach((t) => t.stop()); recordingStream = null; }
    const duration = recordingSeconds.value;
    // Resolve the real container: prefer what the recorder reports, fall back
    // to what we asked for, then to the first recorded chunk's own type.
    const reported = (mediaRecorder && mediaRecorder.mimeType) || '';
    const requested = (mediaRecorder && mediaRecorder._requestedMime) || '';
    const chunkType = (recordedChunks[0] && recordedChunks[0].type) || '';
    let type = (reported || requested || chunkType || 'audio/webm').split(';')[0].trim();
    // Map anything the backend doesn't whitelist onto the closest accepted type.
    if (type === 'audio/x-m4a' || type === 'audio/m4a') type = 'audio/mp4';
    if (!['audio/webm', 'audio/mp4', 'audio/aac', 'audio/ogg', 'audio/mpeg'].includes(type)) {
      type = 'audio/mp4';
    }
    ctx.log('voice recording container:', { reported, requested, chunkType, resolved: type });
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
    const ext = { 'audio/webm': 'webm', 'audio/mp4': 'm4a', 'audio/aac': 'aac', 'audio/ogg': 'ogg', 'audio/mpeg': 'mp3' }[type] || 'm4a';
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
      if (ctx.replyingToMessage.value) payload.reply_to_message_id = ctx.replyingToMessage.value.id;
      ctx.log('WS →', payload);
      ctx.sendRaw(payload);
      ctx.replyingToMessage.value = null;
    } catch (err) {
      ctx.logError('voice send failed', err);
      ctx.messagesError.value = 'Could not send that recording: ' + (err.message || err);
    } finally {
      mediaUploadBusy.value = false;
    }
  }

  return {
    sendMediaMessage, mediaUploadBusy,
    isRecording, recordingSeconds, liveWaveform, startRecording, stopRecording,
  };
}
