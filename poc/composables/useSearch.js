// Message search (ADR 0040 keyword + ADR 0042 semantic) - the magnifying-glass
// modal opened from AppHeader (global, all chats) or from ChatHeader (scoped
// to the open chat). Two tabs share one modal: "Exact" is the cursor-paginated
// GET /search/messages or GET /chats/{id}/messages/search (keyword FTS);
// "Related" is GET /search/semantic (vector similarity, ADR 0042) - a flat
// top-K list, no pagination. Only the active tab fetches; switching tabs
// fetches lazily if that tab hasn't already run for the current query text.
//
// Needs from ctx (call-time): apiFetch, log, logError, friendlyError,
// showErrorToast, jumpToMessage (useChatOpen), chats (to resolve a hit's
// chat_id to a display row - a global hit is always a chat the user is a
// current member of, so it's already in the sidebar list).
//
// Global `useSearch(ctx)` factory.
function useSearch(ctx) {
  const { ref, computed } = Vue;

  const SEARCH_DEBOUNCE_MS = 1500;
  const SEARCH_MIN_LEN = 2;
  const SEMANTIC_LIMIT = 20;

  const showSearchModal = ref(false);
  const searchQuery = ref('');
  const searchChatId = ref(null);      // null = global search; set = scoped to one chat
  const searchTab = ref('exact');      // 'exact' | 'semantic'

  // Exact (keyword FTS) tab state.
  const searchBusy = ref(false);       // first-page fetch in flight
  const searchMoreBusy = ref(false);   // next-page fetch in flight
  const searchError = ref('');
  const searchResults = ref([]);
  const searchHasMore = ref(false);

  // Semantic (vector similarity) tab state - flat list, no cursor.
  const semanticBusy = ref(false);
  const semanticMoreBusy = ref(false);   // "show more results" (expanded=true) fetch in flight
  const semanticError = ref('');
  const semanticResults = ref([]);
  const semanticExpanded = ref(false);   // true once "show more results" has been used for this query

  let searchCursor = null;
  let searchTimer = null;
  let searchSeq = 0;      // guards a stale exact response from clobbering a newer query
  let semanticSeq = 0;    // same, for the semantic tab
  let exactQueriedText = null;     // last query text the exact tab actually fetched
  let semanticQueriedText = null;  // same, for the semantic tab

  function searchEndpoint(cursorPart) {
    return searchChatId.value
      ? `/chats/${searchChatId.value}/messages/search?q=${encodeURIComponent(searchQuery.value.trim())}${cursorPart}`
      : `/search/messages?q=${encodeURIComponent(searchQuery.value.trim())}${cursorPart}`;
  }

  function semanticEndpoint(expanded) {
    const chatPart = searchChatId.value ? `&chat_id=${encodeURIComponent(searchChatId.value)}` : '';
    const expandedPart = expanded ? '&expanded=true' : '';
    return `/search/semantic?q=${encodeURIComponent(searchQuery.value.trim())}&limit=${SEMANTIC_LIMIT}${chatPart}${expandedPart}`;
  }

  // Pass a chatId to scope the search to that chat only; omit/null for global.
  function openSearchModal(chatId) {
    searchChatId.value = chatId || null;
    searchTab.value = 'exact';
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
    exactQueriedText = null;
    semanticResults.value = [];
    semanticError.value = '';
    semanticQueriedText = null;
    semanticExpanded.value = false;
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
      exactQueriedText = q;
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

  // Semantic ("Related") tab - flat top-K list, no cursor/pagination.
  async function runSemanticSearch() {
    const q = searchQuery.value.trim();
    if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
    if (q.length < SEARCH_MIN_LEN) {
      semanticResults.value = [];
      semanticError.value = '';
      semanticQueriedText = null;
      return;
    }
    const seq = ++semanticSeq;
    semanticBusy.value = true;
    semanticError.value = '';
    semanticExpanded.value = false;
    try {
      const body = await ctx.apiFetch(semanticEndpoint(false));
      if (seq !== semanticSeq) return;
      semanticResults.value = body.results || [];
      semanticQueriedText = q;
    } catch (err) {
      if (seq !== semanticSeq) return;
      ctx.logError('semantic search failed for', q, err.message);
      semanticError.value = ctx.friendlyError(err, "Couldn't search right now.");
      semanticResults.value = [];
    } finally {
      if (seq === semanticSeq) semanticBusy.value = false;
    }
  }

  // "Show more results" - re-runs the semantic search with a looser
  // relevance floor (expanded=true) and replaces the list with the wider set
  // (a superset of the default results, same query/backend call shape).
  async function loadMoreSemanticResults() {
    const q = searchQuery.value.trim();
    if (semanticMoreBusy.value || semanticExpanded.value || q.length < SEARCH_MIN_LEN) return;
    const seq = semanticSeq;
    semanticMoreBusy.value = true;
    try {
      const body = await ctx.apiFetch(semanticEndpoint(true));
      if (seq !== semanticSeq) return;
      semanticResults.value = body.results || [];
      semanticExpanded.value = true;
    } catch (err) {
      if (seq !== semanticSeq) return;
      ctx.showErrorToast(ctx.friendlyError(err, "Couldn't load more results."));
    } finally {
      if (seq === semanticSeq) semanticMoreBusy.value = false;
    }
  }

  function runActiveSearch() {
    return searchTab.value === 'semantic' ? runSemanticSearch() : runSearch();
  }

  // Switching tabs fetches lazily - only if that tab hasn't already run for
  // the current query text (so bouncing back and forth doesn't re-fire).
  function switchSearchTab(tab) {
    if (searchTab.value === tab) return;
    searchTab.value = tab;
    const q = searchQuery.value.trim();
    if (q.length < SEARCH_MIN_LEN) return;
    if (tab === 'exact' && exactQueriedText !== q) runSearch();
    else if (tab === 'semantic' && semanticQueriedText !== q) runSemanticSearch();
  }

  // Debounced as-you-type search: fires SEARCH_DEBOUNCE_MS after the user
  // stops typing. Enter / the Search button bypass the debounce (immediate).
  // Only the active tab fetches - the other tab catches up on switchSearchTab.
  function onSearchInput() {
    if (searchTimer) clearTimeout(searchTimer);
    const q = searchQuery.value.trim();
    if (q.length < SEARCH_MIN_LEN) {
      resetSearchResults();
      return;
    }
    searchTimer = setTimeout(() => { searchTimer = null; runActiveSearch(); }, SEARCH_DEBOUNCE_MS);
  }

  // "Smart" pagination: only called when the results list is scrolled near
  // its bottom (see SearchModal.js onResultsScroll) - never loads ahead.
  // No-op on the semantic tab (flat top-K list, no cursor).
  async function loadMoreSearchResults() {
    if (searchTab.value !== 'exact') return;
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

  // Active-tab views the modal actually renders - keeps SearchModal generic
  // (one results list) while each tab keeps its own independent state above.
  const activeResults = computed(() => (searchTab.value === 'semantic' ? semanticResults.value : searchResults.value));
  const activeBusy = computed(() => (searchTab.value === 'semantic' ? semanticBusy.value : searchBusy.value));
  const activeError = computed(() => (searchTab.value === 'semantic' ? semanticError.value : searchError.value));
  const activeHasMore = computed(() => (searchTab.value === 'semantic' ? false : searchHasMore.value));
  // Semantic-only "show more results" affordance: offered once the default
  // (strict) results have loaded and haven't already been expanded.
  const showSemanticExpandButton = computed(
    () => searchTab.value === 'semantic' && !semanticBusy.value && !semanticExpanded.value
      && searchQuery.value.trim().length >= SEARCH_MIN_LEN
  );

  return {
    showSearchModal, searchQuery, searchChatId, searchTab,
    searchBusy, searchMoreBusy, searchError, searchResults, searchHasMore,
    semanticBusy, semanticMoreBusy, semanticError, semanticResults, semanticExpanded,
    activeResults, activeBusy, activeError, activeHasMore, showSemanticExpandButton,
    openSearchModal, closeSearchModal, onSearchInput, runSearch, runSemanticSearch,
    runActiveSearch, switchSearchTab, loadMoreSearchResults, loadMoreSemanticResults, openSearchResult,
  };
}
