# 0046 - Agent time schedules, knowledge base (RAG), BYOK, and pre-filter cache

Status: Accepted

## Context

ADR 0045 shipped a complete, working per-user AI agent (schema, Trigger Rule
Engine, `agent_invoke_stream` worker, Gemini tool-calling, config UI). This
ADR does **not** replace ADR 0045 - it extends it with six concrete
decisions that came out of an architecture review:

1. Reaffirm in-process function calls from `agent_worker` into the app's
   service layer (reject an HTTP/localhost API-first redesign that was
   briefly considered - see Rejected Alternative below).
2. An O(1) in-memory (Redis) pre-filter cache so most incoming messages
   never touch Postgres to check "does anyone here have an agent."
3. A new "unknown sender" trigger with its own per-sender rate limit.
4. A new "time schedule" trigger (recurring and one-off) that lets an agent
   run a task - not just send a canned message - at a given time, reusing
   the agent's own tool-calling loop (e.g. "search my chats and see who's
   waiting on a reply").
5. A per-user knowledge base (RAG) fed by uploaded documents: plain text is
   chunked server-side; PDFs are parsed and chunked entirely client-side so
   the 1GB app host never runs a PDF parser. Retrieval via Postgres FTS
   (GIN), not pgvector.
6. BYOK: an owner can store their own Gemini API key, Fernet-encrypted at
   rest in the `agents` table.

All decisions below were confirmed by the user (2026-09-23) after a review
of the original proposal against the already-shipped ADR 0045 code.

## Rejected alternative: HTTP-over-localhost worker calls

The original proposal had `agent_worker` call the main app over HTTP
(`localhost`) for every tool side-effect, with explicit 429/timeout
handling. Rejected: `agent_worker` already runs the same codebase
(`linka-app:latest` image) and already imports the service layer directly
(`modules.messaging.service`, `modules.chats.service`, ...), sharing the
async engine/connection pool. Going through HTTP would add a TCP+JSON
round trip per tool call, a second auth mechanism, and duplicate
rate-limiting/session logic, for zero benefit on a 1GB RAM host - there is
no process/deployment boundary between `agent_worker` and `app` today that
an HTTP hop would actually be protecting. **No change from ADR 0045**:
tool calls stay in-process, `execute_tool_call` keeps dispatching straight
into the existing service functions, acting as `agent.owner_user_id`.
Revisit only if `agent_worker` is ever extracted into a genuinely separate
deployable (different repo/language) - not planned now.

## Decision

### 1. O(1) pre-filter cache

Today `trigger_engine.evaluate_triggers` runs `get_enabled_agents_for_owners`
against Postgres on **every** sent message, even though the overwhelming
majority of chats involve zero agents. Add a Redis cache-aside layer so the
common case (no agent anywhere in this chat) never hits the DB:

- `agent:enabled_owners` - a Redis **SET** of `owner_user_id`s that
  currently have `Agent.is_enabled = true`. Updated on every enable/disable/
  create (`POST /agents/me`, `PATCH /agents/me` toggling `is_enabled`).
- `agent:trigger_cfg:{owner_user_id}` - a Redis **STRING** holding the JSON
  of that owner's `Agent.id` + `Agent.triggers` (the full trigger config:
  `on_specific_chats`, `on_time_window`, `on_unknown_sender`, `on_schedule`).
  Rewritten on every trigger-affecting write: `PATCH /agents/me`,
  `update_own_triggers` tool calls, agent create.
- `evaluate_triggers` flow: for each other participant in the chat,
  `SISMEMBER agent:enabled_owners {user_id}` (O(1)); only for a hit, `GET
  agent:trigger_cfg:{user_id}` and run the existing `_matches_trigger`
  logic against the cached JSON - no Postgres round trip in the match path.
  Postgres is only touched afterward, once, to load the full `Agent` row
  right before enqueueing onto `agent_invoke_stream` (same as today).
- **Cache-miss fallback**: if either key is absent (cold Redis, first
  deploy, manual flush), fall back to the existing DB query for that one
  evaluation and repopulate the cache - no explicit warm-up script needed,
  the cache self-heals lazily. Source of truth stays Postgres; Redis is
  strictly a read-through cache.

