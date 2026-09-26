# 0069 - Agent Semantic-Search Tool

Status: Accepted
Date: 2026-09-26

## Context

The AI agent (ADR 0045/0046) has a `search_messages` execution-mode tool
wrapping the ADR 0040 keyword FTS search (`modules/search/service.py`), but
no equivalent for ADR 0042's semantic (vector) search
(`modules/vector_search/service.semantic_search`). A keyword search can't
answer a fuzzy/paraphrased request like "did anyone mention being unhappy
with the price" when the exact words were never used. ADR 0068 already added
an optional `start_date`/`end_date` range to both search systems and to the
agent's `search_messages` tool - this ADR closes the remaining gap by adding
the missing semantic-search tool with the same date-range support from day
one.

## Decision

1. **New execution-mode tool `search_semantic`**, thin wrapper over
   `vector_search.service.semantic_search`, same ownership/scoping pattern as
   `search_messages`: `user_id=agent.owner_user_id`, membership enforced by
   the existing `participants` JOIN inside `semantic_search_messages` - never
   a bypass. Registered in `EXECUTION_TOOL_HANDLERS`
   (`modules/agents/tools/execution.py`) and `TOOL_SCHEMAS`
   (`modules/agents/tools/schemas.py`), which the ADR 0062 Supervisor/Builder
   union (`*TOOL_SCHEMAS` / `**EXECUTION_TOOL_HANDLERS`) picks up automatically
   - no separate wiring needed for those two builder_states.
2. **Arguments**: `query` (required), `chat_id` (optional, same
   `blocked_read_chat_ids` restriction check as `search_messages`),
   `start_date`/`end_date` (optional ISO `YYYY-MM-DD`, reuses
   `_parse_tool_date` from ADR 0068 - same "bad date -> ToolDeniedError, not a
   500" behavior). No `cursor`/pagination argument: semantic search is a flat
   top-K list (ADR 0042), not cursor-paginated, same as the REST endpoint.
   No `expanded` argument - the tool always uses the default (strict)
   relevance floor; the "show more results" loosened floor is a UI-only
   affordance (ADR 0042 addendum) with no clear agent use case yet.
3. **Result shape mirrors `search_messages`**: sender identity resolved via
   the existing `_resolve_sender_labels` helper (name + phone number, never a
   raw internal id), `chat_id` kept as the one internal id (the model's
   handle for a follow-up `read_history(chat_id=...)` call), `distance`
   included so the model can gauge how loose a match is. Capped at the
   existing `VectorSearchLimits.default_limit` - no new tool-specific cap.
4. **No new rate-limit bucket** - the handler calls
   `vector_search.service.semantic_search` directly (in-process, not via a
   REST hop), which already enforces the Gemini-embedding queue/cache path;
   the agent's own per-turn/per-minute Gemini call budget (ADR 0045/0047)
   already bounds how often a turn can reach this tool.

## Consequences

- The agent now has parity between exact and semantic search, both
  date-range-filterable, for schedule-fired turns and direct owner commands
  (Supervisor, ADR 0062) alike.
- `search_knowledge`/knowledge-base RAG tools (`get_knowledge_index`/
  `fetch_chunk`) are untouched - this is message search, not document search.
