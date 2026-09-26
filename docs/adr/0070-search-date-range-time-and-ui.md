# 0070 - Search Date-Range: Time-of-Day Precision + Calendar-Icon UI

Status: Accepted
Date: 2026-09-26

## Context

ADR 0068 added an optional `start_date`/`end_date` (calendar-date only) range
to keyword and semantic search, exposed in the PoC as two always-visible
`<input type="date">` fields under the tab switcher. Two problems surfaced
once used: (1) calendar-date precision isn't enough - a user wants to narrow
to a specific hour, not just a day; (2) the always-visible pair of date boxes
took permanent space in the modal for a filter most searches don't need.

## Decision

1. **Full timestamp precision, not just calendar dates.** Every parameter
   involved is renamed `start_date`/`end_date` → `start_at`/`end_at` and
   retyped `date` → `datetime` end-to-end: `modules/search/crud.py`'s
   `_date_range_conditions`, `modules/search/service.py`'s
   `search_in_chat`/`search_global`/`stream_search`/`validate_date_range`,
   `modules/search/router.py`'s three routes, `modules/vector_search/crud.py`'s
   `semantic_search_messages`, `modules/vector_search/service.py`'s
   `semantic_search`, and `modules/vector_search/router.py`'s `/semantic`
   route. FastAPI/Pydantic parses both a bare `YYYY-MM-DD` and a full
   `YYYY-MM-DDTHH:MM:SS` into a `datetime` query param natively - no custom
   parsing needed at the REST layer.
2. **Agent tool** (`_tool_search_messages`/`_tool_search_semantic` in
   `modules/agents/tools/execution.py`) keeps the argument names
   `start_date`/`end_date` in its Gemini-facing schema (an LLM naturally
   supplies either shape under that name) but parses them with a new
   `_parse_tool_datetime(raw, *, is_end)` that accepts both a bare date and a
   full ISO datetime. A bare date defaults to midnight for a start bound and
   23:59:59.999999 for an end bound (so "search March" as
   start_date=2025-03-01/end_date=2025-03-31 remains a non-empty range) -
   the old `_parse_tool_date` is removed.
3. **Frontend: calendar icon inside the search box, not two permanent date
   fields.** `SearchModal.js` gets a small calendar-icon button anchored at
   the right edge of the search input (absolute-positioned inside the
   relative wrapper, mirroring the magnifying-glass icon on the left). The
   two date/time pickers (`<input type="datetime-local">`) live in a popover
   below the search box, toggled open/closed by that icon - hidden by
   default, so the common case (no date filter) keeps the modal exactly as
   compact as before. The icon fills in teal when a range is active
   (`dateRangeActive` in `useSearch.js`), giving an at-a-glance filter
   indicator even with the popover closed.
4. **Requested defaults**: the first time the popover is opened in a given
   modal session (both dates still empty), `useSearch.js` pre-fills
   `start = 2025-01-01T00:00` and `end = <right now>` (computed fresh at
   open time, not once at page load) - a sensible "everything so far" range
   the user can then narrow. Reopening after the user has already set/cleared
   dates never overwrites their choice.
5. **`useSearch.js` still sends one shared range for both tabs** (Exact and
   Related search the same underlying messages) - only the query-param names
   changed (`start_at`/`end_at`) and the conversion is now
   `new Date(datetimeLocalString).toISOString()` instead of passing the bare
   `YYYY-MM-DD` string through unchanged.

## Consequences

- No DB/schema change - `created_at` was always full-precision; this only
  changes what precision the filter itself can express.
- `SearchLimits`/`VectorSearchLimits` unchanged, no new rate-limit bucket
  (same as ADR 0068).
- Superseding ADR 0068's date-only field names in every one of these
  signatures is a breaking rename for any other caller of these functions -
  none exist outside this codebase (no public API contract to preserve).