### 2. Unknown-sender trigger

New `Agent.triggers.on_unknown_sender` block:
```json
{"enabled": false}
```
Fires when a message arrives in a private (non-group) chat that has **no
prior messages** before this one (checked via the existing message-count-
for-chat path, cheap and already indexed by `chat_id`) - i.e. this is
literally the first message ever exchanged in that chat. Fires once per
new chat; once the sender has sent a first message, later messages from
them are no longer "unknown" and fall under the normal `on_specific_chats`/
default-no-trigger path. Independent of `can_message_new_private_contacts`
(that restriction gates the agent *opening* new chats via `create_chat`,
not being messaged first).

Dedicated rate limit, on top of - not instead of - the existing 20/hour
total activation quota: `AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY` (default 20),
`AGENT_UNKNOWN_SENDER_QUOTA_WINDOW_SECONDS` (86400), fixed-window key
`ratelimit:agent_unknown_sender:{agent_id}:{sender_user_id}` via the
existing `infra.ratelimit.service.check_and_increment`. This caps how many
times a *single* unknown sender can wake the agent per day, independent of
the agent's overall hourly budget - stops one bad actor from burning the
whole hourly quota by itself while still being bounded by it in aggregate.

### 3. Time-schedule trigger (recurring + one-off)

New `Agent.triggers.on_schedule`: a list of entries, capped at
`AGENT_MAX_SCHEDULE_ENTRIES` (10) per agent - enforced at `PATCH /agents/me`
and in `update_own_triggers` (a self-editing agent cannot schedule its way
past the cap):
```json
{
  "id": "sch_<snowflake>",
  "kind": "recurring" | "once",
  "time": "12:00",                        // recurring: daily HH:MM, UTC for now (same known simplification as on_time_window - no per-owner timezone field yet)
  "at": "2026-09-24T15:00:00Z",           // once: absolute UTC instant
  "instruction": "free text task fed to the agent as the turn's seed prompt",
  "chat_id": null,                         // optional: scope the task's context to one chat
  "enabled": true
}
```
Unlike ADR 0031's `scheduled_messages` (a human pre-composes exact message
text to fire later), this lets the *agent* run a tool-calling turn at a
given time - e.g. "check which conversations have been waiting more than a
day for my reply and follow up." It deliberately does **not** reuse the
`scheduled_messages` table/worker: that path enqueues onto
`message_send_stream` with fixed content; this path enqueues onto
`agent_invoke_stream` with an instruction for a full agent turn. Sharing
the *mechanism* (Redis due-ZSET + poll) is what's reused, not the table or
consumer.

- New Redis ZSET `agent_schedule_due` - member `{agent_id}:{schedule_id}`,
  score = next-fire unix timestamp. This is purely a "when to next check"
  index; the schedule definition itself lives in `Agent.triggers.on_schedule`
  (source of truth, Postgres).
- A lightweight poll loop inside the existing `agent_worker` process (same
  container, not a new service - stays within the 192m budget):
  `ZRANGEBYSCORE agent_schedule_due -inf <now>` every
  `AGENT_SCHEDULE_POLL_INTERVAL_SECONDS` (30). For each due member: re-load
  the `Agent` row + matching `on_schedule` entry (defense in depth, same
  pattern as the `is_enabled` re-check in step 3 of ADR 0045), skip if
  disabled/deleted/entry removed, otherwise XADD onto `agent_invoke_stream`
  with `{agent_id, schedule_id, kind: "schedule"}` (no `chat_id`/
  `message_id` - `_run_turn` seeds the conversation from `instruction`
  instead of chat history, optionally joined with `read_history` on
  `chat_id` if the entry set one).
  - `recurring`: after firing, compute next occurrence (same `time`
    tomorrow, UTC) and re-`ZADD`.
  - `once`: after firing, `ZREM` and flip that entry's `enabled: false` in
    `Agent.triggers` (one small DB write) so it's visible as fired in the
    UI, not silently gone.
