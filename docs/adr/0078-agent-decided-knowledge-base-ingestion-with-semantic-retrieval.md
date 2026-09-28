# 0078 — Agent-decided knowledge-base ingestion, embedding-based chunk retrieval

Status: Accepted

## Context

An agent owner configuring a sales/support persona today pastes large
reference blocks (e.g. a store inventory list) directly as chat messages in
the config chat. That text becomes part of the turn's conversation history —
and since config-mode turns replay history each turn (per
`.claude_docs/ai_agent.md`), the entire inventory gets re-sent to Gemini on
every subsequent turn, burning input tokens indefinitely even when the
current customer question has nothing to do with it.

The existing knowledge-base feature (ADR 0046/0047) already solves storage +
retrieval for uploaded files, but:

- **Ingestion is REST-only** (`modules/agents/router.py`, upload-ticket +
  `commit_knowledge_document` in `modules/agents/knowledge_service.py`) —
  nothing in the chat tool-calling path can create a knowledge document.
  `commit_knowledge_document` also requires an S3-backed `storage_key`
  (uploaded file), which pasted chat text never has.
- **Retrieval is browse-then-fetch, not ranked** (ADR 0047 decision 5):
  `get_knowledge_index` returns *every* chunk's excerpt for the agent to
  browse, `fetch_chunk` returns one by id. This scales to a handful of
  documents but re-inflates the same problem at one remove for a large
  single-document KB (e.g. one big inventory split into many chunks) — the
  index listing itself grows unbounded per turn.
- **No per-chunk embedding exists.** `AgentKnowledgeChunk` (`modules/agents/
  models.py`) has `content` + a trigger-maintained `content_tsv`, but no
  vector column. The only embedding infra in the codebase (ADR 0042) is
  hardcoded to `messages` — queue (`modules/vector_search/queue.py`),
  write-back (`modules/vector_search/crud.py::update_embeddings`), and the
  cosine query (`semantic_search_messages`) all reference the `messages`
  table directly.

## Decision

**1. Agent-decided ingestion, not an owner-invoked command.** A new
execution-available config tool, `save_knowledge_from_text`, is added to
`CONFIG_TOOL_SCHEMAS` (`modules/agents/tools/schemas.py`) and flows into the
existing Supervisor/Builder tool unions the same way every other config tool
does — no new state, no new dispatch branch. The model decides *within the
normal tool-calling turn* (no extra Gemini call — this is one more schema
in the same request) whether the owner's latest message is reference/lookup
data that should never re-enter the prompt verbatim (inventory, price
lists, policy documents, FAQs) versus conversational instruction that stays
inline. This was an explicit, deliberate choice over an owner-invoked
"save this" command: the owner is not expected to know when the mechanism
applies.

Known, accepted risk: this is a content-type **classification** judgment,
not a size threshold — a chat message is either data that structurally
never belongs inline, or normal conversation, and the model can misjudge
either direction (saving something that wasn't meant as reference data, or
missing real reference data because of how the owner phrased it). There is
no way to eliminate this risk short of an explicit owner command, which was
rejected; mitigation is prompt wording (`BUILDER_PROMPT`/`SUPERVISOR_PROMPT`)
plus the transparency requirement below, and it should be expected to need
tuning after real usage, not to be right on the first pass.

**Transparency requirement:** whenever `save_knowledge_from_text` fires, the
agent's reply to the owner in that same turn must say what it saved and
briefly why this path is better than leaving it inline — e.g. "Saved this as
background info about your inventory — this keeps it out of every future
message so I don't re-send it to you (and burn tokens) on every turn; ask me
to remove or update it anytime." This is a prompt-level requirement on the
tool's usage instructions, not a separate confirmation step — the model
must not save silently.

