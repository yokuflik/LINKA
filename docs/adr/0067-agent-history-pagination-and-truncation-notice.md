# 0067 - Agent History Pagination and Truncation Notice

Status: Accepted
Date: 2026-09-26

## Context

`read_history` (`modules/agents/tools/execution.py::_tool_read_history`) is
hardcoded to `get_message_history(..., limit=20)` with no `before_id`, and
`search_messages` (`_tool_search_messages`) is hardcoded to `limit=10` with
`cursor=None`. Neither tool tells the model whether more results exist beyond
what was returned.

When a customer or the owner asks about a long history ("what did we discuss
over the last month", "find everything about the contract"), the model has no
way to know it only saw a slice - it can silently answer as if 20 messages or
10 search hits were the whole conversation, which is misleading and, at
larger scale, would require reading an unbounded amount of history into a
single Gemini turn (context bloat, cost, and the existing per-turn token
budget from ADR 0059).

## Decision

1. **Caps stay hard, application-only, unchanged in size** - `read_history`
   stays capped at 20 messages/call, `search_messages` at 10 results/call.
   This ADR does not raise or remove either cap; it makes them page-able and
   visible.
2. **Real pagination, not a bigger cap**: both tools gain an optional input
   parameter for the model to request the next slice explicitly -
   `read_history` gets `before_id` (mirrors the existing REST
   `get_message_history` cursor), `search_messages` gets `cursor` (mirrors
   the existing ADR 0040 `SearchResponseOut.next_cursor`). The model asks for
   more the same way a paginating client would; there is no new "give me
   everything" mode.
3. **Explicit truncation flag in the tool result**: both tool results add
   `has_more: bool` and, where the model would need it to page further,
   `next_before_id` / `next_cursor`. `read_history` derives `has_more` by
   fetching `limit + 1` rows and trimming the extra one (cheap, no separate
   count query, same trick used elsewhere in the codebase for cursors).
   `search_messages` already gets `has_more`/`next_cursor` for free from
   `SearchResponseOut` - it was just being discarded.
4. **Prompt-level rule, not just a data field**: `personas.py::CHAT_STYLE_RULES`
   (execution-mode) and `builder_flow.py::STYLE_RULES` (config-mode) both get
   a new instruction: whenever a `read_history`/`search_messages` result has
   `has_more: true`, the model must not imply it has seen the full history -
   it should say plainly that there's more than it can pull in one go and
   offer to go further in parts (e.g. by date range or topic), rather than
   silently answering as if the slice were complete. This is soft guidance
   (like the rest of `CHAT_STYLE_RULES`), not a new enforced restriction -
   there's no way to force a model to disclose something at the code level
   short of refusing to answer, which is not the goal here.

## Rejected alternatives

- **Raise the caps instead**: pushes more tokens into every turn and still
  hits a wall eventually for a genuinely long chat; doesn't solve the
  underlying "silently incomplete" problem, only delays it.
- **Auto-loop the tool call server-side until exhausted**: would blow past
  the ADR 0047 8-round-trip-per-turn cap and the ADR 0059 token budget for a
  single user request; the model deciding whether/when to page is cheaper
  and keeps the existing budgets meaningful.
- **A dedicated "summarize entire history" tool**: bigger scope (would need
  its own chunking/map-reduce pass), not needed to fix the immediate
  silently-incomplete-answer problem; can be reconsidered later if this
  turns out to be common.

## Consequences

- No schema/migration change - both tools' extra fields are plain dict keys.
- `read_history`'s extra `limit + 1` fetch is one more row per call, no new
  query shape.
- `search_messages` now threads through `cursor` instead of hardcoding
  `None`, so a second call can legitimately continue a prior page.
- Existing behavior for a short chat/small result set is unchanged
  (`has_more: false`, nothing to page).
