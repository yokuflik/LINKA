// Message search (ADR 0040) - the magnifying-glass modal opened from
// AppHeader (global, all chats) or from ChatHeader (scoped to the open chat).
// Cursor-paginated GET /search/messages or GET /chats/{id}/messages/search,
// "smart" loading: only the first page fetches on open/type/Enter; further
// pages fetch lazily as the results list is scrolled near its bottom (no
// upfront full-history load).
//
// Needs from ctx (call-time): apiFetch, log, logError, friendlyError,
// showErrorToast, jumpToMessage (useChatOpen), chats (to resolve a hit's
// chat_id to a display row - a global hit is always a chat the user is a
// current member of, so it's already in the sidebar list).
//
// Global `useSearch(ctx)` factory.
function useSearch(ctx) {
  const { ref } = Vue;

  const SEARCH_DEBOUNCE_MS = 1500;
  const SEARCH_MIN_LEN = 2;

  const showSearchModal = ref(false);
  const searchQuery = ref('');
  const searchBusy = ref(false);       // first-page fetch in flight
  const searchMoreBusy = ref(false);   // next-page fetch in flight
  const searchError = ref('');
  const searchResults = ref([]);
  const searchHasMore = ref(false);
  const searchChatId = ref(null);      // null = global search; set = scoped to one chat

  let searchCursor = null;
  let searchTimer = null;
  let searchSeq = 0; // guards a stale in-flight response from clobbering a newer query

  function searchEndpoint(cursorPart) {
    return searchChatId.value
      ? `/chats/${searchChatId.value}/messages/search?q=${encodeURIComponent(searchQuery.value.trim())}${cursorPart}`
      : `/search/messages?q=${encodeURIComponent(searchQuery.value.trim())}${cursorPart}`;
  }

  // Pass a chatId to scope the search to that chat only; omit/null for global.
  function openSearchModal(chatId) {
    searchChatId.value = chatId || null;
    showSearchModal.value = true;
  }
  function closeSearchModal() {
    showSearchModal.value = false;
    searchChatId.value = null;
    searchQuery.value = '';
    resetSearchResults();
    if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
  }
  function resetSearchResults() {
    searchResults.value = [];
    searchHasMore.value = false;
    searchCursor = null;
    searchError.value = '';
  }

  async function runSearch() {
    const q = searchQuery.value.trim();
    if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
    if (q.length < SEARCH_MIN_LEN) {
      resetSearchResults();
      return;
    }
    const seq = ++searchSeq;
    searchBusy.value = true;
    searchError.value = '';
    try {
      const body = await ctx.apiFetch(searchEndpoint(''));
      if (seq !== searchSeq) return; // superseded by a newer query
      searchResults.value = body.results || [];
      searchCursor = body.next_cursor || null;
      searchHasMore.value = !!body.has_more;
    } catch (err) {
      if (seq !== searchSeq) return;
      ctx.logError('search failed for', q, err.message);
      searchError.value = ctx.friendlyError(err, "Couldn't search right now.");
      searchResults.value = [];
      searchHasMore.value = false;
    } finally {
      if (seq === searchSeq) searchBusy.value = false;
    }
  }

  // Debounced as-you-type search: fires SEARCH_DEBOUNCE_MS after the user
  // stops typing. Enter / the Search button bypass the debounce (immediate).
  function onSearchInput() {
    if (searchTimer) clearTimeout(searchTimer);
    const q = searchQuery.value.trim();
    if (q.length < SEARCH_MIN_LEN) {
      resetSearchResults();
      return;
    }
    searchTimer = setTimeout(() => { searchTimer = null; runSearch(); }, SEARCH_DEBOUNCE_MS);
  }

  // "Smart" pagination: only called when the results list is scrolled near
  // its bottom (see SearchModal.js onResultsScroll) - never loads ahead.
  async function loadMoreSearchResults() {
    if (searchMoreBusy.value || !searchHasMore.value || !searchCursor) return;
    const q = searchQuery.value.trim();
    if (q.length < SEARCH_MIN_LEN) return;
    const seq = searchSeq;
    searchMoreBusy.value = true;
    try {
      const body = await ctx.apiFetch(searchEndpoint(`&cursor=${encodeURIComponent(searchCursor)}`));
      if (seq !== searchSeq) return;
      searchResults.value = searchResults.value.concat(body.results || []);
      searchCursor = body.next_cursor || null;
      searchHasMore.value = !!body.has_more;
    } catch (err) {
      if (seq !== searchSeq) return;
      ctx.showErrorToast(ctx.friendlyError(err, "Couldn't load more results."));
    } finally {
      if (seq === searchSeq) searchMoreBusy.value = false;
    }
  }

  // Tapping a result: close the search modal and open its chat at that message.
  async function openSearchResult(result) {
    closeSearchModal();
    await ctx.jumpToMessage(result.chat_id, result.id);
  }

  return {
    showSearchModal, searchQuery, searchBusy, searchMoreBusy, searchError,
    searchResults, searchHasMore, searchChatId,
    openSearchModal, closeSearchModal, onSearchInput, runSearch,
    loadMoreSearchResults, openSearchResult,
  };
}
