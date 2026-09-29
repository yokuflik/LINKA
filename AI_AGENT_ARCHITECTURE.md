# Linka AI Agent — Technical Architecture

This document describes the design and implementation of Linka's built-in AI agent: a per-user, autonomous messaging service account backed by Gemini function calling. It assumes familiarity with the platform architecture described in [README.md](README.md) (async send pipeline, Redis Stream fan-out, the Rust `ws_gateway`, partitioned Postgres). Every decision below traces to a numbered ADR in `docs/adr/`; ADR numbers are cited inline rather than restated.

---

## 1. Core Mechanism

### 1.1 The agent as a first-class platform actor

The agent is not a chatbot UI bolted onto the product — it is a peer participant inside the core message model. Each agent has:

- A `Agent` row (`modules/agents/models.py`), one per owner (`owner_user_id` is `UNIQUE`).
- Its own permanent 1:1 chat with the owner (`owner_agent_chat_id`), created eagerly at provisioning time and used both as a config/setup channel and as the agent's own notification outbox.
- Every message it sends carries `messages.sender_agent_id`, populated by the same `create_message`/`process_outgoing` path a human send uses. It sends **as the owner** (`sender_id = agent.owner_user_id`) — recipients cannot distinguish an agent-sent message from one the owner typed, which is precisely what makes the restriction and rate-limit sharing described in §2 and §4 necessary.

There is no separate agent-specific send/fan-out/receipt pipeline. The agent calls into the exact same `modules.messaging`/`modules.chats` service layer a human REST/WS request would call, which is what allows the platform's existing partitioning, receipt, and rate-limiting infrastructure to apply to it unmodified (ADR 0045).

### 1.2 Two decoupled models, two different jobs

The agent's reasoning pipeline is deliberately split across two models with non-overlapping responsibilities, invoked in sequence for every inbound execution-mode turn:

| Stage | Model | Role | Output |
|---|---|---|---|
| 1. Gate | TypeSafe `jev` (+ a minimal Gemini call on the reject path) | Structured intent classification | `JudgeVerdict` (approve/reject/malicious), never free text |
| 2. Reason | Gemini (`gemini-2.0-flash` class), function calling | Multi-round-trip tool-calling turn | Text replies + tool invocations |

**Gemini** is the agent's actual reasoning engine — the only model that holds conversation context, sees the tool schemas, and decides what to do. It runs a bounded round-trip loop (`modules/agents/invoke_worker.py::_run_turn`): each call may return either plain text (turn ends) or a function call (dispatched, its result appended to the conversation, another call made), capped at `AGENT_MAX_ROUND_TRIPS` (8) per turn. Gemini never executes anything itself — every tool call is dispatched through `modules/agents/tools/dispatch.py`, which resolves a handler function and returns its structured result back into the conversation.

**`jev`** (ADR 0076, superseding an earlier Gemini-based judge from ADR 0053) is a dedicated, non-generative structured classifier — it answers a small set of atomic yes/no-with-confidence questions about a single inbound message (`on_topic`, `prompt_injection`, `info_extraction`, `code_execution`; `modules/agents/typesafe_client.py::classify`) and returns nothing else. It runs *before* the main Gemini turn is even built, on an isolated context (the single latest message plus a short domain description) — it never sees conversation history or the tool schema set, minimizing what a malicious message can manipulate. Section 1.3 details the routing this produces.

The two models are billed against, and rate-limited against, independent Redis buckets (`agent_gemini_calls:{agent_id}` vs `agent_judge_calls:{agent_id}`), and their token cost is metered into the same two rolling usage windows described in §4.3, so a judge-heavy period cannot silently starve the reasoning budget or vice versa without visibility.

### 1.3 How the classification engine routes requests