- `agent:trigger_cfg:{owner_user_id}` (decision 1's cache) is invalidated/
  rewritten on every `on_schedule` edit, same as the other trigger types;
  the ZSET is kept in lockstep on the same write path.
- Budget: a schedule-fired turn consumes the same hourly activation quota
  (`agent_activation`, 20/hour) and the daily active-time budget (1h/day)
  as a message-fired turn - one unified ceiling on Gemini spend per agent,
  rather than a third quota dimension. If this proves too tight for users
  with several daily schedules in practice, split it out later; not doing
  it preemptively.
- New tools this enables (see decision 5): `search_messages` (existing
  ADR 0040/0042 search, scoped to the owner's chats) lets a schedule-fired
  turn like "who's waiting on a reply" actually work without a new search
  backend.

### 4. Knowledge base (RAG) - storage + retrieval tool

**Client-side responsibility split** (confirmed): plain text/Markdown is
chunked **server-side**; anything else (v1 scope: **PDF only**) is parsed
and chunked entirely **client-side** so the app host never runs a PDF
parser.

- PoC gets a vendored `pdf.js` build under `poc/vendor/pdfjs/` (local file,
  same convention as `poc/vendor/thumbhash.js` - no CDN, per the repo's
  existing vendoring pattern). Upload UI accepts only `.pdf`/`.txt`/`.md`
  (enforced both by the file picker's `accept` attribute and a server-side
  MIME allowlist rejecting anything else) and shows an explicit notice at
  upload time: *"only text is extracted - scanned/image-only PDFs will not
  be searchable."* No OCR.
- **PDF flow**: browser extracts text via `pdf.js`, chunks it client-side
  (simple fixed-size/overlap splitter, mirrors the server-side one so
  retrieval quality is consistent regardless of path), uploads the original
  PDF bytes to S3 via the existing presigned-URL flow (reference/download
  only, never re-parsed server-side), and POSTs the resulting chunk array
  as JSON. The server only stores what it's given.
- **Text/Markdown flow**: client uploads the raw file to S3 (same presigned
  flow) and sends the file key; the server fetches it and chunks it itself
  (fixed-size/overlap splitter, `AGENT_KNOWLEDGE_CHUNK_MAX_CHARS`, no NLP -
  cheap).

**Schema** (new tables, no migration system per repo convention - extend
`scripts/init_db.py`):
```python
class AgentKnowledgeDocument(Base):
    __tablename__ = "agent_knowledge_documents"
    id: BigInteger            # Snowflake, PK
    agent_id: BigInteger      # FK -> agents.id
    filename: str
    s3_key: str
    mime_type: str
    status: str                # "processing" | "ready" | "failed"
    created_at

class AgentKnowledgeChunk(Base):
    __tablename__ = "agent_knowledge_chunks"
    id: BigInteger            # Snowflake, PK
    agent_id: BigInteger      # FK -> agents.id (denormalized for the search index scope)
    document_id: BigInteger   # FK -> agent_knowledge_documents.id
    chunk_index: int
    content: str
    content_tsv: tsvector      # generated column + trigger, same mechanism as modules/search/ddl.py
    created_at
```
`btree_gin(agent_id, content_tsv)` partial index - same pattern as ADR
0040's `(chat_id, content_tsv)`, just scoped to `agent_id` instead of
`chat_id`. **No pgvector, no embedding column, on this table** - ADR 0042
is not reversed, pgvector stays in use for message search; this table
simply doesn't opt into it, to avoid growing a second IVFFlat index's
memory footprint for what's expected to be a small per-agent corpus.
Guardrails: `AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT` and
`AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT` (exact defaults TBD at
implementation time), enforced at upload.

**New tools** (tool registry grows from 6 to 8; round-trip cap stays 4):
- `search_knowledge(query)` - `build_tsquery`-style match against
  `agent_knowledge_chunks.content_tsv` filtered to the caller's own
  `agent_id`, top-N by `ts_rank`. Never crosses into another agent's
  documents (hard-scoped like `update_own_triggers`).
- `search_messages(query, chat_id?)` - thin wrapper reusing the existing
  ADR 0040 (keyword FTS) / ADR 0042 (semantic) search modules, membership
  still enforced via the same `participants` JOIN pattern those modules
  already use, so the agent can only ever search chats its owner is
  actually in. This is what makes the "check who's waiting for a reply"
  schedule example (decision 3) actually work.

### 5. BYOK (bring your own Gemini key)

- New column `Agent.encrypted_gemini_api_key: bytes | None`, Fernet-
  encrypted. New `modules/agents/crypto.py`: `encrypt_api_key(raw) -> bytes`
  / `decrypt_api_key(blob) -> str`, thin wrapper over
  `cryptography.fernet.Fernet` keyed by a new required-if-BYOK-used env var
  `AGENT_BYOK_ENCRYPTION_KEY` (documented in `deploy/env.production.example`,
  separate from `GEMINI_API_KEY`/`JWT_SECRET_KEY`). Confirm `cryptography`
  is already a resolved dependency (likely pulled in transitively via the
  ADR 0009 JWKS verification path) before adding it explicitly.
- **Threat model, stated plainly**: this protects the key against a
  DB-only compromise (e.g. a leaked backup) - not against an app-server
  compromise, since the app process holds the Fernet key in its own env.
  Standard at-rest encryption, not a substitute for secrets-manager-grade
  isolation.
- `PATCH /agents/me` gains a write-only `gemini_api_key: str | None` field
  - never echoed back; `AgentOut` instead exposes a boolean `has_custom_key`.
  Setting it to `null`/empty clears it, falling back to the shared
  `settings.GEMINI_API_KEY`.
- `gemini_client.generate_turn` takes an optional `api_key` param;
  `invoke_worker._run_turn` decrypts `agent.encrypted_gemini_api_key` when
  present and passes it through, else uses the shared key (unchanged path).
- Rate-limit interaction: the 20/hour activation quota and the 1h/day
  active-time budget still apply unconditionally (they protect *our*
  infra - DB/Redis/worker CPU/wall-clock - regardless of whose Gemini key
  is spent). The 5-calls/minute Gemini throttle exists specifically to
  protect the *shared* key's quota; when `has_custom_key` is true, that
  specific limiter is skipped - a BYOK owner burning their own key's quota
  is their own concern. The per-turn 20s timeout and the worker's
  concurrency semaphore stay in force either way (those bound our own
  resource usage, not the API budget).

## Consequences

- New Redis keys: `agent:enabled_owners` (SET), `agent:trigger_cfg:{owner_user_id}`
  (STRING, cache-aside over `Agent.triggers`), `agent_schedule_due` (ZSET).
- New Postgres tables: `agent_knowledge_documents`, `agent_knowledge_chunks`
  (both added via `scripts/init_db.py`, no migration system per repo
  convention) plus a `btree_gin(agent_id, content_tsv)` partial index.
- `agents` table gains `encrypted_gemini_api_key` (nullable bytes).
- `Agent.triggers` schema grows two new keys (`on_unknown_sender`,
  `on_schedule`) - additive, existing agents' JSONB defaults unaffected
  (`DEFAULT_AGENT_TRIGGERS` gets the two new keys with safe empty defaults).
- Tool registry grows from 6 to 8 tools (`search_knowledge`,
  `search_messages`); no change to the round-trip cap or per-turn timeout.
- New required-if-BYOK-is-used secret: `AGENT_BYOK_ENCRYPTION_KEY`.
- `agent_worker` gains a second internal loop (the schedule poller)
  alongside the existing stream consumer, still one container, still
  `mem_limit: 192m` - to be verified against actual usage once the PDF/
  chunking path exists, since that's the first meaningfully larger
  in-process workload this container will do.
- Known simplification carried forward from ADR 0045: no per-owner
  timezone field yet, so both `on_time_window` and the new recurring
  `on_schedule.time` are UTC, not owner-local. Worth revisiting together
  if it becomes a real pain point, rather than fixing it piecemeal per
  trigger type.