**2. Text-only ingestion path, no S3 required.** `commit_knowledge_document`
is not reused as-is (it requires `storage_key`). A new
`commit_knowledge_text(session, agent, *, source_label, raw_text)` in
`modules/agents/knowledge_service.py` runs the same
`chunk_text` → quota-check → `AgentKnowledgeDocument` (`mime_type="text/plain"`,
`s3_key=None`) → `AgentKnowledgeChunk` rows sequence, skipping the S3 fetch
entirely since the text is already in hand from the tool call argument.
`AgentKnowledgeDocument.s3_key` becomes nullable (documents created this way
have nothing to delete from S3 on removal — `delete_knowledge_document`
already best-effort-catches S3 delete failures, so a null key just
short-circuits that call).

**3. Per-chunk embeddings, mirroring ADR 0042 in miniature — not reusing its
tables.** `AgentKnowledgeChunk.embedding vector(768)` (nullable) is added,
via the same `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` safety-net pattern
as `messages.embedding` (`modules/vector_search/ddl.py`), in a new
`modules/agents/knowledge_ddl.py` (alongside the existing
`content_tsv` trigger DDL) or extending it directly. A **separate** IVFFlat
index (`ix_agent_knowledge_chunks_embedding_ivfflat`) is built the same
deferred way — not at empty-table time — because `AgentKnowledgeChunk` and
`messages` are unrelated tables with unrelated growth curves and unrelated
tenant scoping (per-agent, not per-user-across-chats); sharing one index or
one queue would conflate two independent relevance spaces for no benefit.

Embedding happens synchronously at chunk-creation time inside
`commit_knowledge_text`/`commit_knowledge_document` (batch `embed_batch`
call over all chunks of the one document being committed), not via the
flush-on-demand Redis queue ADR 0042 uses for messages. Reasoning: knowledge
documents are created rarely (occasional owner action) versus messages
(every send), so there's no hot-path latency concern to defer around, and
having a chunk searchable immediately after the tool call returns (so the
owner's very next message could already retrieve it) is more valuable than
the batching win the message queue exists for.

**4. New execution-mode tool `search_knowledge_semantic` replaces ranked
retrieval; `get_knowledge_index`/`fetch_chunk` stay for small KBs.** Embeds
the customer's query via the existing `gemini_client.embed_query` /
`query_cache` path, cosine-searches `agent_knowledge_chunks` scoped to
`agent_id` (own `modules/agents/knowledge_crud.py` query mirroring
`semantic_search_messages`'s shape, `max_distance` floor, `LIMIT` top-K),
and returns the top matches' content directly — no index-then-fetch
two-hop. `get_knowledge_index`/`fetch_chunk` are **not removed**: they
remain the fallback for agents whose knowledge base is small enough that
browsing is cheap, and for chunks lacking an embedding (e.g. an embed
failure mid-ingest — mirrors ADR 0042's "drop the batch on Gemini failure"
posture rather than blocking the whole document commit). The system prompt
should steer the model to prefer `search_knowledge_semantic` once a KB
exists, but both tools stay registered.

## Consequences

- Reuses 100% of existing chunking (`chunk_text`), quota
  (`check_knowledge_quota`), and Gemini-embedding (`gemini_client`,
  `query_cache`) infra — no new external dependency.
- Two IVFFlat indexes now exist (messages, knowledge chunks) instead of one;
  each is deferred-build per its own data-population point, same operational
  step as `seed_vector_data.py` does for messages today.
- `AgentKnowledgeDocument.s3_key` becomes nullable — any code path that
  assumes it's always present (e.g. building a download URL for a document
  list in the UI) must handle `None` for chat-originated documents.
- Misclassification risk (over- or under-saving) is accepted, not solved;
  revisit prompt wording after real usage rather than trying to fully
  specify the boundary up front.
- Synchronous per-document embedding means `save_knowledge_from_text`'s tool
  call latency includes one Gemini embedding call — bounded by the existing
  per-turn Gemini round-trip budget/timeout (ADR 0047), same as any other
  tool making an external call mid-turn.
