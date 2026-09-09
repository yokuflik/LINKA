// Media & voice messages. Same direct-to-storage model as avatars: ask the
// app for a presigned PUT ticket, PUT the bytes straight at MinIO, then send
// a send_message WS frame carrying the object key (message_type 2/3/4/5).
// Global `useMediaUpload(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, log, logError, wsIsOpen, activeChatId,
// messageInput, messagesError, MEDIA_MAX_BYTES, shrinkImageToFit,
// notifyRecording, messages, currentUser, scrollMessagesToBottom, and
// (from useMessageSend) replyingToMessage - read call-time via ctx.
function useMediaUpload(ctx) {
  const { ref } = Vue;

  const MEDIA_IMAGE_MIME = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];
  const MEDIA_VIDEO_MIME = ['video/mp4', 'video/webm', 'video/quicktime'];
  // kind 'file' accepts any content type (backend ALLOWED_UPLOAD_MIME['file']
  // is the empty "allow any" sentinel); image/video stay locked to the sets
  // above since they render inline.
  const MEDIA_MESSAGE_TYPE = { image: 2, video: 3, audio: 4, file: 5 };
  const mediaUploadBusy = ref(false);

  // sha256 of a Blob/File as lowercase hex - the content-addressed dedup key
  // the upload-ticket endpoint expects (ADR 0010). Lets the server skip the
  // upload entirely for a file someone already sent.
  async function sha256Hex(blob) {
    const buf = await blob.arrayBuffer();
    const digest = await crypto.subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  }

  // Compute a ThumbHash base64 placeholder (ADR 0014) from a decoded image
  // source. Draws into a <=100px canvas (ThumbHash's hard cap), reads the RGBA
  // bytes, encodes. Any failure returns null - the placeholder is cosmetic and
  // the bubble degrades to eager-load without it.
  function encodeThumbHash(source, srcW, srcH) {
    try {
      if (!window.ThumbHash || !srcW || !srcH) return null;
      const scale = Math.min(1, 100 / Math.max(srcW, srcH));
      const w = Math.max(1, Math.round(srcW * scale));
      const h = Math.max(1, Math.round(srcH * scale));
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const g = canvas.getContext('2d');
      g.drawImage(source, 0, 0, w, h);
      const { data } = g.getImageData(0, 0, w, h);
      return window.ThumbHash.thumbHashToBase64(window.ThumbHash.rgbaToThumbHash(w, h, data));
    } catch (err) {
      ctx.logError('thumbhash encode failed', err);
      return null;
    }
  }

  // Image file -> ThumbHash base64 via a decoded <img>.
  async function computeImageBlurHash(file) {
    try {
      const url = URL.createObjectURL(file);
      try {
        const img = new Image();
        img.decoding = 'async';
        await new Promise((resolve, reject) => {
          img.onload = resolve;
          img.onerror = () => reject(new Error('image decode failed'));
          img.src = url;
        });
        return encodeThumbHash(img, img.naturalWidth, img.naturalHeight);
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (err) {
      ctx.logError('image blur hash failed', err);
      return null;
    }
  }

  // Video file -> ThumbHash base64 of the first frame via a detached <video>
  // seeked to 0.
  async function computeVideoBlurHash(file) {
    try {
      const url = URL.createObjectURL(file);
      const video = document.createElement('video');
      video.muted = true;
      video.preload = 'auto';
      video.playsInline = true;
      try {
        await new Promise((resolve, reject) => {
          video.onloadeddata = resolve;
          video.onerror = () => reject(new Error('video load failed'));
          video.src = url;
        });
        await new Promise((resolve) => {
          video.onseeked = resolve;
          try { video.currentTime = 0; } catch (_) { resolve(); }
          // Some browsers fire nothing if currentTime is already 0.
          setTimeout(resolve, 500);
        });
        return encodeThumbHash(video, video.videoWidth, video.videoHeight);
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (err) {
      ctx.logError('video blur hash failed', err);
      return null;
    }
  }

  // `forceKind` ('file') is passed when the pick came from the Documents menu
  // entry rather than Photos & Videos, so a picked image is still sent as a
  // downloadable document, not an inline photo.
  function mediaKindForMime(mime, forceKind) {
    if (forceKind === 'file') return 'file';
    if (MEDIA_IMAGE_MIME.includes(mime)) return 'image';
    if (MEDIA_VIDEO_MIME.includes(mime)) return 'video';
    return null;
  }

  // Shared "prepare + upload, return a media block" pipeline used by both the
  // live media send (sendMediaMessage) and the scheduled-message flow
  // (useScheduleMessage). Downscales an oversize photo, computes the ThumbHash
  // placeholder, hashes the bytes, asks for an upload ticket, PUTs the bytes
  // (skipped on an already-uploaded dedup hit), and returns everything the
  // caller needs to build a send_message frame or a scheduled-message body.
  //
  // Returns { key, name, mime, size, blur_hash, duration_seconds, file } - the
  // (possibly shrunk) file is handed back so the caller can build an optimistic
  // preview from it. `onProgress(blurHash)` is an optional early callback fired
  // once the blur hash is known (before the upload) so an already-rendered
  // optimistic bubble can adopt it.
  async function prepareMediaBlock(file, kind, { chatId, durationSeconds = null, onBlurHash } = {}) {
    const targetChatId = chatId || ctx.activeChatId.value;
    const mimeType = file.type || 'application/octet-stream';
    // Downscale/recompress an oversize photo in-browser (not documents/video/audio).
    if (kind === 'image' && file.size > ctx.MEDIA_MAX_BYTES.image) {
      file = await ctx.shrinkImageToFit(file, ctx.MEDIA_MAX_BYTES.image, { maxDim: 1600 });
    }
    if (file.size > ctx.MEDIA_MAX_BYTES[kind]) {
      throw new Error(kind === 'image'
        ? 'image is still too large after downscaling'
        : 'file is too large');
    }
    // Blur placeholder (ADR 0014): computed from the final (post-shrink) file.
    let blurHash = null;
    if (kind === 'image') blurHash = await computeImageBlurHash(file);
    else if (kind === 'video') blurHash = await computeVideoBlurHash(file);
    if (blurHash && typeof onBlurHash === 'function') onBlurHash(blurHash);

    const sha256 = await sha256Hex(file);
    const ticket = await ctx.apiFetch(`/chats/${targetChatId}/messages/upload-ticket`, {
      method: 'POST',
      body: JSON.stringify({ kind, mime_type: mimeType, size_bytes: file.size, sha256 }),
    });
    // already_uploaded => the bytes are on the server from a prior send;
    // skip the PUT entirely (the whole point of ADR 0010).
    if (!ticket.already_uploaded) {
      const putResp = await fetch(ticket.upload_url, {
        method: 'PUT',
        headers: ticket.required_headers || { 'Content-Type': mimeType },
        body: file,
      });
      if (!putResp.ok) throw new Error('upload failed (' + putResp.status + ')');
    }
    return {
      key: ticket.storage_key,
      name: file.name,
      mime: mimeType,
      size: file.size,
      blur_hash: blurHash,
      duration_seconds: durationSeconds,
      file,
    };
  }

  async function sendMediaMessage(file, forceKind) {
    ctx.messagesError.value = '';
    if (!ctx.activeChatId.value) {
      if (ctx.draftChat.value) ctx.messagesError.value = 'Send a message first to start the chat.';
      return;
    }
    const kind = mediaKindForMime(file.type, forceKind);
    if (!kind) {
      ctx.messagesError.value = "That file type isn't supported. Please choose a photo, video, or document.";
      return;
    }
    // A generic document can carry any content type; the browser sometimes
    // reports none at all, so fall back to a neutral one the backend accepts.
    const mimeType = file.type || 'application/octet-stream';
    if (file.size <= 0) { ctx.messagesError.value = 'That file looks empty.'; return; }
    // Non-image oversize is a hard stop (we don't recompress video / documents).
    // An oversize photo is downscaled below, after the optimistic bubble is up.
    if (kind !== 'image' && file.size > ctx.MEDIA_MAX_BYTES[kind]) {
      const label = kind === 'video' ? 'video' : 'file';
      const limitMb = ctx.MEDIA_MAX_BYTES[kind] / 1024 / 1024;
      const gotMb = (file.size / 1024 / 1024).toFixed(1);
      ctx.messagesError.value = 'This ' + label + ' is ' + gotMb + ' MB — the maximum is '
        + limitMb + ' MB. Please choose a smaller ' + label + '.';
      return;
    }
    if (!ctx.wsIsOpen()) {
      ctx.messagesError.value = "You appear to be offline right now. Please try again once you're reconnected.";
      return;
    }

    const clientMessageId = crypto.randomUUID();
    const caption = ctx.messageInput.value.trim();
    const replyToId = ctx.replyingToMessage.value ? ctx.replyingToMessage.value.id : null;

    // Optimistic bubble FIRST, before any of the slow work (shrink, thumbhash,
    // hashing, upload). The sender already holds the bytes they just picked, so
    // render straight from a local object URL with a 🕓 pending clock - never
    // download our own media back from the server. The bubble stays pending
    // until its own new_message echo lands, at which point the normal
    // sent/delivered/read tick logic takes over. `_localMediaUrl` marks the row
    // so the reconcile keeps this URL (see useWsRouter) and the message cache
    // swaps in the presigned URL only when persisting (blob: URLs die on reload).
    const localUrl = URL.createObjectURL(file);
    let optimistic = null;
    if (ctx.activeChatId.value) {
      optimistic = {
        id: null, client_message_id: clientMessageId, chat_id: ctx.activeChatId.value,
        sender_id: ctx.currentUser.value.id, type: MEDIA_MESSAGE_TYPE[kind],
        content: caption || null, created_at: new Date().toISOString(),
        is_edited: false, edited_at: null, status: 'SENT',
        reply_to_message_id: replyToId,
        media_url: localUrl, _localMediaUrl: localUrl,
        media_mime: mimeType, media_size: file.size, media_name: file.name,
        media_duration_seconds: null, media_blur_hash: null,
        pending: true, send_failed: false,
      };
      ctx.messages.value.push(optimistic);
      Vue.nextTick(ctx.scrollMessagesToBottom);
    }
    // Clear the composer now - the bubble is already on screen.
    ctx.messageInput.value = '';
    ctx.replyingToMessage.value = null;

    mediaUploadBusy.value = true;
    try {
      const chatId = ctx.activeChatId.value;
      const block = await prepareMediaBlock(file, kind, {
        chatId,
        onBlurHash: (h) => { if (optimistic) optimistic.media_blur_hash = h; },
      });
      const blurHash = block.blur_hash;

      const payload = {
        type: 'send_message',
        chat_id: chatId,
        client_message_id: clientMessageId,
        message_type: MEDIA_MESSAGE_TYPE[kind],
        media: { key: block.key, name: block.name },
      };
      if (blurHash) payload.media.blur_hash = blurHash;
      if (caption) payload.content = caption;
      if (replyToId) payload.reply_to_message_id = replyToId;
      ctx.log('WS →', payload);
      ctx.sendRaw(payload);
    } catch (err) {
      ctx.logError('media send failed', err);
      if (optimistic) { optimistic.pending = false; optimistic.send_failed = true; }
      ctx.messagesError.value = ctx.friendlyError(err, "We couldn't send that file. Please try again.");
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
    // Unlock the shared AudioContext synchronously inside this tap gesture,
    // before any await - otherwise iOS leaves it 'suspended' and the FIRST
    // voice message's playback waveform (decodeAudioData) silently fails.
    if (ctx.unlock) ctx.unlock();
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      ctx.messagesError.value = 'Voice recording is not supported in this browser.';
      return;
    }
    try {
      recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      ctx.messagesError.value = "We couldn't access your microphone. Please allow microphone access and try again.";
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
      ctx.messagesError.value = "You appear to be offline right now. Please try again once you're reconnected.";
      return;
    }
    const ext = { 'audio/webm': 'webm', 'audio/mp4': 'm4a', 'audio/aac': 'aac', 'audio/ogg': 'ogg', 'audio/mpeg': 'mp3' }[type] || 'm4a';
    const name = 'voice-' + Date.now() + '.' + ext;

    const clientMessageId = crypto.randomUUID();
    const chatId = ctx.activeChatId.value;
    const replyToId = ctx.replyingToMessage.value ? ctx.replyingToMessage.value.id : null;

    // Optimistic voice bubble FIRST, played from the local recording blob with
    // a 🕓 pending clock - no round-trip to fetch our own audio back, and the
    // 🕓 -> tick transition follows the normal receipt logic. See sendMediaMessage.
    const localUrl = URL.createObjectURL(blob);
    let optimistic = null;
    if (chatId) {
      optimistic = {
        id: null, client_message_id: clientMessageId, chat_id: chatId,
        sender_id: ctx.currentUser.value.id, type: MEDIA_MESSAGE_TYPE.audio,
        content: null, created_at: new Date().toISOString(),
        is_edited: false, edited_at: null, status: 'SENT',
        reply_to_message_id: replyToId,
        media_url: localUrl, _localMediaUrl: localUrl,
        media_mime: type, media_size: blob.size, media_name: name,
        media_duration_seconds: duration, media_blur_hash: null,
        pending: true, send_failed: false,
      };
      ctx.messages.value.push(optimistic);
      Vue.nextTick(ctx.scrollMessagesToBottom);
    }
    ctx.replyingToMessage.value = null;

    mediaUploadBusy.value = true;
    try {
      const sha256 = await sha256Hex(blob);
      const ticket = await ctx.apiFetch(`/chats/${chatId}/messages/upload-ticket`, {
        method: 'POST',
        body: JSON.stringify({ kind: 'audio', mime_type: type, size_bytes: blob.size, sha256 }),
      });
      if (!ticket.already_uploaded) {
        const putResp = await fetch(ticket.upload_url, {
          method: 'PUT',
          headers: ticket.required_headers || { 'Content-Type': type },
          body: blob,
        });
        if (!putResp.ok) throw new Error('upload failed (' + putResp.status + ')');
      }
      const payload = {
        type: 'send_message',
        chat_id: chatId,
        client_message_id: clientMessageId,
        message_type: MEDIA_MESSAGE_TYPE.audio,
        media: { key: ticket.storage_key, name, duration_seconds: duration },
      };
      if (replyToId) payload.reply_to_message_id = replyToId;
      ctx.log('WS →', payload);
      ctx.sendRaw(payload);
    } catch (err) {
      ctx.logError('voice send failed', err);
      if (optimistic) { optimistic.pending = false; optimistic.send_failed = true; }
      ctx.messagesError.value = ctx.friendlyError(err, "We couldn't send that voice message. Please try again.");
    } finally {
      mediaUploadBusy.value = false;
    }
  }

  return {
    sendMediaMessage, mediaUploadBusy, prepareMediaBlock, mediaKindForMime,
    isRecording, recordingSeconds, liveWaveform, startRecording, stopRecording,
  };
}