For every execution-mode, message-fired turn (any chat other than the owner's own config chat, excluding schedule-fired turns), `invoke_worker.py::_run_turn` inserts the judge gate immediately after computing whether the turn is config-mode, before any Gemini contents are built:

```
inbound message
     │
     ▼
jev.classify(message, domain_context)  ──fail-open──►  approved (logged ERROR)
     │
     ├─ on_topic == false  ──► reject
     ├─ prompt_injection / info_extraction / code_execution ≥ threshold ──► reject + is_malicious=true
     └─ otherwise ──► approve
     │
     ▼
approved? ──no──► redirect_message sent to the customer (Gemini, minimal call, reject-path only)
     │                      │
     │                      └─ is_malicious? ──yes──► escalate_chat(): pause chat + push + owner notice
     │
    yes
     │
     ▼
mark_as_read(triggering message)   (ADR 0080)
     │
     ▼
full Gemini tool-calling turn (up to 8 round-trips against the 14-tool
execution registry) ──► send_message / reply_message / … dispatched via
execute_tool_call, each checked against Agent.restrictions
```

Key properties of this routing:

- **Rejection short-circuits before any main-model call.** On `is_approved=False`, `_run_turn` returns before `_build_initial_contents`/`generate_turn` is invoked at all — zero Gemini reasoning calls are spent on a message the gate has already dismissed. The customer still receives a reply: either the judge's own `redirect_message` (authored in the customer's language by a minimal, history-free Gemini call over the deterministic rejection `reason`) or a fixed local fallback if that call itself fails.
- **A media-only message (no text) is approved without invoking the classifier at all** — there is nothing to classify.
- **`is_malicious` is a stricter sub-condition of rejection**, not a separate model call — `jev`'s four atomic answers are derived server-side into both `is_approved` and `is_malicious` from the same single API call, so escalation detection costs nothing beyond ordinary rejection.
- **Follow-up detection avoids the "how much?" pronoun problem** without giving the judge chat history: a Python-computed boolean (`_is_follow_up_in_active_conversation`, based on whether the agent replied recently, never on message content) is passed to the classifier as a hint, keeping the judge's own input surface minimal.
- **Fail-open by design, and narrowly scoped.** A `jev` API error, malformed response, or the judge's own rate limit being exceeded resolves to `is_approved=True` — a technical failure of the safety layer degrades to "let the main agent handle it," never to silently dropping the message, and can never itself flip `is_malicious=True` (a failure is not a positive detection).
- **Config-mode turns (the owner's own setup conversation) bypass the judge entirely** — it exists to protect the agent's real-world conversational surface (customers, third parties), not the trusted owner talking to their own agent.

Within a successfully-approved Gemini turn, tool selection is itself gated by a second, independent routing decision — the tool-mode gate:

```python
# modules/agents/tools/dispatch.py (conceptual)
is_config_mode(chat_id, agent)  # chat_id == owner_agent_chat_id?
    → config mode: further branch on Agent.builder_state
        (supervisor | builder_agent | help_agent_building | help_general)
        — each state gets its own system prompt and its own disjoint
          tool schema subset, selected purely by chat_id + builder_state
    → execution mode: the full 14-tool registry (send_message, reply_message,
      create_chat, leave_group, read_history, search_messages,
      search_semantic, search_knowledge_semantic, get_knowledge_index,
      fetch_chunk, list_attached_files, send_attached_file,
      update_own_triggers, pause_and_escalate)
```

This gate is deliberately keyed only on `chat_id` (is this the owner's private config chat?) and `builder_state` — **never** on `active_skill`, `system_prompt` content, or anything the model itself asserts about its own state (ADR 0047 decision 4). That is a hard, code-level boundary specifically because prompt content is attacker-influenceable and a state string the model can freely emit is not a safe gate for which tools become reachable. Supervisor and Builder are the sole deliberate exception, unioning in the full execution toolset so the owner can issue a direct "message X and tell me what they say" command from their own idle or setup chat without a state transfer — both Help states keep the strict non-overlap invariant, since they only explain and never act (ADR 0062, ADR 0064).

### 1.4 Tool-calling implementation

Tool schemas (`modules/agents/tools/schemas.py`) are plain Gemini function-declaration JSON, dispatched via `dispatch.py`'s handler tables — no dynamic reflection, no code-execution tool exists in the registry at all (the "never run code" prompt rule in §2 is defense in depth on top of an already-absent capability, not a fix for a real one). Every handler:

1. Receives the already-authenticated `Agent` row and the model-supplied arguments.
2. Performs its own restriction/quota check (§2) before touching the database.
3. Calls into the ordinary `modules.messaging`/`modules.chats`/`modules.search`/`modules.vector_search` service layer — the same functions a REST/WS request handler calls.
4. Returns a structured JSON result, with all internal identifiers stripped except `chat_id` (see identity masking, §2.3) — this result is appended back into the Gemini conversation as a function response, feeding the next round-trip.
5. Is logged to `AgentToolCallLog` (`tool_name`, `arguments`, `allowed`, `denial_reason`) for audit, regardless of outcome.

Recursion is capped at 8 round-trips per turn (`AGENT_GEMINI_CALLS_PER_MINUTE`/round-trip budget, ADR 0047), enforced in-process, not by the model's own judgment about when to stop.

---

## 2. Security & Restrictions

The agent's permission model is a **denylist over a fixed capability set**: there is no account-management, settings, or profile tool in the registry at all — those actions are structurally absent, not merely refused. `Agent.restrictions` (JSONB) only ever narrows within the messaging domain it already has:

```json
{
  "can_send_messages": true,
  "can_message_groups": false,
  "can_message_private": true,
  "can_message_new_private_contacts": true,
  "can_leave_groups": true,
  "blocked_read_chat_ids": [],
  "max_messages_per_day": null
}
```

`Agent.system_prompt` is explicitly a **soft** constraint — behavioral guidance injected into Gemini's system instruction, never treated as a security boundary. Every restriction above is enforced in code, in `execute_tool_call`, independent of anything the prompt says.

### 2.1 Two independent enforcement layers

Restriction enforcement is deliberately duplicated at two layers that read the *same* underlying JSONB row, rather than maintaining two copies of policy:

**Layer 1 — application layer (`modules/agents/tools/execution.py`).** Each tool handler checks the relevant restriction fields immediately before calling the underlying service (e.g. `_tool_send_message` checks `can_send_messages`/`can_message_groups`/`can_message_private`/`blocked_read_chat_ids` before calling `process_outgoing`). A denial raises `ToolDeniedError`, which becomes a clean function-response the model sees — no raw exception, no DB round-trip. This layer protects against a misbehaving *prompt*: the model cannot argue its way past a Python `if`.

**Layer 2 — database layer (ADR 0066).** Layer 1 protects against the model, but not against a *bug*: a new tool handler that forgets a check, or a future code path that calls the messaging/chat service directly for an agent without going through `execution.py`, would silently bypass every restriction with nothing at the database layer to stop it. Three `BEFORE` triggers on Postgres itself re-check the same `agents.restrictions` row, independent of any Python code path:

- **`trg_agents_enforce_message_restrictions`** (`BEFORE INSERT ON messages`) — reads the new row's `sender_agent_id` column (populated by `create_message` whenever the writer is an agent — deliberately *not* inferred from the message's display type, which is display-only and not a reliable signal to gate on) and raises if `can_send_messages` is false, the target chat is in `blocked_read_chat_ids`, or the chat's group-ness disagrees with `can_message_groups`/`can_message_private`.
- **`trg_agents_enforce_leave_group`** (`BEFORE DELETE ON participants`) — enforces `can_leave_groups` on the real `DELETE` `leave_group` performs (not a soft state flag).
- **`trg_agents_enforce_new_private_chat`** (`BEFORE INSERT ON participants`) — enforces `can_message_new_private_contacts`, distinguishing "brand-new 1:1 chat" from "adding a member to a chat the owner already belongs to" by checking for a pre-existing pair at trigger time.

**Identity propagation without changing the connection model.** The natural-looking design — a Postgres role per agent with Row-Level Security — was explicitly rejected: the app's async engine is a single shared connection pool, often behind PgBouncer in transaction-pooling mode, so there is no durable per-request Postgres identity to hang RLS policies off without abandoning pooling or adding a `SET ROLE` dance per statement. Instead, any write path that persists an agent-attributed row issues `SET LOCAL app.current_agent_id = '<id>'` inside the same transaction as the mutating statement — `SET LOCAL` is transaction-scoped, so it is safe under transaction pooling (a bare `SET` would leak onto whatever request reuses the connection next) and is never persisted or readable outside that transaction. The trigger functions read it via `current_setting('app.current_agent_id', true)`, falling back to NULL (treated as human-originated, no agent check) when unset.

This layer is strictly additive — Layer 1's checks are unchanged and still give the model a clean denial without ever surfacing a raw Postgres exception; the trigger is the net that catches what Layer 1 might miss.

**Deliberate gaps, documented rather than silently assumed covered:**
- `blocked_read_chat_ids` on the *read* side (`read_history`/`search_messages`) has no DB trigger — Postgres has no `BEFORE SELECT`, and enforcing it there would mean adopting the RLS approach already rejected above. Read restriction stays application-only.
- `max_messages_per_day` stays Redis/application-enforced — a rolling quota isn't expressible as a row-level constraint without its own counter table and a race-prone read-modify-write; the existing Redis counter already serves this correctly, and the worst case of a missed check is one over-quota message, not an unrestricted bypass.

### 2.2 Trigger-side and worker-side gating

Beyond per-tool-call restrictions, two coarser gates bound when the agent runs at all:

- **`Agent.is_enabled`** (default `false` for newly-provisioned agents) is checked both at trigger-evaluation time (inside the ordinary message-persistence path, synchronously but cheaply — short-circuits immediately if no chat participant owns an enabled agent) and again at worker-dequeue time, closing the race window between a trigger firing and the worker actually picking it up.
- **`paused_chat_ids`** — chats the agent has escalated via `pause_and_escalate` — are skipped entirely at trigger-evaluation time until a human explicitly resumes the chat or the pause auto-expires (default 24h). The agent is simply never invoked for a paused chat, not refused at the tool layer.

### 2.3 Identity masking

Every tool result handed back to Gemini has internal database identifiers stripped before the model ever sees them: `read_history` and `search_messages` resolve `sender_id` to `{name, phone_number}` via a batch lookup rather than returning the raw id, and `search_messages` additionally drops `message_id`. The one deliberate exception is `chat_id`, kept because it is the sole handle the model needs to target a follow-up tool call (e.g. `read_history(chat_id=...)`) — it is passed between tool calls, never reproduced as text to a person. This is enforced unconditionally, server-side, not behind any restriction toggle — the same posture as every other security boundary in this system: never trust the prompt to withhold what the code can withhold instead.

### 2.4 No code-execution surface

The tool registry contains no code-execution primitive of any kind. A fixed prompt rule additionally instructs the model to refuse any request — including from its own owner — to run, execute, or simulate executing code, a script, or a shell command. This is explicitly framed as defense in depth against the model being talked into *role-playing* execution in its text output, not a fix for an actual capability, since no such capability exists in the first place.

---

## 3. Semantic Search Integration

The agent has two distinct search surfaces, both built on the same server-side vector infrastructure and both scoped so that no message content ever leaves the server as a bulk download to be searched client-side.

### 3.1 Platform-wide semantic message search (ADR 0042)

Linka embeds message text server-side (Gemini `gemini-embedding-001`, Matryoshka-truncated to 768 dimensions) into `messages.embedding vector(768)` (pgvector), indexed with **IVFFlat** rather than HNSW:

- HNSW keeps its entire graph in RAM at build time and is known to OOM-kill on memory-constrained hosts as row counts grow; IVFFlat's footprint is a small, bounded multiple of the raw vector data, at the cost of approximate (not exact) nearest-neighbor recall — an explicit, documented trade-off, not an oversight.
- Because IVFFlat's clustering quality depends on representative data at build time, the index is **not** created automatically by schema initialization — it is built explicitly, once real data exists to compute meaningful centroids, and can be rebuilt later as the corpus grows by orders of magnitude.
- **Embedding generation is kept off the hot send path.** After a text message is persisted, its `{message_id, content}` pair is pushed onto a plain Redis list (`vector_embed_queue`) — O(1), no external call, no added send latency. Two independent triggers drain it: a size-based auto-flush once the queue reaches 50 entries (fired as a detached background task, never awaited inline on the sender's request), and an on-demand synchronous flush the moment a semantic-search request is made, guaranteeing a just-sent message is searchable even below the size threshold. A failed embedding batch is dropped, logged, and not retried — an accepted gap for the demo scale this targets.
- **Query time**: `SELECT … FROM messages m JOIN participants p ON p.chat_id = m.chat_id AND p.user_id = :user_id WHERE m.embedding IS NOT NULL … ORDER BY m.embedding <=> :query_vector LIMIT :k`. Chat membership is enforced **in the query itself**, via the join, not via a pre-fetched chat-id list handed to the agent or cached anywhere — a member removed from a chat loses search access to it immediately, at the next query, not at some future cache-refresh boundary.

### 3.2 The agent's `search_semantic` tool (ADR 0069)

A thin execution-mode wrapper over the same `vector_search.service.semantic_search` the REST endpoint calls — `user_id` is fixed to `agent.owner_user_id`, membership is enforced by the identical `participants` JOIN, and results are capped at the platform's own default result-size limit; there is no separate, looser limit for the agent. It accepts an optional `chat_id` (re-checked against `blocked_read_chat_ids`, §2.1) and an optional date range, and returns each hit's resolved sender identity (§2.3) plus its cosine `distance`, so the model can judge how loose a match is before deciding whether to act on it. No new rate-limit bucket exists for this tool: the handler calls the vector-search service **in-process**, and the agent's own per-minute Gemini call budget already bounds how often a turn can reach it — the tool itself is not a channel to bypass anything.

Critically, the tool returns only the matched message text and metadata for the handful of top-K hits the model asked about — never a bulk transcript. Retrieval always happens server-side, inside the tool handler; nothing resembling "download chat history for local search" exists anywhere on this path.

### 3.3 Per-agent knowledge base (Agentic RAG, ADR 0046 / ADR 0078)

A second, independent semantic-search surface exists for the agent's own reference material — uploaded documents, or free text the model itself decides mid-conversation is lookup data (`save_knowledge_from_text`) rather than something to answer from directly. This is structurally separate from message search: its own tables (`AgentKnowledgeDocument`, `AgentKnowledgeChunk`), its own `embedding vector(768)` column, its own independently-deferred IVFFlat index, scoped hard to `agent_id` — it shares no index, no Redis queue, and no table with `messages.embedding`.

Ingested text is chunked, then embedded **synchronously** right after the chunk rows are written (unlike the flush-on-demand queue for messages) — knowledge documents are created rarely (an occasional owner or model action, not on every send), so there is no hot-path latency to defer around, and the owner's very next message may already need to retrieve what was just saved. A Gemini embedding failure leaves the chunk's `embedding` column `NULL` and logs a warning; it never blocks or rolls back the document commit.

Two retrieval paths exist and are model-selectable per situation: `get_knowledge_index` + `fetch_chunk` (a two-hop index-then-fetch, the fallback for a small knowledge base or for chunks that failed to embed) and `search_knowledge_semantic` (a direct one-hop cosine search over `agent_knowledge_chunks`, preferred once a real knowledge base exists). Both are scoped exclusively to the calling agent's own `agent_id` — one agent's knowledge base is architecturally invisible to another's, and to the platform's own message search, which cannot see it either.

A hard transparency requirement is attached to `save_knowledge_from_text` at the prompt level: the model must tell the owner what it saved and why, in the same turn — the classification of "this is reference data" is a judgment call, not a size threshold, and can misfire in either direction, so silent ingestion is deliberately disallowed as a mitigation.

---

## 4. Real-time Flow

The agent is not a synchronous request/response service — it is one more consumer hanging off the platform's existing async, Redis-Stream-based messaging backbone, with its own dedicated stream and worker pool layered on top.

### 4.1 Wake-up: the Trigger Rule Engine

Message persistence already runs inside `modules/messaging/service.py` on the ordinary async send path (`send_message` → `XADD message_send_stream` → `send_worker` persists via `process_outgoing` → `XADD message_fanout_stream` for delivery). Immediately after a message is persisted — in parallel with, never blocking, the normal fan-out described in [README.md](README.md) — the Trigger Rule Engine evaluates whether any chat participant owns an enabled agent whose `triggers` configuration matches this message:

- `on_specific_chats` — per-chat opt-in, with optional case-insensitive keyword gating (deliberately substring matching, not regex, to avoid ReDoS from owner-supplied keyword input).
- `on_time_window` — restricts wake-ups to a daily local-time window.
- `on_unknown_sender` — fires once, on the very first message from a stranger in a private chat, and auto-registers that chat into `on_specific_chats` so the agent keeps responding to the same person going forward (capped, FIFO-evicted at 200 auto-added chats).
- `on_any_message` — a broader, stateless catch-all: every message in every private (non-group) chat, no per-chat setup required.
- `on_schedule` — recurring or one-off entries, driven separately by a Redis due-ZSET (`agent_schedule_due`) polled by the agent worker, not by an inbound message at all.

A matched, quota-permitting trigger does not invoke Gemini synchronously inline in the send path. It pushes a lightweight event (`agent_id`, `chat_id`, `message_id`) onto a dedicated Redis Stream, `agent_invoke_stream` — architecturally the same pattern as `message_send_stream`/`message_fanout_stream`: the hot path only enqueues, and a decoupled consumer does the actual work. An hourly per-agent activation quota (Redis fixed-window) gates entry to this queue; exceeding it silently drops the trigger (the message is still delivered normally — the agent simply doesn't respond), rather than queueing a backlog.

**Debounce and turn-mutex (ADR 0063 / ADR 00732 / ADR 0077).** A burst of fast messages from the same sender — or a customer correcting themselves mid-thought — would otherwise race several independent turns against the same chat. Instead of enqueueing immediately, a trigger match `ZADD`s onto a debounce due-ZSET keyed `{agent_id}:{chat_id}` (default 2s window); a second match for the same pair before the window fires overwrites the score and the stashed `message_id`, coalescing the burst into a single turn seeded from the latest message. A separate per-`(agent_id, chat_id)` Redis mutex around turn execution prevents a debounce-fired turn from racing a still-running previous one — instead of blocking, it re-arms the debounce and a superseded-turn flag tells the in-flight turn to drop any pending `send_message`/`reply_message` call rather than deliver a reply built from stale context.

### 4.2 Execution: the dedicated worker pool

`agent_invoke_stream` is consumed by an async worker pool running in its **own Docker Compose service** (`agent_worker`), fully decoupled from the main Uvicorn/FastAPI process and the Rust `ws_gateway` — a crash inside a Gemini turn cannot take down message delivery, and scaling the agent workload is "add more `agent_worker` instances," not a code change. Concurrency inside each worker is bounded by a plain `asyncio.Semaphore`, not by artificial time-slicing — the workload is I/O-bound (Gemini HTTP calls, database round-trips), so `asyncio` already yields at every `await`, making a custom cooperative scheduler unnecessary complexity for this workload shape. A per-turn wall-clock timeout (`asyncio.wait_for`) aborts a stuck turn cleanly rather than leaving it to resume indefinitely.

Once a turn completes (§1.2–1.4), outgoing messages the agent sends re-enter the **exact same** send pipeline a human WS client uses: `process_outgoing` → persistence → `enqueue_fanout` → the routing layer (`chat_instances:{chat_id}` lookup → `PUBLISH instance_inbox:{server_id}`) → the Rust `ws_gateway`'s `fanin.rs`, which delivers the frame to every live connection subscribed to that chat. The agent's own connection to the recipient is invisible in this pipeline — there is no agent-specific WebSocket frame type, no agent-specific fan-out path; a message the agent sends is, from the transport layer's perspective, indistinguishable from one the owner typed by hand.

**Read receipts (ADR 0080).** At the start of a message-fired execution turn, right before the peer-visible typing indicator begins, the worker enqueues a `read` receipt for the triggering message via the ordinary async receipt path (`XADD receipt_log_stream`) — mirroring a human reading before replying. This is unconditional at the call site; the existing 1:1-only/privacy-masking logic in the receipts worker applies exactly as it does for a human-issued read receipt, so no new privacy logic was needed.

### 4.3 Interaction with the Rust gRPC ID service and the Rust WS gateway

The agent participates in the platform's Rust-backed infrastructure exactly as a human user does, with one narrow, deliberate exception:

- **Snowflake IDs**: every row the agent's actions create — messages, tool-call log entries — is assigned an id the same way any other row is, via the standalone Rust gRPC Snowflake service under load (or the in-process generator otherwise). The agent has no separate id-allocation path.
- **The Rust `ws_gateway`** is the *only* thing serving `/ws` in this system; the agent never talks to it directly, because the agent doesn't hold a WebSocket connection at all — it runs entirely server-side, inside `agent_worker`, and reaches recipients purely through the Redis Stream/pub-sub pipeline described above, which the gateway is already the terminal consumer of. From the gateway's point of view, an agent-originated message is just another `new_message` event arriving on `instance_inbox:{server_id}`.
- **Shared send-rate budget (ADR 0058)**: because the agent sends *as* the owner, and the owner's own WebSocket send throughput is capped by the Rust gateway's sliding-window Lua script (`rlsw:send_message:{user_id}` 3/1s + `rlsw:send_message_burst:{user_id}` 40/60s), the agent's in-process send path reads and writes the **exact same Redis keys** — not a separate or unlimited budget — before calling `process_outgoing`. On rejection it retries with exponential backoff rather than failing the tool call outright, bounded ultimately by the per-turn timeout. This closes what would otherwise be a real bypass: without it, the agent could out-throughput the very rate limit the gateway enforces on the human it's acting for.
- **`app_worker_alive` liveness gate (ADR 0041)**: the gateway refuses to ack a `send_message`/`mark_*` frame from a human client if the Python app's stream-consumer processes aren't running (a `SET … EX 10s` liveness key refreshed every consumer loop iteration). The agent worker is a separate process from the main app and is not itself gated by this key — its own failure mode is the per-turn timeout and the worker pool's own crash isolation, not this particular liveness check, which exists specifically to protect the human-facing WS acknowledgment contract.

### 4.4 Budget and usage tracking as a real-time concern

Resource consumption is tracked live, in Redis, and surfaced back to the owner without a batch/offline reporting step:

- Two independent rolling token-usage windows (5h/500k tokens, 7d/3M tokens, combined input+output) are fixed-window Redis counters, updated immediately after every Gemini **and** `jev` call returns — `jev` calls bill input tokens only, estimated (no completion side to measure, and the API returns no usable token count). Before each Gemini call, the tighter of the two windows' remaining budget is checked against a char-per-token estimate of that call's own input; a call whose input already exceeds what's left is skipped outright rather than spent on a certain truncation, and `maxOutputTokens` is capped to whatever budget remains for calls that do proceed.
- `GET /agents/me/usage` is a read-only endpoint over the same counters (`token_budget.peek_usage`), polled by the PoC frontend every 30 seconds while the agent drawer is open — the UI reflects live server state, not a cached snapshot, and locally disables the composer once a window is exhausted (server-side enforcement is the real gate; the UI disablement is convenience only).
- Daily active-processing-time (Gemini calls + tool execution wall-clock, not calendar time) is tracked the same way and gates whether the agent processes triggers at all that day, independent of the token windows and the hourly activation quota — three separate budget dimensions, each with its own Redis key and its own reset cadence, all readable live via the capacity-introspection tool (`get_capacity_status`) without incrementing any of them.

---

## Summary of key files

| Concern | Location |
|---|---|
| Schema, restrictions/triggers shapes | `modules/agents/models.py` |
| Trigger Rule Engine | `modules/agents/trigger_engine.py` |
| Turn execution, round-trip loop | `modules/agents/invoke_worker.py`, `invoke_turn_helpers.py` |
| Tool schemas / dispatch / handlers | `modules/agents/tools/{schemas,dispatch,execution,config_mode,common}.py` |
| Gemini client | `modules/agents/gemini_client.py` |
| `jev` classifier client | `modules/agents/typesafe_client.py` |
| Judge gate | `modules/agents/judge.py` |
| DB-level restriction triggers | `modules/agents/restriction_ddl.py` (ADR 0066) |
| Token/time budgets | `modules/agents/token_budget.py`, `time_budget.py` |
| Knowledge base / Agentic RAG | `modules/agents/knowledge_service.py`, `knowledge_crud.py`, `knowledge_ddl.py`, `chunking.py` |
| Platform semantic search (messages) | `modules/vector_search/` (ADR 0042) |
