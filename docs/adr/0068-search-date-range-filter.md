# 0068 - Date-Range Filter for Keyword and Semantic Search

Status: Accepted
Date: 2026-09-26

## Context

Message search (ADR 0040 keyword FTS, ADR 0042 semantic) has no way to
restrict results to a time window. A user searching a long-lived chat (or
globally) for something they remember discussing "in March" has no filter
but the query text itself, and the same gap exists for the AI agent's
`search_messages` tool (ADR 0046 decision 3) when asked something like
"what did we agree on last week".

## Decision

1. **Optional `start_date` / `end_date`, ISO calendar dates (`YYYY-MM-DD`),
   inclusive on both ends.** Not datetimes - the UI offers date pickers, not
   time pickers, and the extra precision isn't needed for this use case.
   `end_date` is treated as the end of that day (23:59:59.999999) so a
   same-day range is a valid non-empty window.
2. **Filtered on `messages.created_at`, not the snowflake id.** Cursor-based
   pagination (`before_id`) already narrows by id; the date range is an
   independent predicate applied in addition to it, not a replacement for the
   id-based partition-pruning skew trick already in `crud.py`. Both bounds
   are plain `created_at >=` / `created_at <=` comparisons - no new
   partition-pruning logic needed since `created_at` is the partition key
   itself.
3. **Applies to all four keyword-search entry points** (`search_chat_messages`,
   `search_global_messages`, `stream_global_messages` and the two routers that
   front them) **and to semantic search** (`semantic_search_messages`).
   Threaded through `service.py` as two new optional params on
   `search_in_chat` / `search_global` / `stream_search` / `semantic_search`,
   validated once in each service function (`start_date > end_date` -> the
   existing `SearchQueryTooShortError`/`VectorSearchQueryTooShortError` 422
   path is reused for "bad range" rather than adding a new error type).
4. **Agent tool parity**: `search_messages` (execution-mode) gains the same
   two optional string arguments, passed straight through to
   `search_service.search_in_chat` / `search_global`. No new semantic-search
   agent tool exists yet (out of scope - only `search_messages` is wired to
   the agent today), so nothing to extend there beyond the one tool.
5. **Frontend**: `SearchModal.js` gets two native `<input type="date">`
   pickers, shared (not per-tab) state in `useSearch.js` since both the
   Exact and Related tabs search the same underlying messages - switching
   tabs keeps the same date range. Changing either date re-runs the active
   tab's search immediately (bypasses the debounce, like Enter/the Search
   button) and invalidates the other tab's cached query text so it refetches
   on switch.
6. **No new rate-limit bucket** - date filtering only narrows an existing
   query's WHERE clause, doesn't add a new kind of DB work.

## Consequences

- `SearchLimits`/`VectorSearchLimits` unchanged - no new tunable needed, this
  is unbounded date input validated for ordering only.
- `messages_around` (jump-to-context) and the agent's knowledge-base tools
  are untouched - date range is a *search* filter, not a history-browsing
  one.
