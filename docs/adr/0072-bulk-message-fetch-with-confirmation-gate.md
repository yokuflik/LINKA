# 0072 - Bulk Message Fetch With a Hard Confirmation Gate

Status: Accepted
Date: 2026-09-27

## Context

The owner wants their agent (specifically the Supervisor, ADR 0062) to be able
to summarize a whole chat's history - e.g. "summarize the standup group" - not
just the last page `read_history` returns. `read_history`
(`modules/agents/tools/execution.py`) is capped at
`READ_HISTORY_PAGE_SIZE = 20` per call, and a turn is capped at
`AGENT_TURN_MAX_TOOL_ROUNDTRIPS` (12) round-trips, so paging through a long
chat one page at a time is not viable for a real summarization request.

Token math is not the constraint: Linka's messages are short (chat-style),
and even 1000 messages serialize to roughly 30-75k tokens - well inside both
Gemini's context window and `AGENT_TOKEN_BUDGET_5H` (1,000,000). The actual
gap is tool plumbing (the pagination cap), not model capacity.

But a single tool call that can pull up to 1000 messages is also a real cost
(one large Gemini input, done on the owner's own token budget) that should
not fire silently on every summarization request without the owner's
awareness, and must have a hard ceiling so a chat far larger than that isn't
silently truncated and passed off as complete.

## Decision

Two new execution-mode tools, added to `EXECUTION_TOOL_HANDLERS` /
`TOOL_SCHEMAS` (`modules/agents/tools/execution.py`,
`modules/agents/tools/schemas.py`) - automatically unioned into Supervisor
and Builder per ADR 0062's `**EXECUTION_TOOL_HANDLERS` pattern, no new wiring
needed there:

- `count_messages_in_range(chat_id, start_at?, end_at?)` - cheap
  `SELECT COUNT(*)`, no LLM cost. A new `count_messages_in_range` CRUD helper
  in `modules/messaging/crud.py`, sibling to the existing cursor-based
  `count_unread_messages`, reusing `_tool_search_messages`'s
  `_parse_tool_datetime` date-parsing convention (ADR 0068/0070).
- `bulk_fetch_messages(chat_id, start_at?, end_at?)` - fetches up to
  `AGENT_BULK_FETCH_MAX_MESSAGES` (1000, fixed) messages in one call, in a
  compact form (no `has_more`/pagination envelope - this is a single-shot
  fetch, not a page).

Fixed rule, prompt-taught (`SUPERVISOR_PROMPT`,
`modules/agents/builder_flow.py`) but **hard-enforced in
`bulk_fetch_messages` itself**, not just in the prompt:

1. The model must call `count_messages_in_range` before `bulk_fetch_messages`
   for the same chat/range.
2. If the count exceeds 1000, the model must not attempt the fetch at all -
   it tells the owner the chat is too large and to narrow by date range or
   message count. `bulk_fetch_messages` independently re-counts server-side
   and raises `ToolDeniedError` if the true count exceeds the cap, regardless
   of what the model believes or claims.
3. If the count is <=1000, the model must not call `bulk_fetch_messages`
   directly in the same turn. It must first stash the pending request and end
   the turn asking the owner to confirm ("this pulls in N messages, an
   expensive operation - confirm?"). Only a **subsequent** turn, seeded by
   the owner's own reply, may call `bulk_fetch_messages` - and only for
   exactly the chat_id/range that was stashed.

### Persistence: `Agent.pending_confirmation`

New nullable JSONB column on `Agent` (no migration, per the project's
no-migrations convention - additive column with a server default of `NULL`),
following the same shape/expiry idiom as `paused_chat_ids` (ADR 0054):

```
{"tool": "bulk_fetch_messages", "chat_id": "...", "start_at": "...",
 "end_at": "...", "count": N, "created_at": "<iso>"}
```

Single dict, not a list - only the Supervisor's own owner-conversation can
have a pending confirmation at a time, unlike `paused_chat_ids` which spans
many customer chats. Expires after 1 hour (checked lazily on read, same
lazy-expiry style as `paused_chat_ids`/`on_ephemeral_task`) - if the owner
doesn't respond, the offer silently lapses rather than firing on a stale,
possibly-unrelated later message.

A small `stash_pending_bulk_fetch` internal helper (not a model-facing tool -
called directly by `count_messages_in_range`'s handler when it decides to
ask for confirmation) writes this field; `bulk_fetch_messages` reads and
clears it as its hard gate.

### Turn boundary, not an in-loop wait

There is no primitive in `_run_turn` (`modules/agents/invoke_worker.py`) to
suspend a tool-call loop mid-turn and resume it later when a *new* owner
message arrives - every tool call in the round-trip loop is fire-and-continue
within the same Python call, and a fresh inbound owner message always starts
a brand-new `_run_turn` from a freshly-built transcript. So "wait for
confirmation" is implemented as: the first turn ends normally (an ordinary
`return`, posting the question as its reply) instead of trying to block:
`Agent.pending_confirmation` is what carries the state across that boundary,
not the turn/loop itself. The next turn (triggered by the owner's reply)
re-derives its system prompt in `invoke_worker.py` the same way `builder_state`
already does every round-trip: if `agent.pending_confirmation` is set and not
expired, a short reminder of the stashed request is appended to the system
prompt, telling the model to call `bulk_fetch_messages` if the owner just
confirmed, or to clear the pending confirmation (a lightweight tool-free
path - clearing happens automatically the next time `count_messages_in_range`
or `bulk_fetch_messages` runs for a different chat/range) if they declined or
moved on.

### Hard enforcement, server-side

Per explicit owner instruction: `bulk_fetch_messages`'s handler independently
verifies, before touching the DB for real data:

- `agent.pending_confirmation` exists, its `tool` is `bulk_fetch_messages`,
  it has not expired, and its `chat_id`/`start_at`/`end_at` match the call's
  actual arguments exactly - a mismatched or absent pending confirmation is a
  `ToolDeniedError`, never a soft warning.
- A fresh `count_messages_in_range`-equivalent count at call time is still
  `<= AGENT_BULK_FETCH_MAX_MESSAGES` - closes the race where messages arrived
  between the confirmation and the fetch.

This mirrors the project's existing restriction-enforcement posture (ADR
0045: "enforcement happens server-side in execute_tool_call, never left to
the system prompt alone") - the prompt teaches the *courtesy* (ask first,
explain the cost), the handler enforces the *rule* (cannot run unconfirmed,
cannot exceed the cap) regardless of what the model does.

## Consequences

- No schema migration: one new nullable JSONB column
  (`Agent.pending_confirmation`, default `NULL`), one new CRUD count helper.
- Two new execution-mode tools, reachable from every execution-mode chat and
  (via the existing ADR 0062 union) the Supervisor/Builder config chats.
- A summarization request over a chat with <=1000 total messages costs the
  owner exactly one extra turn (the confirmation round-trip) before the real
  fetch - by design, not an accident of the turn-boundary constraint.
- A chat with >1000 messages cannot be bulk-summarized at all yet; the owner
  must narrow by date range or count. No "confirm anyway, truncate the rest"
  escape hatch in v1 - matches the owner's explicit ask for a hard ceiling,
  not a soft warning.
- `pending_confirmation` is scoped to the Supervisor/owner-agent chat only
  (execution-mode chats have no owner to confirm with mid-conversation); the
  two new tools are still callable from a real execution-mode chat, but there
  the confirm-then-fetch flow doesn't make sense with a customer on the other
  end - that usage isn't restricted by this ADR, but isn't the intended path
  either (summarizing a customer conversation for the owner happens via the
  Supervisor chat, using the customer chat's chat_id as the target).
