// Chat body of the AI agent drawer (AGENT_DRAWER_UI_PLAN.md Step 7). A
// minimal, dedicated read/compose loop over `agentMessages` - NOT MessageList
// (which hard-reads LinkaChatStore.messages/activeChatId; reusing it here
// would make opening the drawer act like navigating away from whatever chat
// the user has open behind it). Bubble look (shape/tail/colors), the
// timestamp + delivered/read ticks row, and the day-separator pills mirror
// MessageList.js exactly (same Tailwind classes, same statusTickSymbol/
// statusTickClass from LinkaChatStore) so the agent's own chat is visually
// indistinguishable from a normal 1:1 chat - only its content (agent replies
// are plain text, type 7) differs.
const AgentChatView = {
  props: {
    currentUser: { type: Object, required: true },
    messages: { type: Array, required: true },
    // Set briefly by useAgentConfig.js's jumpToAgentMessage (a search hit) to
    // flash-highlight one bubble, same amber treatment as MessageList.js.
    highlightedId: { default: null },
    loading: { type: Boolean, default: false },
    hasMore: { type: Boolean, default: false },
    loadingOlder: { type: Boolean, default: false },
    thinkingStatus: { default: null }, // { status, detail } | null
    // a file picked via the [+] "Attached file" menu, staged (not yet sent)
    // so the owner can add a caption before sending; same shape as
    // MessageInput's stagedAttachment prop.
    stagedAttachment: { default: null },
    // ADR 0059 - true once either token-usage window is exhausted; disables
    // the composer (input, +, send) until the window resets. Server-side is
    // the real enforcement (invoke_worker.py's pre-flight gate) - this is a
    // UX convenience only.
    usageBlocked: { type: Boolean, default: false },
    // {window_5h, window_7d} | null - used only to compute the exact
    // "frozen until HH:MM" clock time shown above the composer while blocked
    // (UsageProgressBar's popover is the only place the percentages/bars
    // themselves are shown, per explicit user requirement).
    usage: { type: Object, default: null },
    // Same media-rendering helper props MessageList.js takes, threaded through
    // so image/video/voice/file bubbles render identically to a normal chat.
    imageOrientation: { type: Function, required: true },
    thumbHashToDataUrl: { type: Function, required: true },
    thumbHashToAspect: { type: Function, required: true },
    uploadProgress: { type: Object, default: () => ({}) },
  },
  emits: ['send', 'send-attachment', 'load-older', 'pick-knowledge-file', 'stage-attachment', 'clear-attachment', 'voice-played'],
  data() {
    return {
      draft: '', pinnedToBottom: true, prependAdjust: null, now: Date.now(), attachMenuOpen: false,
      imageLoaded: {}, openedMedia: {}, mediaDownloading: {}, downloadedBlobUrl: {},
      // ADR 0086: a caption is mandatory for an attached file (it's the only
      // signal send_attached_file has to match the file to a customer's
      // request later) - true right after a blocked submit, cleared as soon
      // as the owner types something or cancels the attachment.
      captionRequiredWarning: false,
    };
  },
  mounted() {
    this.scrollToBottom();
    this._tickTimer = setInterval(() => { this.now = Date.now(); }, 1000);
  },
  beforeUnmount() {
    if (this._tickTimer) clearInterval(this._tickTimer);
  },
  watch: {
    // ADR 0086: clear the caption-required hint as soon as the owner starts
    // typing something, or as soon as the staged attachment is cancelled.
    draft(text) {
      if (text.trim()) this.captionRequiredWarning = false;
    },
    stagedAttachment(val) {
      if (!val) this.captionRequiredWarning = false;
    },
    // A search jump can land on a message already scrolled off-screen (e.g.
    // an around-window replace) - scroll it into view once the DOM updates.
    highlightedId(id) {
      if (!id) return;
      this.pinnedToBottom = false;
      this.$nextTick(() => {
        const el = this.$refs.scrollEl && this.$refs.scrollEl.querySelector('[data-msg-id="' + id + '"]');
        if (el) el.scrollIntoView({ block: 'center' });
      });
    },
  },
  computed: {
    RING_CIRCUMFERENCE() { return 2 * Math.PI * 20; }, // r=20, same as MessageList.js
    // Whichever blocked window resets furthest in the future is the one
    // actually still gating the composer (usageBlocked is true if ANY
    // window is blocked).
    freezeEndsAt() {
      if (!this.usage) return null;
      const candidates = ['window_5h', 'window_7d']
        .map((k) => this.usage[k])
        .filter((w) => w && w.is_blocked);
      if (!candidates.length) return null;
      const elapsedMs = this.now - (this.usage._fetchedAtMs || this.now);
      const latest = candidates.reduce((a, b) => (b.resets_in_seconds > a.resets_in_seconds ? b : a));
      const remainingMs = Math.max(0, latest.resets_in_seconds * 1000 - elapsedMs);
      return new Date(this.now + remainingMs);
    },
    freezeEndsAtLabel() {
      const d = this.freezeEndsAt;
      if (!d) return '';
      const sameDay = d.toDateString() === new Date(this.now).toDateString();
      const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      return sameDay ? time : `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })}, ${time}`;
    },
    // Same day-grouping + WhatsApp-style clustering as MessageList.js's `rows`
    // computed - a message from the same sender within GROUP_WINDOW_MS of the
    // previous one is visually clustered (tight gap, one timestamp at the end).
    rows() {
      const GROUP_WINDOW_MS = 10 * 60 * 1000;
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
        const opts = d.getFullYear() === today.getFullYear()
          ? { day: 'numeric', month: 'long' }
          : { day: 'numeric', month: 'long', year: 'numeric' };
        return d.toLocaleDateString([], opts);
      }
      function sameCluster(prev, curr) {
        if (!prev || !curr) return false;
        if (prev.type !== curr.type) return false;
        if (!prev.created_at || !curr.created_at) return false;
        return dayKey(prev.created_at) === dayKey(curr.created_at)
          && (new Date(curr.created_at) - new Date(prev.created_at)) <= GROUP_WINDOW_MS;
      }
      const out = [];
      let lastKey = null;
      // Purged/soft-deleted rows (deleted_at set) survive in the DB as
      // content=null tombstones so a live in-place delete can render
      // (ADR 0021) and so purge keeps the partition key (ADR 0050's
      // chat-wide reset) - but this view has no "message deleted" bubble
      // like MessageList.js does, so just drop them from what's shown.
      const list = this.messages.filter(m => !m.deleted_at);
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
    },
  },
  beforeUpdate() {
    // Preserve scroll position when older messages are prepended by
    // load-older, mirroring useChatOpen.js's loadOlderMessages - without
    // this the scroll-to-top trigger and the resulting prepend fight each
    // other and the view jumps back to the newly-loaded block's top.
    if (this.loadingOlderCaptured) {
      const el = this.$refs.scrollEl;
      if (el) this.prependAdjust = { prevHeight: el.scrollHeight, prevTop: el.scrollTop };
      this.loadingOlderCaptured = false;
    }
  },
  updated() {
    if (this.prependAdjust) {
      const el = this.$refs.scrollEl;
      if (el) el.scrollTop = this.prependAdjust.prevTop + (el.scrollHeight - this.prependAdjust.prevHeight);
      this.prependAdjust = null;
      return;
    }
    if (this.pinnedToBottom) this.scrollToBottom();
  },
  methods: {
    // Agent replies (type 7) and system messages (type 6, e.g. the
    // pause_and_escalate handoff notice, sender_id=null) render like
    // "theirs"; only the owner's own sends (any other type, always
    // sender_id=owner) render like "mine".
    isMine(m) { return m.type !== 7 && m.type !== 6; },
    // Global function from composables/messageFormat.js (shared with
    // MessageList.js): WhatsApp-style *bold* + "- " bullet-list rendering.
    formatMessageContent,
    formatTime(iso) {
      return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    },
    // Mirrors useChatOpen.js's scrollMessagesToBottom: jump now, then re-pin
    // as any still-loading image/video in the pane finishes sizing (a media
    // bubble's box only reaches its final height after its bytes load).
    scrollToBottom() {
      const el = this.$refs.scrollEl;
      if (!el) return;
      const jump = () => { el.scrollTop = el.scrollHeight; };
      jump();
      requestAnimationFrame(jump);
      const media = el.querySelectorAll('img, video');
      media.forEach((node) => {
        const done = node.tagName === 'IMG' ? node.complete : node.readyState >= 1;
        if (done) return;
        const onSettled = () => {
          node.removeEventListener('load', onSettled);
          node.removeEventListener('loadedmetadata', onSettled);
          node.removeEventListener('error', onSettled);
          if (this.pinnedToBottom) jump();
        };
        node.addEventListener('load', onSettled);
        node.addEventListener('loadedmetadata', onSettled);
        node.addEventListener('error', onSettled);
      });
    },
    onScroll() {
      const el = this.$refs.scrollEl;
      if (!el) return;
      this.pinnedToBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      if (el.scrollTop < 80 && this.hasMore && !this.loadingOlder) {
        this.loadingOlderCaptured = true;
        this.$emit('load-older');
      }
    },
    submit() {
      if (this.usageBlocked) return;
      const text = this.draft.trim();
      if (!text && !this.stagedAttachment) return;
      if (this.stagedAttachment && !text) {
        // ADR 0086: a caption describing the file is mandatory - block the
        // send and show an inline hint instead of emitting send-attachment
        // with an empty caption.
        this.captionRequiredWarning = true;
        return;
      }
      this.captionRequiredWarning = false;
      this.pinnedToBottom = true;
      if (this.stagedAttachment) {
        this.$emit('send-attachment', text);
      } else {
        this.$emit('send', text);
      }
      this.draft = '';
      this.$nextTick(this.resizeDraft);
    },
    onEnter(event) {
      if (event.shiftKey) return; // allow newline
      event.preventDefault();
      this.submit();
    },
    // Auto-grow the textarea to fit its content, capped at ~6 lines.
    resizeDraft() {
      const el = this.$refs.draftEl;
      if (!el) return;
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 144) + 'px';
    },
    toggleAttachMenu() {
      this.attachMenuOpen = !this.attachMenuOpen;
    },
    closeAttachMenu() {
      this.attachMenuOpen = false;
    },
    openKnowledgeFilePicker() {
      this.closeAttachMenu();
      this.$refs.knowledgeFileInput.value = '';
      this.$refs.knowledgeFileInput.click();
    },
    onKnowledgeFileChosen(event) {
      const file = event.target.files && event.target.files[0];
      if (file) this.$emit('pick-knowledge-file', file);
    },
    openAttachmentPicker() {
      this.closeAttachMenu();
      this.$refs.attachmentFileInput.value = '';
      this.$refs.attachmentFileInput.click();
    },
    onAttachmentFileChosen(event) {
      const file = event.target.files && event.target.files[0];
      // No forceKind: auto-detect by MIME (image/video/file), matching the
      // main chat composer so images/videos sent here render inline too.
      if (file) this.$emit('stage-attachment', file);
    },
    formatFileSize(bytes) {
      if (!bytes && bytes !== 0) return '';
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    },
    // File (type 5) cards: browsers preview PDFs inline, so only those get
    // "Tap to open" - everything else just saves to disk, so say "download".
    isPdfFile(m) {
      const mime = (m.media_mime || '').toLowerCase();
      if (mime) return mime === 'application/pdf';
      return /\.pdf$/i.test(m.media_name || '');
    },
    // Media rendering below mirrors MessageList.js exactly (ADR 0014 blur
    // placeholder, download-on-tap, upload-progress ring) so an image/video/
    // voice/file sent in the owner-agent chat looks identical to a normal chat.
    hasBlur(m) { return !!(m && m.media_blur_hash); },
    isMediaOpened(m) {
      return !this.hasBlur(m) || !!m._localMediaUrl || !!this.openedMedia[m.id || m.client_message_id];
    },
    openMedia(m) {
      this.openedMedia = { ...this.openedMedia, [m.id || m.client_message_id]: true };
    },
    markImageLoaded(url, event) {
      if (url) this.imageLoaded = { ...this.imageLoaded, [url]: true };
      if (event && event.target) event.target.classList.remove('opacity-0');
    },
    mediaBoxStyle(m) {
      const ratio = m._localAspect || (this.hasBlur(m) ? this.thumbHashToAspect(m.media_blur_hash) : null);
      if (!ratio) return null;
      const w = ratio >= 1 ? 256 : 192;
      return { width: w + 'px', aspectRatio: String(ratio) };
    },
    // Caps the bubble to the image/video box width when there's a caption,
    // so the caption text can't wrap wider than the media above it.
    bubbleMaxWidthStyle(m) {
      if (!m.content || !(m.type === 2 || m.type === 3)) return null;
      const box = this.mediaBoxStyle(m);
      const w = box ? parseInt(box.width, 10) : (this.imageOrientation(m.media_url) === 'portrait' ? 192 : 256);
      return { maxWidth: w + 'px' };
    },
    blurUrl(m) {
      return this.hasBlur(m) ? this.thumbHashToDataUrl(m.media_blur_hash) : null;
    },
    mediaSrc(m) {
      const key = m.id || m.client_message_id;
      return this.downloadedBlobUrl[key] || m.media_url;
    },
    async downloadMedia(m) {
      const key = m.id || m.client_message_id;
      if (!m.media_url || this.mediaDownloading[key]) return;
      this.mediaDownloading = { ...this.mediaDownloading, [key]: true };
      try {
        const resp = await fetch(m.media_url);
        if (!resp.ok) throw new Error('http ' + resp.status);
        const blob = await resp.blob();
        this.downloadedBlobUrl = { ...this.downloadedBlobUrl, [key]: URL.createObjectURL(blob) };
        this.openMedia(m);
      } catch (err) {
        console.error('[Linka] agent chat media download failed', err);
      } finally {
        this.mediaDownloading = { ...this.mediaDownloading, [key]: false };
      }
    },
    isUploading(m) {
      const key = m.client_message_id;
      return !!(m._localMediaUrl && m.pending && key && key in this.uploadProgress);
    },
    uploadFraction(m) {
      const v = this.uploadProgress[m.client_message_id];
      return typeof v === 'number' ? Math.max(0, Math.min(1, v)) : null;
    },
    ringDashOffset(m) {
      const f = this.uploadFraction(m);
      return this.RING_CIRCUMFERENCE * (1 - (f == null ? 0.25 : f));
    },
    uploadPercentLabel(m) {
      const f = this.uploadFraction(m);
      return f == null ? '' : Math.round(f * 100) + '%';
    },
  },
  template: `
    <div class="flex flex-col h-full min-h-0">
      <div ref="scrollEl" @scroll="onScroll" class="flex-1 min-h-0 overflow-y-auto p-4 chat-background">
        <div v-if="loadingOlder" class="flex flex-col items-center justify-center gap-1 py-3 text-xs text-slate-400">
          <span class="w-5 h-5 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
        </div>
        <p v-if="loading" class="text-xs text-slate-400 text-center py-2">Loading…</p>
        <template v-for="row in rows" :key="row.type === 'separator' ? row.key : (row.m.id || row.m.client_message_id)">
          <div v-if="row.type === 'separator'" class="day-separator flex justify-center py-1">
            <span class="inline-block px-3 py-1 rounded-full text-[11px] font-medium bg-slate-200 text-slate-600 shadow-sm whitespace-nowrap">{{ row.label }}</span>
          </div>
          <template v-else>
          <div :data-msg-id="row.m.id" :class="[row.groupEnd ? 'mb-2' : 'mb-0.5', highlightedId && row.m.id === highlightedId ? 'bg-amber-200/70 rounded-2xl' : '']">
            <div class="max-w-md w-fit flex items-end gap-2 rounded-2xl"
                 :class="isMine(row.m) ? 'ml-auto text-right' : ''">
              <div class="min-w-0">
                <div dir="auto" class="inline-block text-sm cursor-default whitespace-pre-wrap break-words"
                     :style="bubbleMaxWidthStyle(row.m)"
                     :class="isMine(row.m)
                       ? (row.groupEnd
                           ? 'px-3 py-2 rounded-2xl bubble-tail bg-teal-700 text-white rounded-br-none bubble-tail-mine'
                           : 'px-3 py-2 rounded-2xl bg-teal-700 text-white')
                       : (row.groupEnd
                           ? 'px-3 py-2 rounded-2xl bubble-tail bg-white border border-slate-200 rounded-bl-none bubble-tail-theirs'
                           : 'px-3 py-2 rounded-2xl bg-white border border-slate-200')">
                  <!-- Image: same reserved box / blur-placeholder / download-on-tap
                       behavior as MessageList.js (ADR 0014). -->
                  <div v-if="row.m.media_url && row.m.type === 2" class="mb-1">
                    <div v-if="row.m._localMediaUrl"
                         class="relative rounded-lg overflow-hidden bg-slate-100 border border-black"
                         :style="mediaBoxStyle(row.m)"
                         :class="mediaBoxStyle(row.m) ? '' : (imageOrientation(row.m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]')">
                      <img :src="row.m.media_url" :alt="row.m.media_name || 'image'" class="w-full h-full object-cover" />
                      <div v-if="isUploading(row.m)" class="absolute inset-0 flex items-center justify-center bg-black/30">
                        <div class="relative w-14 h-14">
                          <svg viewBox="0 0 48 48" class="w-full h-full -rotate-90" :class="uploadFraction(row.m) == null ? 'animate-spin' : ''">
                            <circle cx="24" cy="24" r="20" fill="none" stroke="rgba(255,255,255,0.3)" stroke-width="4" />
                            <circle cx="24" cy="24" r="20" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"
                                    :stroke-dasharray="RING_CIRCUMFERENCE" :stroke-dashoffset="ringDashOffset(row.m)"
                                    style="transition: stroke-dashoffset 0.2s linear" />
                          </svg>
                          <span class="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-white">{{ uploadPercentLabel(row.m) }}</span>
                        </div>
                      </div>
                    </div>
                    <div v-else class="relative rounded-lg overflow-hidden bg-white border border-black"
                         :style="mediaBoxStyle(row.m)"
                         :class="mediaBoxStyle(row.m) ? '' : (imageOrientation(row.m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]')">
                      <img v-if="blurUrl(row.m) && !isMediaOpened(row.m)" :src="blurUrl(row.m)" alt="" @error="$event.target.style.display='none'"
                           class="absolute inset-0 w-full h-full object-cover" />
                      <div v-if="!isMediaOpened(row.m)" class="absolute inset-0 flex items-center justify-center">
                        <button type="button" @click.stop="downloadMedia(row.m)" :disabled="mediaDownloading[row.m.id || row.m.client_message_id]"
                                class="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium bg-black/60 text-white hover:bg-black/75 disabled:opacity-60">
                          <span v-if="mediaDownloading[row.m.id || row.m.client_message_id]" class="w-3.5 h-3.5 rounded-full border-2 border-white/40 border-t-white animate-spin"></span>
                          <svg v-else viewBox="0 0 24 24" class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 21h16"/></svg>
                          <span>{{ formatFileSize(row.m.media_size) || 'Download' }}</span>
                        </button>
                      </div>
                      <template v-if="isMediaOpened(row.m)">
                        <div v-if="!imageLoaded[row.m.media_url]" class="absolute inset-0 flex items-center justify-center">
                          <span class="w-6 h-6 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
                        </div>
                        <img :src="mediaSrc(row.m)" :alt="row.m.media_name || 'image'" loading="lazy"
                             class="w-full h-full object-cover opacity-0 transition-opacity duration-200"
                             @load="markImageLoaded(row.m.media_url, $event)" />
                      </template>
                    </div>
                  </div>
                  <!-- Video: same reserved box; blurred first frame + tap-to-play. -->
                  <div v-else-if="row.m.media_url && row.m.type === 3" class="mb-1">
                    <div v-if="row.m._localMediaUrl"
                         class="relative rounded-lg overflow-hidden bg-black border border-black"
                         :style="mediaBoxStyle(row.m)"
                         :class="mediaBoxStyle(row.m) ? '' : (imageOrientation(row.m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]')">
                      <video :src="row.m.media_url" controls preload="metadata" class="w-full h-full object-contain"></video>
                      <div v-if="isUploading(row.m)" class="absolute inset-0 flex items-center justify-center bg-black/40 pointer-events-none">
                        <div class="relative w-14 h-14">
                          <svg viewBox="0 0 48 48" class="w-full h-full -rotate-90" :class="uploadFraction(row.m) == null ? 'animate-spin' : ''">
                            <circle cx="24" cy="24" r="20" fill="none" stroke="rgba(255,255,255,0.3)" stroke-width="4" />
                            <circle cx="24" cy="24" r="20" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"
                                    :stroke-dasharray="RING_CIRCUMFERENCE" :stroke-dashoffset="ringDashOffset(row.m)"
                                    style="transition: stroke-dashoffset 0.2s linear" />
                          </svg>
                          <span class="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-white">{{ uploadPercentLabel(row.m) }}</span>
                        </div>
                      </div>
                    </div>
                    <div v-else class="relative rounded-lg overflow-hidden bg-black/80 border border-black"
                         :style="mediaBoxStyle(row.m)"
                         :class="mediaBoxStyle(row.m) ? '' : (imageOrientation(row.m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]')">
                      <template v-if="!isMediaOpened(row.m)">
                        <img v-if="blurUrl(row.m)" :src="blurUrl(row.m)" alt="" @error="$event.target.style.display='none'"
                             class="absolute inset-0 w-full h-full object-cover" />
                        <div class="absolute inset-0 flex items-center justify-center">
                          <button type="button" @click.stop="downloadMedia(row.m)" :disabled="mediaDownloading[row.m.id || row.m.client_message_id]"
                                  class="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium bg-black/60 text-white hover:bg-black/75 disabled:opacity-60">
                            <span v-if="mediaDownloading[row.m.id || row.m.client_message_id]" class="w-3.5 h-3.5 rounded-full border-2 border-white/40 border-t-white animate-spin"></span>
                            <svg v-else viewBox="0 0 24 24" class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 21h16"/></svg>
                            <span>{{ formatFileSize(row.m.media_size) || 'Download' }}</span>
                          </button>
                        </div>
                      </template>
                      <video v-else :src="mediaSrc(row.m)" controls preload="metadata"
                             controlslist="nodownload noremoteplayback" disablepictureinpicture
                             class="w-full h-full object-contain"></video>
                    </div>
                  </div>
                  <!-- Voice message (type 4): same custom player as MessageList.js. -->
                  <VoiceMessage v-else-if="row.m.media_url && row.m.type === 4"
                                :src="row.m.media_url"
                                :decodeSrc="row.m.media_url_remote || row.m.media_url"
                                :durationSeconds="row.m.media_duration_seconds || 0"
                                :mine="isMine(row.m)" class="mb-1"
                                @played="$emit('voice-played', row.m)" />
                  <!-- File (type 5): attachment card, opens the presigned GET in a new tab. -->
                  <a v-else-if="row.m.media_url && row.m.type === 5" :href="row.m.media_url" target="_blank" rel="noopener"
                     class="mb-1 flex items-center gap-3 px-3 py-3 rounded-xl no-underline min-w-[15rem] max-w-[18rem] transition-colors"
                     :class="isMine(row.m) ? 'bg-white/15 text-white hover:bg-white/25' : 'bg-slate-100 text-slate-700 hover:bg-slate-200'"
                     :title="'Open ' + (row.m.media_name || 'file')">
                    <span class="shrink-0 w-10 h-10 flex items-center justify-center rounded-lg text-xl"
                          :class="isMine(row.m) ? 'bg-white/20' : 'bg-white'">📄</span>
                    <span class="min-w-0 flex-1">
                      <span class="block truncate text-sm font-medium">{{ row.m.media_name || 'File' }}</span>
                      <span class="block text-[11px] opacity-70">{{ (row.m.media_size ? formatFileSize(row.m.media_size) + ' · ' : '') + (isPdfFile(row.m) ? 'Tap to open' : 'Tap to download') }}</span>
                    </span>
                  </a>
                  <div v-else-if="row.m.type >= 2 && row.m.type <= 5" class="mb-1 text-xs italic opacity-70">
                    [attachment unavailable]
                  </div>
                  <span v-if="row.m.content" v-html="formatMessageContent(row.m.content)"></span>
                </div>
                <div v-if="row.groupEnd || row.m.send_failed || row.m.pending"
                     class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-1"
                     :class="isMine(row.m) ? 'justify-end' : ''">
                  <span>{{ formatTime(row.m.created_at) }}</span>
                  <span v-if="row.m.send_failed" class="text-sm font-bold leading-none text-red-500" title="Not sent">⚠️</span>
                  <span v-else-if="row.m.pending" class="text-sm leading-none text-slate-400" title="Sending…">🕓</span>
                </div>
              </div>
            </div>
          </div>
          </template>
        </template>
        <div v-if="thinkingStatus" class="mb-2 flex justify-start">
          <div class="max-w-[80%] rounded-2xl px-3 py-1.5 text-xs italic text-slate-500 bg-white border border-slate-200">
            {{ thinkingStatus.detail || 'Thinking…' }}
          </div>
        </div>
      </div>
      <p v-if="usageBlocked" class="shrink-0 px-3 py-1 text-[11px] text-rose-600 bg-rose-50 border-t border-rose-100 text-center">
        Usage limit reached - frozen until {{ freezeEndsAtLabel }}
      </p>
      <!-- Staged attachment preview: picked via [+] "Attached file" but not
           sent yet, so a caption can be added first; x cancels without sending. -->
      <div v-if="stagedAttachment" class="shrink-0 border-t border-slate-200 px-3 pt-2 flex items-center gap-2">
        <div class="flex-1 min-w-0 flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5">
          <img v-if="stagedAttachment.isImage" :src="stagedAttachment.previewUrl" alt=""
               class="shrink-0 w-10 h-10 rounded object-cover" />
          <span v-else class="shrink-0 text-2xl leading-none">{{ stagedAttachment.kind === 'video' ? '🎬' : '📄' }}</span>
          <div class="min-w-0 text-xs">
            <div class="truncate font-medium text-slate-700">{{ stagedAttachment.file.name }}</div>
            <div class="text-slate-400">{{ formatFileSize(stagedAttachment.file.size) }}</div>
          </div>
        </div>
        <button @click="$emit('clear-attachment')" class="text-slate-400 hover:text-slate-600 text-lg leading-none px-1">&times;</button>
      </div>
      <p v-if="captionRequiredWarning" class="shrink-0 px-3 pt-1 text-[11px] text-rose-600">
        Please describe what this file is before sending - your agent needs it to know when to reuse it.
      </p>
      <div class="shrink-0 border-t border-slate-200 p-2 flex items-center gap-2">
        <div class="relative shrink-0 self-end">
          <button type="button" @click="toggleAttachMenu" :disabled="usageBlocked"
                  class="w-9 h-9 rounded-full bg-slate-100 hover:bg-slate-200 text-slate-600 text-xl leading-none flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed"
                  :class="attachMenuOpen ? 'bg-slate-200' : ''"
                  title="Attach">+</button>
          <div v-if="attachMenuOpen"
               class="absolute bottom-11 left-0 z-20 w-56 bg-white border border-slate-200 rounded-lg shadow-lg py-1 text-sm">
            <button type="button" @click="openKnowledgeFilePicker"
                    class="w-full text-left px-3 py-2 hover:bg-slate-50 flex items-center gap-2">
              <span>📄</span><span>Content file (txt/pdf)</span>
            </button>
            <button type="button" @click="openAttachmentPicker"
                    class="w-full text-left px-3 py-2 hover:bg-slate-50 flex items-center gap-2">
              <span>📎</span><span>Attached file</span>
            </button>
          </div>
        </div>
        <input ref="knowledgeFileInput" type="file" class="hidden"
               accept=".txt,.md,.markdown,text/plain,text/markdown,application/pdf"
               @change="onKnowledgeFileChosen" />
        <input ref="attachmentFileInput" type="file" class="hidden" @change="onAttachmentFileChosen" />
        <textarea ref="draftEl" v-model="draft" @keydown.enter="onEnter" @input="resizeDraft" @focus="closeAttachMenu" :disabled="usageBlocked"
                  rows="1" placeholder="Message your agent…" dir="auto"
                  class="flex-1 min-w-0 px-3 py-1.5 text-sm border border-slate-300 rounded-2xl resize-none leading-normal max-h-36 overflow-y-auto disabled:bg-slate-100 disabled:cursor-not-allowed"></textarea>
        <button @click="submit" :disabled="(!draft.trim() && !stagedAttachment) || usageBlocked"
                class="w-9 h-9 shrink-0 self-end flex items-center justify-center rounded-full bg-teal-700 text-white disabled:opacity-40 disabled:cursor-not-allowed">
          <svg viewBox="0 0 24 24" class="w-4 h-4" fill="currentColor"><path d="M3 20l18-8L3 4v6l12 2-12 2z"/></svg>
        </button>
      </div>
    </div>
  `,
};
