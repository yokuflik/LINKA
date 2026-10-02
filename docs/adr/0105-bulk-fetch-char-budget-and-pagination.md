# ADR 0105 — `bulk_fetch_messages`: per-message + total character budget, cursor pagination

Status: Accepted
Date: 2026-10-03

## Context

ADR 0072 lets `bulk_fetch_messages` return up to 1000 messages in one tool
result. Nothing bounds the *size* of that result: message `content` is passed
through untouched and the whole result is appended to the turn's `contents`,
which is re-sent on every later round-trip (up to 8). A chat of long messages
can therefore put hundreds of thousands of tokens into the window and burn the
5h/7d token budgets (ADR 0059, usage recorded after the fact, never gating).
Every other read tool is already bounded (`read_history` 20/50 messages with
`has_more`; `search_knowledge_semantic` 5–20 chunks of ≤1500 chars, chunked at
ingestion).

## Decision

Server-side truncation in the handler, same `has_more` contract as
`read_history` (ADR 0067). No extra Gemini call (map-reduce summarisation was
considered and deferred — more calls, more moving parts).

- `AGENT_BULK_FETCH_MESSAGE_MAX_CHARS` (default 1000): a longer message's
  content is cut and suffixed `…[truncated N chars]`.
- `AGENT_BULK_FETCH_MAX_CHARS` (default 60000): messages (oldest first) are
  accumulated until adding the next would exceed the total, always returning at
  least one. Remaining messages → `has_more=true` + `next_after_message_id`.
- New optional arg `after_message_id`: continues from that cursor within the
  *same* confirmed range. `get_messages_in_range` gains `after_id`.
- Confirmation gate (ADR 0072) is unchanged: `pending_confirmation` is cleared
  only on the final page (`has_more=false`), so paging a confirmed range needs
  no second owner confirmation, and every page still re-verifies match + count
  server-side. A different range still needs a fresh confirm.
- Tool description tells the model to say the summary is partial and to keep
  fetching with `after_message_id` while `has_more` is true.

## Consequences

- A single result is bounded (~60k chars ≈ 15–20k tokens) regardless of chat.
- Whole-chat summaries of big chats take several round-trips (each re-sends
  prior pages); still bounded by the 8-round-trip cap, after which the existing
  cap notice/outcome judge applies.
- No schema change, no migration.
