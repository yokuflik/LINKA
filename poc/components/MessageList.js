// Scrollable message pane: system-message pills, bubbles (mine/theirs), and
// the "no messages" empty state. Kept as one component (not split further
// into a MessageBubble child) since the v-for body's mine/theirs/system
// branches share the same list and messagesEl ref must stay on this
// scrolling container for scrollMessagesToBottom to keep working unchanged.
const MessageList = {
  props: {
    messages: { type: Array, required: true },
    currentUser: { type: Object, required: true },
    shouldShowSystemMessage: { type: Function, required: true },
    systemMessageText: { type: Function, required: true },
    senderLabel: { type: Function, required: true },
    senderAvatarUrl: { type: Function, required: true },
    statusTickSymbol: { type: Function, required: true },
    statusTickClass: { type: Function, required: true },
    quotedPreviewFor: { type: Function, required: true },
    // (url) => 'portrait' | 'landscape' - resolved frontend-side by the root
    // (probes naturalWidth/Height once per url, defaults 'landscape'). Drives
    // the fixed reserved box below so the bubble has its final height before
    // the image decodes.
    imageOrientation: { type: Function, required: true },
  },
  emits: ['message-contextmenu', 'load-older', 'voice-played'],
  // Exposes the scrollable element so the root's scrollMessagesToBottom()
  // (which needs messagesEl.value.scrollTop/scrollHeight) keeps working
  // unchanged across the component boundary.
  setup(props, { emit }) {
    const messagesEl = Vue.ref(null);
    // On scroll, count how many message rows are still fully above the top of
    // the viewport and hand that to the root - it decides when to page.
    // Only real message rows carry data-row="msg"; sticky day separators are
    // skipped so they don't inflate the paging count.
    function onScroll() {
      const el = messagesEl.value;
      if (!el) return;
      const top = el.scrollTop;
      let rowsAbove = 0;
      for (const child of el.children) {
        if (child.dataset.row !== 'msg') continue;
        if (child.offsetTop + child.offsetHeight < top) rowsAbove++;
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
    const rows = Vue.computed(() => {
      const out = [];
      let lastKey = null;
      for (const m of props.messages) {
        if (!m.created_at) { out.push({ type: 'msg', m }); continue; }
        const k = dayKey(m.created_at);
        if (k !== lastKey) {
          out.push({ type: 'separator', key: 'sep-' + k, label: dayLabel(m.created_at) });
          lastKey = k;
        }
        out.push({ type: 'msg', m });
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

    return {
      messagesEl, onScroll, isBareMedia, rows,
      onTouchStart, onTouchMove, onTouchEnd,
      imageLoaded, markImageLoaded,
    };
  },
  expose: ['messagesEl'],
  template: `
    <div ref="messagesEl" @scroll="onScroll" class="flex-1 overflow-y-auto p-4 space-y-2">
      <template v-for="row in rows" :key="row.type === 'separator' ? row.key : (row.m.id || row.m.client_message_id)">
        <!-- Sticky day separator (WhatsApp-style). data-row is absent so
             onScroll's paging count ignores it. -->
        <div v-if="row.type === 'separator'" class="day-separator flex justify-center">
          <span class="inline-block w-40 text-center px-3 py-1 rounded-full text-[11px] font-medium bg-slate-200 text-slate-600 shadow-sm whitespace-nowrap overflow-hidden text-ellipsis">{{ row.label }}</span>
        </div>
      <template v-else>
      <template v-for="m in [row.m]" :key="m.id || m.client_message_id">
        <!-- System messages (sender_id == null, e.g. "X added Y to the group") -
             centered, small, gray pill, like WhatsApp's own group-event lines.
             shouldShowSystemMessage filters out "role_changed" notices for
             anyone but the actor/target - see chat_service.change_member_role. -->
        <div v-if="m.sender_id == null && shouldShowSystemMessage(m)" data-row="msg" class="flex justify-center">
          <span class="inline-block px-2.5 py-1 rounded-full text-[11px] bg-slate-200 text-slate-600">{{ systemMessageText(m) }}</span>
        </div>
      <div v-else-if="m.sender_id != null" data-row="msg"
           class="max-w-md w-fit flex items-end gap-2"
           :class="m.sender_id === currentUser.id ? 'ml-auto text-right' : ''">
        <Avatar v-if="m.sender_id !== currentUser.id"
                :url="senderAvatarUrl(m.sender_id)" :name="senderLabel(m.sender_id)"
                :colorKey="m.sender_id" sizeClass="w-7 h-7 text-xs"
                class="shrink-0 mb-[18px]" />
        <div class="min-w-0">
        <div class="inline-block text-sm cursor-pointer"
             :class="[
               isBareMedia(m)
                 ? 'p-0 bg-transparent rounded-lg'
                 : (m.sender_id === currentUser.id
                     ? 'px-3 py-2 rounded-2xl bubble-tail bg-teal-700 text-white rounded-br-none bubble-tail-mine'
                     : 'px-3 py-2 rounded-2xl bubble-tail bg-white border border-slate-200 rounded-bl-none bubble-tail-theirs')
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
          <div v-if="m.sender_id !== currentUser.id && !isBareMedia(m)" class="text-[11px] opacity-60 mb-0.5">{{ senderLabel(m.sender_id) }}</div>
          <!-- Quoted reply preview (WhatsApp-style) - only when this message
               is itself a reply (reply_to_message_id set). quotedPreviewFor
               looks the original message up client-side (it's a lookup, not
               a re-render decision, so it stays a plain function prop). -->
          <div v-if="quotedPreviewFor(m)" class="mb-1 px-2 py-1 rounded border-l-4 text-left text-xs"
               :class="m.sender_id === currentUser.id ? 'bg-white/10 border-white/60 text-white/90' : 'bg-slate-100 border-teal-600 text-slate-600'">
            <div class="font-semibold truncate">{{ quotedPreviewFor(m).sender }}</div>
            <div class="truncate opacity-90">{{ quotedPreviewFor(m).snippet }}</div>
          </div>
          <!-- Media attachment (image / video). media_url is a short-lived
               presigned GET attached by the backend to both history and the
               live new_message event. -->
          <!-- Image: fixed reserved box in one of two shapes (portrait /
               landscape). The box has its final size immediately (bg-white
               placeholder), so scrollHeight is right before the <img>
               decodes; the image fades in on load, filling the box
               (object-cover). Two shapes only, by design. -->
          <div v-if="m.media_url && m.type === 2" class="mb-1">
            <div class="relative rounded-lg overflow-hidden bg-white border border-black"
                 :class="imageOrientation(m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]'">
              <!-- Loading spinner over the white placeholder until @load -->
              <div v-if="!imageLoaded[m.media_url]" class="absolute inset-0 flex items-center justify-center">
                <span class="w-6 h-6 rounded-full border-2 border-slate-300 border-t-slate-500 animate-spin"></span>
              </div>
              <img :src="m.media_url" :alt="m.media_name || 'image'" loading="lazy"
                   class="w-full h-full object-cover opacity-0 transition-opacity duration-200"
                   @load="markImageLoaded(m.media_url, $event)" />
            </div>
          </div>
          <!-- Video: same fixed two-shape reserved box as images, so
               scrollHeight is right before metadata loads. -->
          <div v-else-if="m.media_url && m.type === 3" class="mb-1">
            <div class="rounded-lg overflow-hidden bg-black/80 border border-black"
                 :class="imageOrientation(m.media_url) === 'portrait' ? 'w-48 aspect-[3/4]' : 'w-64 aspect-[4/3]'">
              <video :src="m.media_url" controls preload="metadata"
                     class="w-full h-full object-contain"></video>
            </div>
          </div>
          <!-- Voice message (type 4): clean custom player - play/pause toggle
               + progress track + elapsed/total time. -->
          <VoiceMessage v-else-if="m.media_url && m.type === 4"
                        :src="m.media_url"
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
            : ' (edited)' }}
          </template>
        </div>
        <div class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-1"
             :class="m.sender_id === currentUser.id ? 'justify-end' : ''">
          <span>{{ new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}</span>
          <span v-if="m.send_failed" class="text-sm font-bold leading-none text-red-500" title="Send failed">⚠️</span>
          <span v-else-if="m.pending" class="text-sm leading-none text-slate-400" title="Sending…">🕓</span>
          <span v-else-if="m.sender_id === currentUser.id" class="text-sm font-bold leading-none" :class="statusTickClass(m.status)">{{ statusTickSymbol(m.status) }}</span>
        </div>
        </div>
      </div>
      </template>
      </template>
      </template>
      <p v-if="!messages.length" class="h-full flex items-center justify-center text-sm text-slate-400">No messages here</p>
    </div>
  `,
};
