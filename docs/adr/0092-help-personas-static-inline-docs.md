# 0092 - Help personas get static inline docs instead of a knowledge-base lookup

Status: Accepted

Supersedes: ADR 0084 (`0084-help-personas-knowledge-base-access.md`)

## Context

ADR 0084 gave `HELP_GENERAL`/`HELP_BUILDING` three read-only tools
(`search_knowledge_semantic`, `get_knowledge_index`, `fetch_chunk`) to answer
from the agent's own per-`agent_id` knowledge base
(`agent_knowledge_documents`/`agent_knowledge_chunks`, ADR 0046/0078).

In practice this does not work for a Help persona's actual job:

- **Empty by default.** The per-agent knowledge base is populated only by the
  owner manually calling `save_knowledge_from_text` or uploading a file
  (ADR 0046/0078). A brand-new agent has zero documents, so
  `search_knowledge_semantic`/`get_knowledge_index` return nothing and the
  Help persona must say "I don't know" about Linka itself - confirmed live:
  the only documents that exist in the dev database today are computer-catalog
  PDFs seeded for a `sales_agent` demo, not Linka product documentation.
- **Wrong scope.** What a Help persona needs is fixed, identical reference
  material about the Linka platform (how the app works) and about building an
  agent (how the builder interview/triggers/knowledge base/escalation work) -
  the same content for every owner, not something that varies per agent or
  that an owner is expected to author themselves.
- **Wrong lifecycle.** `POST /agents/me/reset` (ADR 0050) explicitly deletes
  the knowledge base. Anything seeded into it would be wiped on reset, even
  though Help's reference material has nothing to do with an individual
  owner's agent configuration.
- **Unnecessary cost/latency.** A vector-search round trip (embedding the
  query, ANN lookup, a further `fetch_chunk` call) is pure overhead for a
  small, fixed corpus (~110 lines total across both docs) that comfortably
  fits inline in a system prompt.

## Decision

Two static markdown files, committed to the repo, are the single source of
truth for Help reference content:

- `docs/agent_knowledge/linka_general_help.md` - general Linka platform usage
  (chats, media, search, scheduled messages, privacy, etc.)
- `docs/agent_knowledge/linka_agent_building_help.md` - building/configuring
  an agent (triggers, knowledge base, escalation, usage limits, reset)

A new `modules/agents/help_docs.py` reads both files once at import time into
two string constants. `HELP_GENERAL_PROMPT`/`HELP_BUILDING_PROMPT`
(`modules/agents/builder_flow.py`) inline the matching constant directly into
the system prompt text ("reference material - answer only from this, say you
don't know otherwise") instead of instructing the model to call a lookup
tool.

The three knowledge-base tools (`search_knowledge_semantic`,
`get_knowledge_index`, `fetch_chunk`) are removed from
`BUILDER_STATE_TOOL_SCHEMAS`/`BUILDER_STATE_HANDLERS` for `HELP_GENERAL`/
`HELP_BUILDING` (`modules/agents/tools/schemas.py`,
`modules/agents/tools/builder_handoff.py`), returning both Help states to
ADR 0064's original zero-tool-call posture (transfer tools only) - now
satisfied by prompt content instead of a runtime lookup. `SUPERVISOR`/
`BUILDER` keep these tools unchanged (ADR 0062's execution-toolset union is
untouched; this change is scoped to the two Help states only).

## Consequences

- **No embedding cost per agent, ever.** The docs are read from disk once per
  process, not chunked/embedded/stored per `agent_id`. Editing either file
  and restarting the app updates what every agent's Help persona knows,
  instantly, for all owners at once - no seeding script, no migration, no
  per-agent re-upload.
- **Reset-safe.** Since this content is process-level constant code, not a
  DB row scoped to `agent_id`, `POST /agents/me/reset` cannot touch it -
  every agent, new or freshly reset, has identical Help reference content
  with zero setup.
- **Owner-seeded knowledge bases are untouched and now irrelevant to Help.**
  Existing per-agent documents (e.g. the two catalog demo docs on the dev
  agent) remain reachable by execution-mode personas (e.g. `sales_agent`)
  exactly as before; Help personas simply no longer look at them.
- **Trade-off:** the docs are static, English-only text baked into every
  Help turn's system prompt (a few hundred tokens each, well within budget
  at their current ~55-60 lines). If this content needs to grow far beyond
  what comfortably fits in a system prompt, or needs to vary (e.g. per
  language), a real global (non-per-agent) RAG table would be the next step
  - not needed at the current size.
