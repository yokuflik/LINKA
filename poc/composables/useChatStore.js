// Chat domain — SINGLE SOURCE OF TRUTH for shared chat state (ADR 0035).
//
// `LinkaChatStore` is a module-level singleton, built once at script-load. It
// owns every reactive ref the chat list + message pane touch, the unread-badge
// state, the buffered-live-message map, plus the pure derived helpers that need
// no I/O: the `sortChats` comparator, the tick-based mute helpers, and the small
// status/role formatters. No fetching, no WS.
//
// Legacy path: the `useChats` facade still does
// `Object.assign(ctx, useChatStore(ctx))`, so every store member also appears on
// the shared `ctx` for the composables not yet migrated off it — the SAME ref
// objects, so reactivity and existing consumers are unaffected.
//
// New path: `useWsRouter` and `MessageList` read/write state through
// `LinkaChatStore.*` directly (imported / injected), never through `ctx`.
const LinkaChatStore = (function buildChatStore() {
  const { ref } = Vue;

  // ---------------------------------------------------------------
  // Chats + message pane
  // ---------------------------------------------------------------
  const chats = ref([]);
  const activeChatId = ref(null);
  // A private chat the user has "opened" from the sidebar but not yet
  // committed: nothing is created server-side until the first message is
  // sent. Shape: { otherUserId, phone, user } or null. Leaving the pane
  // (selecting any real chat, or closing it) discards it silently.
  const draftChat = ref(null);
  const messages = ref([]);
  const messageInput = ref('');
  const chatsError = ref('');
  const messagesError = ref('');
  // True when the newest-page history fetch failed (server down / no network)
  // AND we have nothing cached to show - drives the "waiting for connection"
  // state in MessageList instead of a misleading "No messages here" + a raw
  // "Failed to fetch" banner over the composer.
  const messagesConnectionError = ref(false);
  // True while the initial history fetch for the open chat is in flight (or
  // retrying on a dead connection) and there's nothing on screen yet - drives
  // the "Loading messages…" spinner instead of a premature "No messages here".
  const messagesLoading = ref(false);
  const messagesEl = ref(null);
  // Infinite-scroll-up pagination state.
  const hasMoreMessages = ref(false);
  const loadingOlderMessages = ref(false);
  // True while a "load older" page is stuck retrying on a dead connection -
  // keeps the top spinner up and tells the user we're actively retrying.
  const olderMessagesRetrying = ref(false);
  const MESSAGE_PAGE_SIZE = 50;
  // Set by useChatOpen.jumpToMessage (search "jump to result") right after the
  // target message's context window has been rendered; MessageList watches
  // this to scroll to + briefly highlight the row, then clears it.
  const pendingHighlightId = ref(null);
  // How close to the top (in messages still above the viewport) we get before
  // pulling the next older page.
  const LOAD_OLDER_THRESHOLD = 20;

  // chat_id -> the other participant's phone number, for private chats.
  const privateChatTitles = ref({});
  // chat_id -> the other participant's user id, for private chats only - needed
  // (not just the phone number) to send subscribe_presence.
  const privateChatOtherUserId = ref({});
  // user_id -> that user's UserOut, so a bubble can show who sent it by name.
  const userById = ref({});
  // chat_id -> that chat's member list, straight from /chats/{id}/members.
  const groupChatMembers = ref({});

  // How many member names fit in the header before the rest collapse to "...".
  const MAX_VISIBLE_MEMBERS = 8;

  // 1=Member, 2=Admin, 3=Owner (see database/models/participant.py).
  const ROLE_LABELS = { 1: '', 2: 'Admin', 3: 'Owner' };
  function roleLabel(role) { return ROLE_LABELS[role] || ''; }

  // MessageStatus from the server: 1=sent, 2=delivered, 3=read. Only ever
  // rendered for your own messages.
  function statusTickSymbol(status) { return status >= 2 ? '✓✓' : '✓'; }
  function statusTickClass(status) { return status === 3 ? 'text-sky-500' : 'text-slate-400'; }

  // ---------------------------------------------------------------
  // Unread-count badge (WhatsApp-style number on each sidebar chat) — ADR 0035
  // ---------------------------------------------------------------
  // Seeded from the server on every loadChats() call - GET /chats returns each
  // chat's real unread_count, so a fresh login/reload shows the true count.
  // Then kept current live by useWsRouter: incremented for a real message (not
  // a system message, not our own) arriving for a chat that isn't the active
  // one, and reset to 0 the moment that chat is opened (see selectChat).
  const unreadCountByChatId = ref({});

  function bumpUnreadCount(chatId) {
    unreadCountByChatId.value = {
      ...unreadCountByChatId.value,
      [chatId]: (unreadCountByChatId.value[chatId] || 0) + 1,
    };
  }

  function clearUnreadCount(chatId) {
    if (!unreadCountByChatId.value[chatId]) return;
    const next = { ...unreadCountByChatId.value };
    delete next[chatId];
    unreadCountByChatId.value = next;
  }

  // ---------------------------------------------------------------
  // Buffered live messages for a chat that wasn't open at the time — ADR 0035
  // ---------------------------------------------------------------
  // selectChat() drains the matching entry and merges it with the history
  // fetch (dedupe by message_id) so opening a chat mid-burst doesn't lose the
  // messages that landed before you switched to it. chat_id -> [msg,...].
  const pendingChatMessages = new Map();

  function bufferMessage(chatId, row) {
    const buf = pendingChatMessages.get(chatId) || [];
    buf.push(row);
    // Cap so a chat that's never opened can't grow this without bound.
    if (buf.length > 200) buf.shift();
    pendingChatMessages.set(chatId, buf);
  }

  function takeBufferedMessages(chatId) {
    const buf = pendingChatMessages.get(chatId) || [];
    pendingChatMessages.delete(chatId);
    return buf;
  }

  // ---------------------------------------------------------------
  // Chat-list ordering
  // ---------------------------------------------------------------
  // Same ordering the server applies (crud_participant.get_user_chats): pinned
  // chats first, then by last activity. Kept client-side too so an optimistic
  // pin/unpin re-sorts the list without a full reload.
  function sortChats() {
    // Reassign (not in-place .sort()) so every consumer of the `chats` ref -
    // including the ChatSidebar v-for on another device's window - reliably
    // re-renders in the new order.
    chats.value = chats.value.slice().sort((a, b) => {
      if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
      const ta = a.chat.last_message_at || '';
      const tb = b.chat.last_message_at || '';
      if (ta !== tb) return ta < tb ? 1 : -1;
      return a.chat.id < b.chat.id ? 1 : -1;
    });
  }

  // ---------------------------------------------------------------
  // Mute presets + the ticking "now" that invalidates mute state
  // ---------------------------------------------------------------
  // Mute presets are just convenience shortcuts - the server accepts ANY
  // absolute `muted_until`, and the menu also has a free "Custom…" datetime
  // picker (see ChatContextMenu). "always" is a far-future timestamp. ADR 0004.
  const MUTE_PRESETS = [
    { key: '8h', label: '8 hours', ms: 8 * 3600 * 1000 },
    { key: '1d', label: '1 day', ms: 24 * 3600 * 1000 },
    { key: '1w', label: '1 week', ms: 7 * 24 * 3600 * 1000 },
    { key: 'always', label: 'Forever', ms: null },
  ];
  const MUTE_FOREVER_ISO = '9999-12-31T23:59:59Z';

  function presetExpiryIso(presetKey) {
    const opt = MUTE_PRESETS.find((o) => o.key === presetKey);
    if (!opt || opt.ms == null) return MUTE_FOREVER_ISO;
    return new Date(Date.now() + opt.ms).toISOString();
  }

  // A ticking "now" so time-based views (the mute icon) re-render when an
  // expiry passes - nothing else would invalidate isChatMuted() otherwise.
  const nowTick = ref(Date.now());
  setInterval(() => {
    nowTick.value = Date.now();
    // Clear a lapsed local muted_until so the item is genuinely un-muted
    // (also stops mutedUntilLabel showing a past time).
    for (const item of chats.value) {
      if (item.muted_until && new Date(item.muted_until).getTime() <= nowTick.value) {
        item.muted_until = null;
      }
    }
  }, 30 * 1000);

  // True when this item is muted and the expiry is still in the future.
  function isChatMuted(item) {
    return !!item && !!item.muted_until && new Date(item.muted_until).getTime() > nowTick.value;
  }

  // Short "until ..." hint for the menu; '' for an effectively-forever mute.
  function mutedUntilLabel(item) {
    if (!isChatMuted(item)) return '';
    const t = new Date(item.muted_until);
    if (t.getUTCFullYear() >= 9999) return '';
    return t.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  return {
    chats, activeChatId, draftChat, messages, messageInput,
    chatsError, messagesError, messagesConnectionError, messagesLoading, messagesEl,
    hasMoreMessages, loadingOlderMessages, olderMessagesRetrying, pendingHighlightId,
    MESSAGE_PAGE_SIZE, LOAD_OLDER_THRESHOLD, MAX_VISIBLE_MEMBERS,
    privateChatTitles, privateChatOtherUserId, userById, groupChatMembers,
    ROLE_LABELS, roleLabel, statusTickSymbol, statusTickClass,
    sortChats,
    unreadCountByChatId, bumpUnreadCount, clearUnreadCount,
    bufferMessage, takeBufferedMessages,
    MUTE_PRESETS, MUTE_FOREVER_ISO, presetExpiryIso,
    nowTick, isChatMuted, mutedUntilLabel,
  };
})();

// Back-compat shim (ADR 0035): the `useChats` facade still calls this and
// `Object.assign`s the result onto `ctx`, so every store member keeps
// appearing on `ctx` for the composables not yet migrated. Same object
// identity => reactivity and existing consumers are unaffected.
function useChatStore(_ctx) { return LinkaChatStore; }
