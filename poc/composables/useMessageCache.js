// Client-side lazy message cache (front-end only, no backend changes).
//
// Goal: opening a chat you've already visited should NOT re-download its
// history. We persist the newest page of messages per chat to localStorage
// and, on the next open, render straight from that snapshot instead of
// hitting GET /chats/{id}/messages.
//
// Freshness is maintained by write-through: useChats watches `messages` for
// the active chat and calls saveChatMessages() on every change, so live
// events (new_message / edit / delete / optimistic send) keep the cache
// current. Older pages fetched via "load older" are intentionally not
// persisted - the cache only ever holds the most recent page.
function useMessageCache(ctx) {
  const KEY_PREFIX = 'linka_msgcache_';
  // Bump when the stored shape changes so stale entries are ignored.
  const SCHEMA = 1;
  // Cap what we keep per chat so localStorage can't grow unbounded.
  const MAX_CACHED = 60;

  function keyFor(chatId) {
    const uid = (ctx.currentUser.value && ctx.currentUser.value.id) || 'anon';
    return `${KEY_PREFIX}${uid}_${chatId}`;
  }

  // Returns the cached messages array (oldest-first, same order the UI wants)
  // or null on a miss / parse error / schema mismatch.
  function loadChatMessages(chatId) {
    try {
      const raw = localStorage.getItem(keyFor(chatId));
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.v !== SCHEMA || !Array.isArray(parsed.messages)) return null;
      return parsed.messages;
    } catch (err) {
      ctx.logError('message cache read failed:', err.message);
      return null;
    }
  }

  // Persist the newest slice of `list` (oldest-first) for this chat.
  function saveChatMessages(chatId, list) {
    if (!chatId || !Array.isArray(list)) return;
    try {
      const messages = list.slice(-MAX_CACHED);
      localStorage.setItem(keyFor(chatId), JSON.stringify({ v: SCHEMA, messages }));
    } catch (err) {
      // Quota errors etc. are non-fatal - we just fall back to the network.
      ctx.logError('message cache write failed:', err.message);
    }
  }

  function clearChatMessages(chatId) {
    try { localStorage.removeItem(keyFor(chatId)); } catch (_) {}
  }

  // Drop every cached chat (called on logout).
  function clearAllMessageCache() {
    try {
      const doomed = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith(KEY_PREFIX)) doomed.push(k);
      }
      doomed.forEach((k) => localStorage.removeItem(k));
    } catch (_) {}
  }

  return { loadChatMessages, saveChatMessages, clearChatMessages, clearAllMessageCache };
}
