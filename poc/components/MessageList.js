// Scrollable message pane: system-message pills, bubbles (mine/theirs), and
// the "no messages" empty state. Kept as one component (not split further
// into a MessageBubble child) since the v-for body's mine/theirs/system
// branches share the same list and messagesEl ref must stay on this
// scrolling container for scrollMessagesToBottom to keep working unchanged.
const MessageList = {
  // `messages` is read straight from the LinkaChatStore singleton (ADR 0035),
  // injected by the app root in setup() - not passed down as a prop.
  props: {
    currentUser: { type: Object, required: true },
    // Group chat? Sender names above bubbles only show in groups; in a 1:1
    // chat the peer's name is already in the header, so it's noise here.
    isGroup: { type: Boolean, default: false },
    shouldShowSystemMessage: { type: Function, required: true },
    systemMessageText: { type: Function, required: true },
    senderLabel: { type: Function, required: true },
    senderAvatarUrl: { type: Function, required: true },
    senderAvatarPreview: { type: Function, required: true },
    statusTickSymbol: { type: Function, required: true },
    statusTickClass: { type: Function, required: true },
    quotedPreviewFor: { type: Function, required: true },
    // (url) => 'portrait' | 'landscape' - resolved frontend-side by the root
    // (probes naturalWidth/Height once per url, defaults 'landscape'). Drives
    // the fixed reserved box below so the bubble has its final height before
    // the image decodes.
    imageOrientation: { type: Function, required: true },
    // ADR 0014 blur placeholder: (hash) => 'data:image/png;base64,...' | null
    // and (hash) => width/height ratio | null. Both memoized in the composable.
    thumbHashToDataUrl: { type: Function, required: true },
    thumbHashToAspect: { type: Function, required: true },
    // True when the history fetch failed (server down / offline) and there's
    // nothing cached - show a "waiting for connection" state instead of the
    // misleading "No messages here" empty state.
    connectionError: { type: Boolean, default: false },
    // True while the initial history fetch is in flight with nothing on screen
    // yet - show a spinner, not "No messages here".
    loading: { type: Boolean, default: false },
    // True while a "load older" page is being fetched (or retried on a dead
    // connection) - shows a spinner pinned at the top of the pane.
    loadingOlder: { type: Boolean, default: false },
    // True when that "load older" fetch is stuck retrying offline.
    loadingOlderRetrying: { type: Boolean, default: false },
    // Per-outgoing-media upload progress keyed by client_message_id
    // (useMediaUpload.uploadProgress): number 0..1, null = indeterminate
    // (spin, don't fill), absent = not uploading.
    uploadProgress: { type: Object, default: () => ({}) },
  },
  emits: ['message-contextmenu', 'load-older', 'voice-played', 'retry-message'],
  // Exposes the scrollable element so the root's scrollMessagesToBottom()
  // (which needs messagesEl.value.scrollTop/scrollHeight) keeps working
  // unchanged across the component boundary.
  setup(props, { emit }) {
    const messagesEl = Vue.ref(null);
    // Shared message list - the single source of truth (ADR 0035). Falls back
    // to the global singleton if injection is unavailable (e.g. isolated test).
    const store = Vue.inject('chatStore', null)
      || (typeof LinkaChatStore !== 'undefined' ? LinkaChatStore : null);
    const messages = Vue.computed(() => (store ? store.messages.value : []));
    // On scroll, count how many message rows are still fully above the top of
    // the viewport and hand that to the root - it decides when to page.
    // Only real message rows carry data-row="msg"; sticky day separators are
    // skipped so they don't inflate the paging count.
    function onScroll() {
      const el = messagesEl.value;
      if (!el) return;
      // Each message row is nested inside a spacing wrapper div, so el.children
      // are the wrappers - query the tagged rows directly. Count the ones fully
      // scrolled above the visible area (measured against el's own top, since
      // offsetParent isn't guaranteed to be el).
      const elTop = el.getBoundingClientRect().top;
      let rowsAbove = 0;
      for (const rowEl of el.querySelectorAll('[data-row="msg"]')) {
        if (rowEl.getBoundingClientRect().bottom < elTop) rowsAbove++;
        else break;
      }
      emit('load-older', rowsAbove);
    }
    // Client-side day grouping: walk messages in order and, whenever the local
    // calendar day of created_at changes, inject a { separator, label } marker
    // before the message. Purely derived from created_at, no backend field.
    function dayKey(iso) {
      const d = new Date(iso);
      return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
    }
    function dayLabel(iso) {
      const d = new Date(iso);
      const today = new Date();
      const yesterday = new Date();
      yesterday.setDate(today.getDate() - 1);
      if (dayKey(iso) === dayKey(today.toISOString())) return 'Today';
      if (dayKey(iso) === dayKey(yesterday.toISOString())) return 'Yesterday';
      // Full localized date; include the year only when it differs from the
      // current one (WhatsApp behaviour) - keeps same-year pills compact
      // without ever dropping the year when it actually matters.
      const opts = d.getFullYear() === today.getFullYear()
        ? { day: 'numeric', month: 'long' }
        : { day: 'numeric', month: 'long', year: 'numeric' };
      return d.toLocaleDateString([], opts);
    }
    // Two messages belong to the same visual cluster when the same user sent
    // them back-to-back within a short window (WhatsApp-style). Within a
    // cluster we tighten the vertical gap, show the sender name only on the
    // first bubble and the sender avatar only next to the last one.
    const GROUP_WINDOW_MS = 10 * 60 * 1000;
    function sameCluster(prev, curr) {
      if (!prev || !curr) return false;
      if (prev.sender_id == null || curr.sender_id == null) return false;
      if (prev.sender_id !== curr.sender_id) return false;
      if (!prev.created_at || !curr.created_at) return false;
      return dayKey(prev.created_at) === dayKey(curr.created_at)
        && (new Date(curr.created_at) - new Date(prev.created_at)) <= GROUP_WINDOW_MS;
    }
    const rows = Vue.computed(() => {
      const out = [];
      let lastKey = null;
      const list = messages.value;
      for (let i = 0; i < list.length; i++) {
        const m = list[i];
        if (!m.created_at) {
          out.push({ type: 'msg', m, groupStart: true, groupEnd: true });
          continue;
        }
        const k = dayKey(m.created_at);
        if (k !== lastKey) {
          out.push({ type: 'separator', key: 'sep-' + k, label: dayLabel(m.created_at) });
          lastKey = k;
        }
        const groupStart = !sameCluster(list[i - 1], m);
        const groupEnd = !sameCluster(m, list[i + 1]);
        out.push({ type: 'msg', m, groupStart, groupEnd });
      }
      return out;
    });
    // A pure image/video message (no caption, not a reply) - rendered flush
    // with no bubble background/padding so only the media's own black border
    // shows, not the green/white bubble.
    function isBareMedia(m) {
      return (m.type === 2 || m.type === 3) && m.media_url && !m.content && m.reply_to_message_id == null
        && m.deleted_at == null;
    }

    // Per-image "has it finished loading?" flags, keyed by media_url, so the
    // spinner overlay can sit above the white placeholder until <img> @load.
    const imageLoaded = Vue.reactive({});
    function markImageLoaded(url, event) {
      if (url) imageLoaded[url] = true;
      if (event && event.target) event.target.classList.remove('opacity-0');
    }

    // ADR 0014: a hash-bearing image/video bubble does not fetch its bytes
    // until the viewer taps it. `_openedMedia` holds the message ids whose
    // real <img>/<video> src is now bound. Legacy rows with no blur_hash keep
    // loading eagerly (openMedia is a no-op guard away).
    const openedMedia = Vue.reactive({});
    function hasBlur(m) { return !!(m && m.media_blur_hash); }
    function isMediaOpened(m) {
      // Our own just-sent media is already local (blob: URL) - show it straight
      // away, never behind a "tap to view" affordance.
      return !hasBlur(m) || !!m._localMediaUrl || !!openedMedia[m.id || m.client_message_id];
    }
    function openMedia(m) {
      openedMedia[m.id || m.client_message_id] = true;
    }
    // Human-readable byte size for the download button label.
    function formatBytes(n) {
      if (!n || n <= 0) return '';
      if (n < 1024) return n + ' B';
      if (n < 1048576) return Math.max(1, Math.round(n / 1024)) + ' KB';
      return (n / 1048576).toFixed(1) + ' MB';
    }
    // Which hash-bearing bubbles are mid-download (spinner on the button).
    const mediaDownloading = Vue.reactive({});
    // Per-message blob: URL of downloaded bytes. We bind THIS (not the presigned
    // URL) as the <img>/<video> src so the browser never sees the S3
    // Content-Disposition: attachment header and never triggers a "save as".
    const downloadedBlobUrl = Vue.reactive({});
    function mediaSrc(m) {
      const key = m.id || m.client_message_id;
      return downloadedBlobUrl[key] || m.media_url;
    }
    // "Download" = pull the bytes into memory and reveal the media inline in the
    // bubble (WhatsApp-style). No "save as" dialog, no auto-save.
    async function downloadMedia(m) {
      const key = m.id || m.client_message_id;
      if (!m.media_url || mediaDownloading[key]) return;
      mediaDownloading[key] = true;
      try {
        const resp = await fetch(m.media_url);
        if (!resp.ok) throw new Error('http ' + resp.status);
        const blob = await resp.blob();
        downloadedBlobUrl[key] = URL.createObjectURL(blob);
        openMedia(m);
      } catch (err) {
        console.error('[Linka] media download failed', err);
      } finally {
        mediaDownloading[key] = false;
      }
    }
    // Reserved-box style from the thumbhash aspect ratio (falls back to the
    // legacy two-shape guess when the row has no hash). Width is capped so a
    // very wide/tall image still fits the pane.
    function mediaBoxStyle(m) {
      // Prefer a locally-measured aspect ratio on the sender's own optimistic
      // bubble (set before upload) so the reserved box is right from the first
      // paint and never resizes mid-load; else fall back to the thumbhash.
      const ratio = m._localAspect || (hasBlur(m) ? props.thumbHashToAspect(m.media_blur_hash) : null);
      if (!ratio) return null;
      const w = ratio >= 1 ? 256 : 192;
      return { width: w + 'px', aspectRatio: String(ratio) };
    }
    function blurUrl(m) {
      return hasBlur(m) ? props.thumbHashToDataUrl(m.media_blur_hash) : null;
    }

    // Small thumbnail for a quoted image/video reply: the real presigned URL
    // if we have it (already loaded elsewhere in this chat), else the blur
    // data: URL, else nothing. Never triggers its own download.
    // Tapping a quoted reply preview scrolls to the original message (if it's
    // in the loaded list) and briefly highlights it.
    const highlightedId = Vue.ref(null);
    let highlightTimer = null;
    function jumpToQuoted(m) {
      if (m.reply_to_message_id == null) return;
      const targetId = m.reply_to_message_id;
      const root = messagesEl.value;
      if (!root) return;
      const el = root.querySelector('[data-mid="' + targetId + '"]');
      if (!el) return; // scrolled out of the loaded page - nothing to jump to
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      highlightedId.value = targetId;
      if (highlightTimer) clearTimeout(highlightTimer);
      highlightTimer = setTimeout(() => { highlightedId.value = null; }, 1600);
    }

    function quotedReplyThumb(m) {
      const q = props.quotedPreviewFor(m);
      if (!q || !q.media || (q.media.type !== 2 && q.media.type !== 3)) return null;
      if (q.media.url) return q.media.url;
      if (q.media.blur_hash) return props.thumbHashToDataUrl(q.media.blur_hash);
      return null;
    }

    // Long-press = right-click on touch devices (WhatsApp/Telegram/iMessage
    // convention). Hold ~450ms without moving more than a few px, then open
    // the same context menu at the touch point. A move/scroll or an early
    // lift cancels it, so a tap or a swipe-scroll still works normally.
    let pressTimer = null;
    let pressStart = null;
    let pressFired = false;
    const LONG_PRESS_MS = 450;
    const MOVE_TOLERANCE_PX = 10;

    function onTouchStart(m, event) {
      if (!event.touches || event.touches.length !== 1) return;
      const t = event.touches[0];
      pressStart = { x: t.clientX, y: t.clientY };
      pressFired = false;
      clearTimeout(pressTimer);
      pressTimer = setTimeout(() => {
        pressFired = true;
        if (navigator.vibrate) navigator.vibrate(10);
        emit('message-contextmenu', {
          message: m,
          event: { clientX: pressStart.x, clientY: pressStart.y },
        });
      }, LONG_PRESS_MS);
    }
    function onTouchMove(event) {
      if (!pressStart || !event.touches || !event.touches.length) return;
      const t = event.touches[0];
      if (Math.abs(t.clientX - pressStart.x) > MOVE_TOLERANCE_PX ||
          Math.abs(t.clientY - pressStart.y) > MOVE_TOLERANCE_PX) {
        clearTimeout(pressTimer);
      }
    }
    function onTouchEnd(event) {
      clearTimeout(pressTimer);
      // Swallow the click/tap that follows a long-press so it doesn't also
      // trigger the bubble's normal tap behaviour (e.g. opening a file).
      if (pressFired && event.cancelable) event.preventDefault();
      pressStart = null;
    }

    // Upload-progress ring for the sender's own still-uploading photo/video.
    // Shown only while the row is optimistic (_localMediaUrl + pending) and an
    // entry exists in uploadProgress. `uploadFraction` is null for an
    // indeterminate (offline / not-computable) ring - the SVG then just spins.
    const RING_CIRCUMFERENCE = 2 * Math.PI * 20; // r=20
    function isUploading(m) {
      const key = m.client_message_id;
      return !!(m._localMediaUrl && m.pending && key && key in props.uploadProgress);
    }
    function uploadFraction(m) {
      const v = props.uploadProgress[m.client_message_id];
      return typeof v === 'number' ? Math.max(0, Math.min(1, v)) : null;
    }
    function ringDashOffset(m) {
      const f = uploadFraction(m);
      return RING_CIRCUMFERENCE * (1 - (f == null ? 0.25 : f));
    }
    function uploadPercentLabel(m) {
      const f = uploadFraction(m);
      return f == null ? '' : Math.round(f * 100) + '%';
    }

    return {
      messages,
      RING_CIRCUMFERENCE,
      isUploading, uploadFraction, ringDashOffset, uploadPercentLabel,
      messagesEl, onScroll, isBareMedia, rows,
      onTouchStart, onTouchMove, onTouchEnd,
      imageLoaded, markImageLoaded,
      isMediaOpened, openMedia, mediaBoxStyle, blurUrl,
      formatBytes, downloadMedia, mediaDownloading, mediaSrc,
      quotedReplyThumb, jumpToQuoted, highlightedId,
    };
  },
  expose: ['messagesEl'],
  template: `
    <div ref="messagesEl" @scroll="onScroll" class="flex-1 overflow-y-auto p-4">
      <!-- "Load older" spinner, pinned at the top while a previous page is
           being fetched (or retried on a dead connection). -->
      <div v-if="messages.length && loadingOlder" class="flex flex-col items-center justify-center gap-1 py-3 text-xs text-slate-400">
        <span class="w-5 h-5 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
        <span v-if="loadingOlderRetrying">No connection — retrying…</span>
      </div>
      <template v-for="row in rows" :key="row.type === 'separator' ? row.key : (row.m.id || row.m.client_message_id)">
        <!-- Sticky day separator (WhatsApp-style). data-row is absent so
             onScroll's paging count ignores it. -->
        <div v-if="row.type === 'separator'" class="day-separator flex justify-center py-1">
          <span class="inline-block w-40 text-center px-3 py-1 rounded-full text-[11px] font-medium bg-slate-200 text-slate-600 shadow-sm whitespace-nowrap overflow-hidden text-ellipsis">{{ row.label }}</span>
        </div>
      <template v-else>
      <template v-for="m in [row.m]" :key="m.id || m.client_message_id">
      <div :class="row.groupEnd ? 'mb-2' : 'mb-0.5'">
        <!-- System messages (sender_id == null, e.g. "X added Y to the group") -
             centered, small, gray pill, like WhatsApp's own group-event lines.
             shouldShowSystemMessage filters out "role_changed" notices for
             anyone but the actor/target - see chat_service.change_member_role. -->
        <div v-if="m.sender_id == null && shouldShowSystemMessage(m)" data-row="msg" class="flex justify-center">
          <span class="inline-block px-2.5 py-1 rounded-full text-[11px] bg-slate-200 text-slate-600">{{ systemMessageText(m) }}</span>
        </div>
      <div v-else-if="m.sender_id != null" data-row="msg"
           :data-mid="m.id"
           class="max-w-md w-fit flex items-end gap-2 rounded-2xl transition-colors duration-500"
           :class="[m.sender_id === currentUser.id ? 'ml-auto text-right' : '', highlightedId && m.id === highlightedId ? 'bg-amber-200/70' : '']">
        <template v-if="m.sender_id !== currentUser.id">
          <Avatar v-if="row.groupEnd"
                  :url="senderAvatarUrl(m.sender_id)" :preview="senderAvatarPreview(m.sender_id)" :name="senderLabel(m.sender_id)"
                  :colorKey="m.sender_id" sizeClass="w-7 h-7 text-xs"
                  class="shrink-0 mb-[18px]" />
          <!-- Keep bubbles aligned when the avatar is hidden mid-cluster. -->
          <div v-else class="w-7 shrink-0"></div>
        </template>
        <div class="min-w-0">
        <div class="inline-block text-sm cursor-pointer"
             :class="[
               isBareMedia(m)
                 ? 'p-0 bg-transparent rounded-lg'
                 : (m.sender_id === currentUser.id
                     ? (row.groupEnd
                         ? 'px-3 py-2 rounded-2xl bubble-tail bg-teal-700 text-white rounded-br-none bubble-tail-mine'
                         : 'px-3 py-2 rounded-2xl bg-teal-700 text-white')
                     : (row.groupEnd
                         ? 'px-3 py-2 rounded-2xl bubble-tail bg-white border border-slate-200 rounded-bl-none bubble-tail-theirs'
                         : 'px-3 py-2 rounded-2xl bg-white border border-slate-200'))
             ]"
             @contextmenu.prevent="$emit('message-contextmenu', { message: m, event: $event })"
             @touchstart.passive="onTouchStart(m, $event)"
             @touchmove.passive="onTouchMove($event)"
             @touchend="onTouchEnd($event)"
             @touchcancel="onTouchEnd($event)">
          <!-- Soft-deleted message: a "This message was deleted" tombstone in
               place of the original content (WhatsApp-style). -->
          <span v-if="m.deleted_at" class="italic opacity-70"
                :class="m.sender_id === currentUser.id ? 'text-white/80' : 'text-slate-400'">🚫 This message was deleted</span>
          <template v-else>
          <div v-if="isGroup && row.groupStart && m.sender_id !== currentUser.id && !isBareMedia(m)" class="text-[11px] opacity-60 mb-0.5">{{ senderLabel(m.sender_id) }}</div>
          <!-- Quoted reply preview (WhatsApp-style) - only when this message
               is itself a reply (reply_to_message_id set). quotedPreviewFor
               looks the original message up client-side (it's a lookup, not
               a re-render decision, so it stays a plain function prop). -->
          <div v-if="quotedPreviewFor(m)" class="mb-1 pl-2 pr-1 py-1 rounded border-l-4 text-left text-xs flex items-stretch gap-2 cursor-pointer"
               @click.stop="jumpToQuoted(m)"
               :class="m.sender_id === currentUser.id ? 'bg-white/10 border-white/60 text-white/90' : 'bg-slate-100 border-teal-600 text-slate-600'">
            <div class="min-w-0 flex-1">
              <div class="font-semibold truncate">{{ quotedPreviewFor(m).sender }}</div>
              <div v-if="quotedPreviewFor(m).media" class="truncate opacity-90 flex items-center gap-1">
                <span>{{ quotedPreviewFor(m).media.type === 2 ? '📷'
                  : quotedPreviewFor(m).media.type === 3 ? '🎬'
                  : quotedPreviewFor(m).media.type === 4 ? '🎤' : '📄' }}</span>
                <span class="truncate">{{ quotedPreviewFor(m).snippet
                  || quotedPreviewFor(m).media.name
                  || quotedPreviewFor(m).media.kindLabel }}</span>
              </div>
              <div v-else class="truncate opacity-90">{{ quotedPreviewFor(m).snippet }}</div>
            </div>
            <!-- Tiny thumbnail for image / video replies. -->
            <img v-if="quotedReplyThumb(m)" :src="quotedReplyThumb(m)" alt=""
                 @error="$event.target.style.display='none'"
                 class="shrink-0 w-9 h-9 rounded object-cover self-center" />
          </div>
          <!-- Media attachment (image / video). media_url is a short-lived
               presigned GET attached by the backend to both history and the
               live new_message event. -->
          <!-- Image: fixed reserved box in one of two shapes (portrait /
               landscape). The box has its final size immediately (bg-white
               placeholder), so scrollHeight is right before the <img>
               decodes; the image fades in on load, filling the box
               (object-cover). Two shapes only, by design. -->
          <!-- ADR 0014: box sized from the thumbhash aspect (mediaBoxStyle),
               else the legacy two-shape guess. When the row has a blur_hash
               the real <img> src is withheld until the bubble is tapped -
               a blurred data: URL background stands in with a "tap to view"
               affordance. Rows without a hash load eagerly as before. -->
          <div v-if="m.media_url && m.type === 2" class="mb-1">
            <!-- Our own just-sent photo: the local file is right here, show it
                 directly in a reserved box, no spinner / opacity / lazy load. -->
            <div v-if="m._localMediaUrl"
                 class="relative rounded-lg overflow-hidden bg-slate-100 border border-black"
                 :style="mediaBoxStyle(m)"
                 :class="mediaBoxStyle(m) ? '' : (imageOrientation(m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]')">
              <img :src="m.media_url" :alt="m.media_name || 'image'"
                   class="w-full h-full object-cover" />
              <div v-if="isUploading(m)" class="absolute inset-0 flex items-center justify-center bg-black/30">
                <div class="relative w-14 h-14">
                  <svg viewBox="0 0 48 48" class="w-full h-full -rotate-90"
                       :class="uploadFraction(m) == null ? 'animate-spin' : ''">
                    <circle cx="24" cy="24" r="20" fill="none" stroke="rgba(255,255,255,0.3)" stroke-width="4" />
                    <circle cx="24" cy="24" r="20" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"
                            :stroke-dasharray="RING_CIRCUMFERENCE" :stroke-dashoffset="ringDashOffset(m)"
                            style="transition: stroke-dashoffset 0.2s linear" />
                  </svg>
                  <span class="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-white">{{ uploadPercentLabel(m) }}</span>
                </div>
              </div>
            </div>
            <div v-else class="relative rounded-lg overflow-hidden bg-white border border-black"
                 :style="mediaBoxStyle(m)"
                 :class="mediaBoxStyle(m) ? '' : (imageOrientation(m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]')">
              <img v-if="blurUrl(m) && !isMediaOpened(m)" :src="blurUrl(m)" alt="" @error="$event.target.style.display='none'"
                   class="absolute inset-0 w-full h-full object-cover" />
              <div v-if="!isMediaOpened(m)"
                   class="absolute inset-0 flex items-center justify-center">
                <button type="button" @click.stop="downloadMedia(m)" :disabled="mediaDownloading[m.id || m.client_message_id]"
                        class="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium bg-black/60 text-white hover:bg-black/75 disabled:opacity-60">
                  <span v-if="mediaDownloading[m.id || m.client_message_id]" class="w-3.5 h-3.5 rounded-full border-2 border-white/40 border-t-white animate-spin"></span>
                  <svg v-else viewBox="0 0 24 24" class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 21h16"/></svg>
                  <span>{{ formatBytes(m.media_size) || 'Download' }}</span>
                </button>
              </div>
              <template v-if="isMediaOpened(m)">
                <div v-if="!imageLoaded[m.media_url]" class="absolute inset-0 flex items-center justify-center">
                  <span class="w-6 h-6 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
                </div>
                <img :src="mediaSrc(m)" :alt="m.media_name || 'image'" loading="lazy"
                     class="w-full h-full object-cover opacity-0 transition-opacity duration-200"
                     @load="markImageLoaded(m.media_url, $event)" />
              </template>
            </div>
          </div>
          <!-- Video: same reserved box; blurred first frame + ▶ badge until
               tapped, then a real <video controls>. -->
          <div v-else-if="m.media_url && m.type === 3" class="mb-1">
            <!-- Our own just-sent video: play straight from the local file, in
                 a reserved box. -->
            <div v-if="m._localMediaUrl"
                 class="relative rounded-lg overflow-hidden bg-black border border-black"
                 :style="mediaBoxStyle(m)"
                 :class="mediaBoxStyle(m) ? '' : (imageOrientation(m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]')">
              <video :src="m.media_url" controls preload="metadata"
                     class="w-full h-full object-contain"></video>
              <div v-if="isUploading(m)" class="absolute inset-0 flex items-center justify-center bg-black/40 pointer-events-none">
                <div class="relative w-14 h-14">
                  <svg viewBox="0 0 48 48" class="w-full h-full -rotate-90"
                       :class="uploadFraction(m) == null ? 'animate-spin' : ''">
                    <circle cx="24" cy="24" r="20" fill="none" stroke="rgba(255,255,255,0.3)" stroke-width="4" />
                    <circle cx="24" cy="24" r="20" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"
                            :stroke-dasharray="RING_CIRCUMFERENCE" :stroke-dashoffset="ringDashOffset(m)"
                            style="transition: stroke-dashoffset 0.2s linear" />
                  </svg>
                  <span class="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-white">{{ uploadPercentLabel(m) }}</span>
                </div>
              </div>
            </div>
            <div v-else class="relative rounded-lg overflow-hidden bg-black/80 border border-black"
                 :style="mediaBoxStyle(m)"
                 :class="mediaBoxStyle(m) ? '' : (imageOrientation(m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]')">
              <template v-if="!isMediaOpened(m)">
                <img v-if="blurUrl(m)" :src="blurUrl(m)" alt="" @error="$event.target.style.display='none'"
                     class="absolute inset-0 w-full h-full object-cover" />
                <div class="absolute inset-0 flex items-center justify-center">
                  <button type="button" @click.stop="downloadMedia(m)" :disabled="mediaDownloading[m.id || m.client_message_id]"
                          class="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium bg-black/60 text-white hover:bg-black/75 disabled:opacity-60">
                    <span v-if="mediaDownloading[m.id || m.client_message_id]" class="w-3.5 h-3.5 rounded-full border-2 border-white/40 border-t-white animate-spin"></span>
                    <svg v-else viewBox="0 0 24 24" class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 21h16"/></svg>
                    <span>{{ formatBytes(m.media_size) || 'Download' }}</span>
                  </button>
                </div>
              </template>
              <video v-else :src="mediaSrc(m)" controls preload="metadata"
                     controlslist="nodownload noremoteplayback" disablepictureinpicture
                     class="w-full h-full object-contain"></video>
            </div>
          </div>
          <!-- Voice message (type 4): clean custom player - play/pause toggle
               + progress track + elapsed/total time. -->
          <VoiceMessage v-else-if="m.media_url && m.type === 4"
                        :src="m.media_url"
                        :decodeSrc="m.media_url_remote || m.media_url"
                        :durationSeconds="m.media_duration_seconds || 0"
                        :mine="m.sender_id === currentUser.id" class="mb-1"
                        @played="$emit('voice-played', m)" />
          <!-- File (type 5): an attachment card, a touch larger than a normal
               bubble. Clicking opens the presigned GET in a new tab - the
               browser previews what it can (PDF, text, images) and downloads
               the rest. -->
          <a v-else-if="m.media_url && m.type === 5" :href="m.media_url" target="_blank" rel="noopener"
             class="mb-1 flex items-center gap-3 px-3 py-3 rounded-xl no-underline min-w-[15rem] max-w-[18rem] transition-colors"
             :class="m.sender_id === currentUser.id ? 'bg-white/15 text-white hover:bg-white/25' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'"
             :title="'Open ' + (m.media_name || 'file')">
            <span class="shrink-0 w-10 h-10 flex items-center justify-center rounded-lg text-xl"
                  :class="m.sender_id === currentUser.id ? 'bg-white/20' : 'bg-white'">📄</span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm font-medium">{{ m.media_name || 'File' }}</span>
              <span class="block text-[11px] opacity-70">{{ (m.media_size ? (m.media_size < 1048576
                ? Math.max(1, Math.round(m.media_size / 1024)) + ' KB'
                : (m.media_size / 1048576).toFixed(1) + ' MB') + ' · ' : '') + 'Tap to open' }}</span>
            </span>
          </a>
          <div v-else-if="m.type >= 2 && m.type <= 5" class="mb-1 text-xs italic opacity-70">
            [attachment unavailable]
          </div>
          <span v-if="m.content">{{ m.content }}</span>
          <span v-if="m.is_edited" class="text-[10px] opacity-60">{{ m.edited_at
            ? ' (edited ' + new Date(m.edited_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ')'
            : ' (edited)' }}</span>
          </template>
        </div>
        <div v-if="row.groupEnd || m.send_failed || m.pending"
             class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-1"
             :class="m.sender_id === currentUser.id ? 'justify-end' : ''">
          <span>{{ new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}</span>
          <span v-if="m.send_failed" @click="$emit('retry-message', m)" role="button"
                class="text-sm font-bold leading-none text-red-500 cursor-pointer" title="Not sent — tap to retry">⚠️</span>
          <span v-else-if="m.pending" class="text-sm leading-none text-slate-400" title="Sending…">🕓</span>
          <span v-else-if="m.sender_id === currentUser.id" class="text-sm font-bold leading-none" :class="statusTickClass(m.status)">{{ statusTickSymbol(m.status) }}</span>
        </div>
        </div>
      </div>
      </div>
      </template>
      </template>
      </template>
      <div v-if="!messages.length && connectionError" class="h-full flex flex-col items-center justify-center gap-3 text-sm text-slate-400">
        <span class="w-7 h-7 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
        <span>Waiting for connection…</span>
      </div>
      <div v-else-if="!messages.length && loading" class="h-full flex flex-col items-center justify-center gap-3 text-sm text-slate-400">
        <span class="w-7 h-7 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
        <span>Loading messages…</span>
      </div>
      <p v-else-if="!messages.length" class="h-full flex items-center justify-center text-sm text-slate-400">No messages here</p>
    </div>
  `,
};
