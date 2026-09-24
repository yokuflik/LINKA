# AI Agent — implementation history (ADR 0045 steps 1-5, ADR 0046 decisions 1-6)

Split out of `.claude_docs/ai_agent.md` on 2026-09-23 once that file passed
the ~300-line split threshold again. This file is the detailed build log for
everything that landed *before* ADR 0047 - current state, schema, and rate
limits stay in `ai_agent.md`; ADR 0047's own implementation log lives in
`docs/adr/0047-agent-skills-tool-mode-gate-and-escalation.md` directly (per
the user's explicit request to keep that ADR's log alongside the ADR).

## Status

**Step 1 (schema) DONE.** `modules/agents/models.py` (`Agent`,
`AgentToolCallLog`), wired into `scripts/init_db.py`. No migration system in
this repo - tables are created by `create_all`. Verified against the
ephemeral test DB (docker `test_db`, port 5433).

**Step 2 (Trigger Rule Engine + hourly quota) DONE.**
- `modules/agents/trigger_engine.py`: `evaluate_triggers(message)` opens its
  own `session_scope()` (never reuses the send path's session, which may
  already be committed/closed by the time the task runs) and:
  1. Skips system messages / messages with no sender.
  2. Loads the chat's other participants, finds their enabled `Agent` rows
     (`modules/agents/crud.get_enabled_agents_for_owners`).
  3. Per candidate agent, `_matches_trigger`: `blocked_read_chat_ids` skip,
     `on_specific_chats[chat_id]` presence + keyword substring match (case-
     insensitive; no match on media-only messages if keywords are set), then
     `on_time_window` (UTC clock - `Agent`/`triggers` carry no timezone
     field yet, so this is a known simplification, not yet owner-local).
  4. Hourly activation quota: `infra.ratelimit.service.check_and_increment(
     agent.id, "agent_activation", settings.AGENT_ACTIVATION_QUOTA_PER_HOUR,
     settings.AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS)` - fixed-window, key
     `ratelimit:agent_activation:{agent_id}`. Exceeded -> silently dropped,
     no backlog (message still delivered normally).
  5. On success: `modules/agents/invoke_queue.enqueue_invocation` XADDs
     `{agent_id, chat_id, message_id}` onto `agent_invoke_stream`
     (`settings.AGENT_INVOKE_STREAM_KEY`, `AGENT_INVOKE_STREAM_MAXLEN`
     capped, `approximate=True`). No consumer group yet - created when the
     worker (step 3) lands.
  - Whole evaluation wrapped in try/except in `evaluate_triggers` - any
    failure is logged and swallowed, never surfaced to the sender.
- Hook site: `modules/messaging/send.py::process_outgoing`, right after
  `send_queue.enqueue_fanout`, via `asyncio.create_task(evaluate_triggers(
  message))` - fire-and-forget, parallel to fan-out, never blocks or fails
  the send path.
- New config module `config/agent_settings.py` (registered in both
  `config/__init__.py`'s import tuple and `config/_accessor.py`'s
  `_MODULES`, ADR 0019/0029 pattern): `AGENT_INVOKE_STREAM_KEY`,
  `AGENT_INVOKE_STREAM_MAXLEN`, `AGENT_ACTIVATION_QUOTA_PER_HOUR` (20),
  `AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS` (3600).
- No schema change, no new tests added (no test harness convention yet for
  the agents module - covered incidentally by the full suite staying green,
  430 tests).

**Step 3 (consumer + `agent_worker` container) DONE.**
- `modules/agents/invoke_worker.py`: `AgentInvokeConsumer(BaseStreamConsumer)`
  (reuses the same shared consumer machinery as
  `realtime/fanout/base_worker.py` / `modules/receipts/receipt_log.py` -
  XREADGROUP + XAUTOCLAIM + XACK, transient failures left unacked for
  reclaim). Single stream, `shard_count=1` (not chat_id-sharded like
  `message_send_stream` - invocation volume is already bounded by the hourly
  activation quota from step 2). `process_entry`:
  1. Re-checks `Agent.is_enabled` at dequeue time (defense in depth for the
     enqueue-to-dequeue window, `modules.agents.crud.get_agent_by_id`) -
     missing/disabled agent -> skip (permanent, acked, not retried).
  2. Re-checks the daily active-time budget
     (`modules/agents/time_budget.py::has_budget_remaining`) - exhausted ->
     skip.
  3. Times `_run_turn` (currently a **placeholder stub** - just logs; the
     real Gemini call + tool dispatch is step 4) and records elapsed seconds
     via `time_budget.record_active_seconds`, in a `finally` so a failed turn
     still counts against the budget.
  - Concurrency bounded by `asyncio.Semaphore(AGENT_WORKER_CONCURRENCY)`
    inside `process_entry`, independent of the per-agent Gemini-call rate
    limit (that gates API usage once a turn is running; this just caps one
    worker process's own memory/DB-connection footprint).
- `modules/agents/time_budget.py`: `has_budget_remaining` /
  `record_active_seconds` - a dedicated Redis fixed-window counter
  (`ratelimit:agent_active_seconds:{agent_id}`, 24h TTL) counting elapsed
  seconds rather than call count, so it doesn't fit
  `infra.ratelimit.service.check_and_increment` (always +1/call). Same
  `ratelimit:` key prefix convention, but agent-specific so it lives in
  `modules/agents/` rather than the generic `infra/ratelimit/`.
- `modules/agents/crud.py::get_agent_by_id` added (plain `session.get`).
- `agent_worker_main.py` (repo root, parallel to `main.py`): standalone
  entrypoint - `load_dotenv()`, SIGTERM/SIGINT -> `stop_event.set()` for
  graceful drain (bounded by `AGENT_INVOKE_STREAM_BLOCK_MS`, default 5s),
  then `dispose_engine()` / `close_redis()` / `id_client.close()` on exit.
  Deliberately **not** a task in `main.py`'s FastAPI lifespan like the other
  stream consumers (receipt/send/fan-out/scheduled) - a stuck/crashing
  Gemini turn must never affect the WS gateway hand-off or message delivery.
- `docker-compose.prod.yml`: new `agent_worker` service - same
  `linka-app:latest` image/build as `app`, `command: ["python",
  "agent_worker_main.py"]`, own `SERVER_ID` suffixed `-agent-worker` (so its
  ADR 0041 liveness key is never conflated with the app's), `mem_limit: 192m`
  / `cpus: 0.5`. No published port (not HTTP-facing, nothing in Caddy routes
  to it). Horizontal scaling = more replicas of this one service.
- New config in `config/agent_settings.py`: `AGENT_INVOKE_STREAM_GROUP`
  (`"agent_worker"`), `AGENT_INVOKE_STREAM_BATCH` (10),
  `AGENT_INVOKE_STREAM_BLOCK_MS` (5000), `AGENT_INVOKE_STREAM_CLAIM_IDLE_MS`
  (60000), `AGENT_WORKER_CONCURRENCY` (10),
  `AGENT_DAILY_ACTIVE_SECONDS_BUDGET` (3600). Documented (commented, opt-in
  override) in `deploy/env.production.example`.
- No schema change. 430 tests pass (suite doesn't yet cover this module -
  same gap noted in step 2). Manually smoke-tested against the dev stack
  (`docker-compose.yml` test_redis/test_db): enqueue -> `drain_once` ->
  correct skip-and-ack for a nonexistent agent; `time_budget` counter/TTL/
  exhaustion behavior verified directly.

**Step 4 (tool registry + Gemini client, wired into `_run_turn`) DONE.**
Decisions locked in before coding (user confirmed 2026-09-13):
- Raw HTTPS via `httpx`, no `google-generativeai` SDK - consistent with
  `modules/vector_search/gemini_client.py` (ADR 0042).
- Model: `gemini-2.0-flash` (the plan's original "1.5 Flash" is being
  retired by Google; user chose the 2.0 successor over keeping 1.5 literal).
- `read_history` returns structured `{sender_id, timestamp, content}` per
  message (not raw concatenated text) - needed for the model to tell senders
  apart in group chats.
- `AgentToolCallLog` logs every tool call attempt, allowed or denied.
- Per-turn timeout 20s (`asyncio.wait_for`); tool round-trip cap 4 (matches
  the ADR's 1 initial + 4 round-trips = 5 calls/min ceiling).

- `modules/agents/tools.py`: `TOOL_SCHEMAS` (Gemini function-declaration
  JSON) + `execute_tool_call(session, agent, tool_name, arguments)`. Six
  tools, each dispatching to the existing facade
  (`modules.messaging.service.process_outgoing` for `send_message`/
  `reply_message`, `modules.chats.service.get_or_create_private_chat` for
  `create_chat`, `.remove_member` for `leave_group`,
  `modules.messaging.read_api.get_message_history` for `read_history`, and
  `modules.agents.crud.update_agent_triggers` - new, shallow-merges a patch
  into `Agent.triggers` - for `update_own_triggers`). Every tool call acts as
  `agent.owner_user_id`, so it rides the same per-user rate limits /
  permission checks as a human client - never a bypass. `restrictions` is
  checked before dispatch (`can_send_messages`, `can_message_groups`/
  `_private`, `can_message_new_private_contacts`, `can_leave_groups`,
  `blocked_read_chat_ids`); a `ToolDeniedError` short-circuits to a logged
  denial and `{"error": reason}` handed back to Gemini so it can adapt.
  `_log_call` always writes an `AgentToolCallLog` row (allowed or not) before
  returning, id via `infra.ids.client.next_id()`. `max_messages_per_day` IS
  enforced (`_check_daily_send_quota`, `send_message`/`reply_message` only):
  a fixed-window counter via `infra.ratelimit.service.check_and_increment(
  agent_id, "agent_messages_per_day", limit, 86400)` - null/absent limit
  skips the check entirely (no Redis round trip for agents that never set
  it). Exceeding it raises `ToolDeniedError`, logged like any other denial.
- `modules/agents/gemini_client.py`: `generate_turn(system_prompt, contents,
  tool_schemas)` posts to `.../v1beta/models/gemini-2.0-flash:generateContent`
  with `tools:[{functionDeclarations: TOOL_SCHEMAS}]` and
  `systemInstruction` set from `Agent.system_prompt`; reuses
  `settings.GEMINI_API_KEY`/`GEMINI_API_BASE`/`GEMINI_HTTP_TIMEOUT_SECONDS`
  from `config/vector_settings.py` (one Gemini key, two endpoints - no new
  env var). `extract_function_call`/`extract_text`/`function_response_part`
  are small shape helpers around the `candidates[0].content.parts` REST
  response. 429 -> `GeminiChatError`, same as `modules/vector_search`'s
  quota handling.
- `invoke_worker.py::_run_turn` (no longer a stub): opens its own
  `session_scope()` (distinct from `process_entry`'s per-batch session - a
  turn can span several DB commits, one per tool call, so it needs a session
  it fully owns), seeds the conversation with a text-rendered transcript
  from `get_message_history` (last 20 messages of the triggering chat), then
  loops up to `AGENT_TURN_MAX_TOOL_ROUNDTRIPS` (4) times: check the per-agent
  Gemini-call budget (`agent_gemini_calls:{agent_id}`, 5/min via
  `infra.ratelimit.service.check_and_increment`) -> `generate_turn` ->
  if the response is a `functionCall`, run it through
  `tools.execute_tool_call` and commit, append the function response, loop;
  if it's plain text, the turn is over. Hitting the round-trip cap appends a
  synthetic `{"error": "tool round-trip limit reached..."}` function response
  and ends the turn (no 5th call). `process_entry` wraps the whole
  `_run_turn` call in `asyncio.wait_for(AGENT_TURN_TIMEOUT_SECONDS)` (20s) -
  a timeout is logged and swallowed, same as any other turn failure; the
  daily time-budget `finally` still accounts the elapsed wall-clock either
  way.
- New config in `config/agent_settings.py`: `AGENT_GEMINI_CALLS_PER_MINUTE`
  (5), `AGENT_GEMINI_CALLS_WINDOW_SECONDS` (60),
  `AGENT_TURN_MAX_TOOL_ROUNDTRIPS` (4), `AGENT_TURN_TIMEOUT_SECONDS` (20.0).
  (These 2 numbers were later raised by ADR 0047 decision 1 - see
  `ai_agent.md`.)
- No schema change (reuses the step-1 tables). Import-smoke-tested (no
  circular imports across `modules.agents.tools` <-> `modules.chats.service`
  <-> `modules.messaging.service` <-> `modules.agents.trigger_engine`); not
  yet exercised against a live Gemini API key or the ephemeral test DB - no
  test harness convention exists for this module yet (same gap as steps
  2-3).

**Step 5 (backend CRUD + config API) DONE.** Landed first (user confirmed
2026-09-13, since no endpoint existed at all):
- `modules/agents/schemas.py`: `AgentOut` (nested `AgentRestrictionsOut` /
  `AgentTriggersOut`, `id`/`owner_agent_chat_id` as `IdStr`) +
  `AgentConfigPatchIn` (top-level `system_prompt`/`is_enabled`/
  `restrictions`/`triggers`, every field `Optional`, PATCH semantics) with
  matching `*RestrictionsIn`/`*TriggersIn` partial-patch shapes.
- `modules/agents/crud.py` gained `get_agent_by_owner` (one agent per user)
  and `update_agent_config` (applies a patch dict: `system_prompt`/
  `is_enabled` replace outright when present, `restrictions`/`triggers`
  shallow-merge like the existing `update_agent_triggers` tool helper - kept
  separate since this one is reachable for `restrictions` too, which the
  `update_own_triggers` tool must never touch).
- `modules/agents/router.py` (`/agents/me`, registered in `main.py`):
  - `GET /agents/me` -> 404 if the caller has no agent yet.
  - `POST /agents/me` -> idempotent create-if-missing (returns the existing
    agent instead of 409ing if one already exists). Creates the owner-agent
    chat (`modules.chats.crud.crud_chat.create_chat`, `is_group=False`) with
    **only the owner as participant** - the agent has no `user_id` of its
    own, it always acts as the owner (ADR 0045), so this is a 1:1-shaped
    chat with a single member, not a real two-user pair (doesn't go through
    `get_or_create_private_chat`/`private_chat_pairs` at all). Then the
    `Agent` row, `owner_agent_chat_id` pointed at that chat.
  - `PATCH /agents/me` -> `body.model_dump(exclude_unset=True)`, strips
    `None` placeholders out of the nested `restrictions`/`triggers` dicts
    before merging (a field the client didn't touch must not get clobbered
    to null by the partial-patch model's `Optional[...] = None` defaults).
  - No listing/admin endpoints - always "mine", one per user.
- No new tests (same gap as steps 2-4, no test harness convention for this
  module yet). Import-smoke-tested only.

**Frontend (step 5) DONE**, scoped per user decisions (2026-09-13). Fully
superseded by the drawer UI rewrite on 2026-09-23 - see
`.claude_docs/ai_agent_frontend.md` for the current shape. That file also
covers the ADR 0046 decision 4 (knowledge base) and decision 6 (BYOK)
frontend pieces referenced below.

**ADR 0046, decision 1 (O(1) pre-filter cache) DONE.**
- `modules/agents/cache.py` (new): the Redis cache-aside layer.
  - `agent:enabled_owners` (SET) - `owner_user_id`s with `is_enabled=true`.
  - `agent:trigger_cfg:{owner_user_id}` (STRING, JSON `{id, triggers}`) -
    that owner's agent id + full `Agent.triggers`. Deliberately did **not**
    carry `restrictions` - `blocked_read_chat_ids` is re-checked on the full
    `Agent` row right before enqueue instead (see below). Widened by ADR
    0047 decision 5 to also carry `active_skill`/`paused_chat_ids` - see
    `ai_agent.md`.
  - `sync_agent_cache(agent)`: writes both keys when `agent.is_enabled`,
    else calls `remove_agent_cache`. Called after every trigger-affecting
    write: `POST /agents/me` (create), `PATCH /agents/me`
    (`modules/agents/router.py`), and the `update_own_triggers` tool
    (`modules/agents/tools.py::_tool_update_own_triggers`). Best-effort -
    wrapped in try/except, a Redis failure never fails the write it's
    attached to.
  - `remove_agent_cache(owner_user_id)`: SREM + DEL, used when an agent is
    disabled (also called internally by `sync_agent_cache`).
  - `is_owner_cached_enabled` / `get_cached_trigger_cfg`: the read side.
- `modules/agents/trigger_engine.py` rewritten: `_evaluate_triggers` no
  longer calls `get_enabled_agents_for_owners` up front for the whole chat.
  Per candidate owner: `_load_trigger_cfg` does `SISMEMBER
  agent:enabled_owners` then `GET agent:trigger_cfg:{owner_user_id}`;
  `_matches_trigger_config` runs the existing `on_specific_chats`/
  `on_time_window` logic against the cached JSON. Postgres is touched only
  (a) as a **per-owner fallback** on a cache miss (SET hit but STRING
  missing/malformed, or SET itself cold) - `_load_trigger_cfg` falls back to
  `get_enabled_agents_for_owners([owner_user_id])` and calls
  `sync_agent_cache` to repopulate, no separate warm-up script; and (b)
  **once**, after a trigger config match, to load the full `Agent` row via
  `get_agent_by_id` for the `is_enabled` re-check and the
  `blocked_read_chat_ids` check, right before the hourly-quota check and
  `enqueue_invocation` - same defense-in-depth spot as the existing
  `is_enabled` re-check pattern from step 3's worker dequeue.
- No schema change. Import-smoke-tested only (same gap as steps 2-5 - no
  test harness convention yet for this module).

**ADR 0046, decision 2 (unknown-sender trigger) DONE.**
- `modules/agents/models.py::DEFAULT_AGENT_TRIGGERS` gains
  `"on_unknown_sender": {"enabled": false}` - additive, existing rows'
  JSONB defaults unaffected (no migration system, `server_default` only
  matters for new inserts; existing agents just won't have the key until
  they PATCH triggers, and `.get("on_unknown_sender", {}).get("enabled")`
  in the engine treats an absent key the same as `false`).
- `modules/messaging/crud.py::has_prior_messages(session, chat_id,
  before_message_id)` - `SELECT EXISTS(...)` for any real message with
  `id < before_message_id` in that chat; used instead of a `COUNT` so
  Postgres stops at the first match.
- `modules/agents/trigger_engine.py::_matches_unknown_sender` - separate
  from the cache-only `_matches_trigger_config` because it needs a DB round
  trip: loads `Chat.is_group` (`modules.chats.crud.crud_chat.get_chat_by_id`)
  and, only for a private chat, `has_prior_messages`. Fires when the
  triggering message is the *first-ever* message in that private chat.
  `_evaluate_triggers` ORs this with the existing config match (either one
  is enough to enqueue) - both still pass through the same `is_enabled` +
  `blocked_read_chat_ids` re-check and the hourly activation quota.
- Dedicated per-sender daily quota, on top of the hourly activation quota:
  `check_and_increment(f"{agent.id}:{message.sender_id}",
  "agent_unknown_sender", AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY (20),
  AGENT_UNKNOWN_SENDER_QUOTA_WINDOW_SECONDS (86400))` - key
  `ratelimit:agent_unknown_sender:{agent_id}:{sender_user_id}`. Only
  checked when the unknown-sender path matched (not for ordinary
  `on_specific_chats`/`on_time_window` matches).
- `modules/agents/schemas.py`: `AgentUnknownSenderOut`/`In` (`{enabled:
  bool}`), added to `AgentTriggersOut`/`In` - `PATCH /agents/me` already
  shallow-merges `triggers`, so no router change needed.
- No schema/migration change (still the same `triggers` JSONB column).
  Import-smoke-tested only (same gap as prior steps - no test harness
  convention yet for this module).

**ADR 0046, decision 3 (time-schedule trigger) DONE.**
- `modules/agents/models.py::DEFAULT_AGENT_TRIGGERS` gains `"on_schedule": []`
  - additive, same no-migration reasoning as decision 2's `on_unknown_sender`.
  Each entry: `{id, kind: "recurring"|"once", time (recurring, daily HH:MM
  UTC) | at (once, ISO-8601 UTC instant), instruction, chat_id (optional),
  enabled}`.
- `modules/agents/schedule.py` (new): the Redis ZSET layer -
  `agent_schedule_due`, member `{agent_id}:{schedule_id}`, score = next-fire
  unix timestamp. `sync_schedule_zset(agent)` re-derives an agent's ZSET
  membership from `Agent.triggers.on_schedule` (adds/updates live entries,
  removes stale ones) - called after every `on_schedule`-touching write:
  `PATCH /agents/me` (`modules/agents/router.py`) and the
  `update_own_triggers` tool (`modules/agents/tools.py`). `next_daily_occurrence`
  does the recurring-entry next-fire math; `due_members`/`remove_due_member`/
  `reschedule_recurring` are the poll loop's read/write primitives.
- `modules/agents/crud.py::_check_schedule_quota` enforces
  `AGENT_MAX_SCHEDULE_ENTRIES` (10) in both `update_agent_config` (router
  PATCH path) and `update_agent_triggers` (the tool path) - raises
  `ScheduleQuotaExceededError`, mapped to HTTP 422 in the router and to a
  `ToolDeniedError` (denied + logged, same as any other restriction) in the
  tool.
- `modules/agents/invoke_queue.py::enqueue_schedule_fire` - XADDs onto the
  same `agent_invoke_stream` as message-fired invocations, tagged
  `kind=schedule` (vs `kind=message` for the existing path) with
  `{agent_id, schedule_id}` instead of `{chat_id, message_id}`.
- `modules/agents/invoke_worker.py`:
  - `_schedule_poll_loop` runs alongside the existing stream consumer inside
    the same `agent_worker` process (`run_forever` now `asyncio.gather`s
    both) - every `AGENT_SCHEDULE_POLL_INTERVAL_SECONDS` (30), pulls
    `due_members()` and calls `_fire_schedule_entry` for each: re-loads the
    live `Agent` row (defense in depth), re-validates the entry still
    exists/is enabled, `enqueue_schedule_fire`s, then either
    `reschedule_recurring`s (recurring) or flips `enabled: false` on that
    entry + `remove_due_member`s (once) - visible as fired in the UI, not
    silently gone, same as the ADR's spec.
  - `process_entry` now dispatches on `fields["kind"]`: `schedule` re-loads
    the live entry by `schedule_id` (defense in depth for the
    enqueue-to-dequeue window) and calls `_run_turn` with
    `schedule_instruction=entry["instruction"]` + optional `chat_id`;
    `message` (default) is the existing path unchanged.
  - `_run_turn` takes optional `schedule_instruction`; when set, seeds the
    conversation via new `_build_schedule_contents` (the instruction text,
    optionally joined with `chat_id` history) instead of
    `_build_initial_contents` (pure chat history). A schedule-fired turn
    consumes the same hourly activation quota and daily time budget as a
    message-fired turn - no third quota dimension, per the ADR.
- `modules/agents/tools.py`: seventh tool `search_messages(query, chat_id?)`
  - thin wrapper over `modules.search.service.search_in_chat`/`search_global`
    (ADR 0040), scoped to the owner's own chats via the same
    participants-JOIN membership check those already do; `chat_id` also
    checked against `blocked_read_chat_ids`. This is what makes a "check who
    hasn't replied" schedule instruction actually work.
- `modules/agents/schemas.py`: `AgentScheduleEntryOut`/`In`, added as
  `on_schedule: List[...]` to `AgentTriggersOut`/`In` (list replaces
  wholesale on patch, same shallow-merge contract as the other trigger keys).
- New config in `config/agent_settings.py`: `AGENT_MAX_SCHEDULE_ENTRIES` (10),
  `AGENT_SCHEDULE_DUE_ZSET_KEY` (`agent_schedule_due`),
  `AGENT_SCHEDULE_POLL_INTERVAL_SECONDS` (30). Documented in
  `deploy/env.production.example`.
- No schema change (still the same `triggers` JSONB column). Import-smoke-
  tested only (same gap as prior steps - no test harness convention yet for
  this module). Frontend not wired for `on_schedule` at the time this
  decision landed - backend-only, same as decision 2 at first.

**ADR 0046, decision 4 (knowledge base / RAG) DONE, full stack.**
- New tables (`modules/agents/models.py`): `AgentKnowledgeDocument`
  (`id`, `agent_id` FK, `filename`, `s3_key`, `mime_type`,
  `status` "processing"|"ready"|"failed", `created_at`) and
  `AgentKnowledgeChunk` (`id`, `agent_id` denormalized, `document_id` FK,
  `chunk_index`, `content`, `content_tsv`), both wired into
  `scripts/init_db.py`'s model-import list and `create_all` - no migration
  system, per repo convention.
- `modules/agents/knowledge_ddl.py` (mirrors `modules/search/ddl.py`
  exactly): `btree_gin` extension + a BEFORE INSERT/UPDATE trigger keeping
  `content_tsv` in lockstep with `content` (`to_tsvector('simple', ...)`) +
  one `gin (agent_id, content_tsv)` partial-free composite index. Applied by
  both `scripts/init_db.py` (after `create_all`, before `ensure_partitions`)
  and `tests/conftest.py`'s `session_factory` fixture (which also gained the
  missing `modules.agents.models` import - the agents tables were not
  created in the ephemeral test DB at all before this). Verified end-to-end
  against a live ephemeral Postgres: trigger populates `content_tsv` on
  insert, `websearch_to_tsquery` match confirmed.
- `modules/agents/chunking.py`: `chunk_text(text, max_chars, overlap_chars)`
  - fixed-size/overlap splitter, no NLP (cheap). Config in
    `config/agent_settings.py`: `AGENT_KNOWLEDGE_CHUNK_MAX_CHARS` (1500),
    `AGENT_KNOWLEDGE_CHUNK_OVERLAP_CHARS` (200). The PoC's client-side PDF
    chunker (`useKnowledgeUpload.js::chunkText`) duplicates this exact
    algorithm/constants in JS so retrieval quality doesn't depend on which
    path a document took - keep both in lockstep if the constants change.
- `modules/agents/knowledge_service.py`: `create_knowledge_upload_ticket`
  (reuses `modules.media.media_service.create_upload_ticket` under a new
  upload kind `"agent_knowledge"` - own MIME allowlist/size ceiling in
  `config/storage_settings.py`, private `S3_BUCKET_MEDIA` bucket, own
  `MAX_UPLOAD_BYTES_AGENT_KNOWLEDGE` env, default 20MB).
  `commit_knowledge_document`: for `text/plain`/`text/markdown`
  (`AGENT_KNOWLEDGE_SERVER_CHUNKED_MIME`) fetches the uploaded object via a
  presigned GET + `httpx` and chunks it server-side (`chunks` argument must
  be empty); for `application/pdf` the caller-supplied `chunks` (computed
  client-side) are used as-is and the server never touches the PDF bytes.
  Enforces `agent_crud.check_knowledge_quota` (raises
  `KnowledgeQuotaExceededError`, mapped to 422) before writing any row.
  `delete_knowledge_document` best-effort deletes the S3 object (logged, not
  fatal) then cascades the DB rows via the FK.
- `modules/agents/crud.py` additions: `count_knowledge_documents`,
  `count_knowledge_chunks`, `check_knowledge_quota` (enforces
  `AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT`=20 and
  `AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT`=2000), `list_knowledge_documents`,
  `get_knowledge_document` (hard-scoped to `agent_id`, so one agent can never
  fetch/delete another's document by guessing an id), `delete_knowledge_document`,
  `search_knowledge_chunks` (`websearch_to_tsquery` + `ts_rank`, top-N via
  `AGENT_KNOWLEDGE_SEARCH_LIMIT`=5, hard-scoped to `agent_id`). Later
  superseded for the tool-facing use case by `list_knowledge_index`/
  `get_knowledge_chunk` (ADR 0047 decision 5) - `search_knowledge_chunks`
  itself stays, just unreferenced by any tool now.
- New endpoints on `modules/agents/router.py` (all under `/agents/me`, same
  "always mine" convention as the rest of this router):
  `POST /me/knowledge/upload-ticket` (presigned PUT), `POST /me/knowledge`
  (commit - fetches+chunks server-side or accepts client chunks depending on
  mime), `GET /me/knowledge` (list), `DELETE /me/knowledge/{document_id}`
  (204, 404 if not this agent's). A shared `_get_my_agent_or_404` helper was
  extracted (previously duplicated inline in `get_my_agent`/`patch_my_agent`).
- Eighth tool `search_knowledge(query)` in `modules/agents/tools.py` - thin
  wrapper over `crud.search_knowledge_chunks`, hard-scoped to `agent.id`
  (never crosses into another agent's documents), returned
  `{document_id, chunk_index, content}` per hit. **Removed from the tool
  registry/schemas by ADR 0047 decision 5**, superseded by
  `get_knowledge_index`/`fetch_chunk` - see `ai_agent.md`.
- **Frontend, full stack** (built against the drawer UI that landed
  concurrently - see "Drawer UI" section below, not the old
  `AgentConfigModal.js`): `poc/vendor/pdfjs/` (vendored pdf.js 4.0.379 legacy
  build, ES-module-only - no CDN, `pdfjs-loader.mjs` bridges it onto
  `window.pdfjsLib` since the rest of the PoC is plain classic `<script>`
  tags with no build step). **Requires serving the PoC over
  `http://localhost` (not `file://`)** - ES module `import` of local files is
  blocked cross-origin under `file://`; this was an explicit user-confirmed
  tradeoff over pinning an older, less-maintained pdf.js with a classic UMD
  build. `useKnowledgeUpload.js` checks `window.pdfjsLib` before use and
  throws a friendly error (routed through the normal `InlineAlert`/
  `friendlyError` path) if it never loaded (e.g. opened via `file://`).
  `extractPdfChunks` renders each page's text via `getTextContent()` and
  chunks client-side; text/markdown files upload raw and skip chunking
  client-side entirely. Upload flow mirrors `useMediaUpload.js`'s
  ticket→PUT→commit shape (plain `fetch` PUT, no XHR progress - knowledge
  uploads don't need a progress ring). Wired into `AgentSettingsView.js`'s
  settings body as a "Knowledge base" section (upload button + document list
  with per-row delete); `openAgentSettings` in `useAgentConfig.js`
  lazy-loads the list on first entry into the settings view
  (`knowledgeLoaded` guard, same idiom as `agentMessagesLoaded`).
- No schema/behavior change to any existing table or tool.
  `MAX_UPLOAD_BYTES_AGENT_KNOWLEDGE`/`AGENT_KNOWLEDGE_ALLOWED_MIME` etc. are
  additive config only.

**Frontend drawer UI (AGENT_DRAWER_UI_PLAN.md) DONE, both waves.** Frontend
detail (component/composable shapes, the agent-chat-outside-the-store
design, `useWsRouter.js` wiring) moved to `.claude_docs/ai_agent_frontend.md`
on 2026-09-23 once this file passed the ~300-line split threshold. Backend
side of Wave 2, landed 2026-09-23:
- `modules/agents/router.py`'s `PATCH /agents/me` now publishes
  `{"event": "agent_config_changed", "agent": <AgentOut>}` over the owner's
  personal channel (`realtime.realtime_service.publish_user_event`, same
  primitive as `chat_pin_changed`/`chat_mute_changed`) after every successful
  patch - cross-tab/cross-device sync, acting tab included (self-echo, no-op
  if already applied).
- `modules/agents/invoke_worker.py::_run_turn` now publishes
  `{"event": "agent_thinking", "status": "started"|"tool_call"|"done"|
  "error", "detail": <label|None>}` to the owner's personal channel: once at
  turn start, once before each tool dispatch (`_TOOL_THINKING_LABELS` maps
  tool name -> a short human label, "Working…" fallback for anything
  unmapped), and exactly once at turn end via a `try/finally` wrapping the
  whole turn body (covers every early-return path: disabled agent, BYOK
  decrypt failure, Gemini budget exhausted, Gemini call failure, round-trip
  cap, plain-text response). Ephemeral - never persisted, no new rate limit
  (bounded by the existing turn's own round-trip cap).
- `modules/agents/trigger_engine.py::_evaluate_triggers` gained an
  owner-chat direct-wake special case, checked **before** the normal
  "other participants' agents" loop: `modules/agents/crud.py::
  get_agent_by_owner_chat(session, chat_id)` looks up the agent whose
  `owner_agent_chat_id` matches the incoming message's chat; if the sender
  is that agent's own owner, it's woken directly (`is_enabled` + the
  existing hourly `agent_activation` quota still apply) - bypassing
  `on_specific_chats`/`on_time_window`/keyword gating entirely, since a
  message the owner sends into their own agent's dedicated chat is always
  an explicit, deliberate wake. Without this, that chat's only participant
  being the owner meant the normal loop (which only ever considers *other*
  participants' agents) never enqueued anything for it.
- `modules/agents/models.py::DEFAULT_AGENT_RESTRICTIONS`: `can_message_groups`
  default flipped `true` -> `false` for *newly created* agents only (no
  migration/backfill - explicit user go-ahead 2026-09-23, CLAUDE.md Rule 10).
  Existing agents keep whatever value they already have.
- The Rust `ws_gateway` needed **no changes** - `fanin.rs::handle_user_event`
  already forwards any personal-channel event's raw JSON to every one of the
  user's live connections regardless of `event` value (same code path that
  already carries `chat_pin_changed` etc.), confirmed by reading
  `crates/ws_gateway/src/fanin.rs`.

**Not done / follow-ups (as of 2026-09-23, still open):**
- Owner-agent-chat avatar showing the agent picture (currently falls back
  to initials).
- Tool-call log endpoint + UI.
- A real chat picker for `on_specific_chats` (currently raw chat-id text
  entry).
- No backend tests added (same gap as every step/decision in this file).
- Frontend toggle for `on_unknown_sender` not yet added.
- Frontend UI for `on_schedule` (add/edit/remove entries) not yet added.
- Config-UI surface for paused-chats / resume (ADR 0047 decision 5's backend
  is done - see `ai_agent.md` - but no drawer UI yet).

**ADR 0046, decision 5 (BYOK Gemini key) DONE, backend only.**
- `modules/agents/models.py::Agent.encrypted_gemini_api_key` (`LargeBinary`,
  nullable) - Fernet ciphertext; `NULL` means "use the shared
  `settings.GEMINI_API_KEY`". `scripts/init_db.py` gained the matching
  `ALTER TABLE agents ADD COLUMN IF NOT EXISTS encrypted_gemini_api_key
  BYTEA` safety-net line (create_all covers fresh DBs; this covers an
  already-deployed one, same pattern as every other column added this way).
- `modules/agents/crypto.py` (new): `encrypt_api_key`/`decrypt_api_key`,
  thin wrapper over `cryptography.fernet.Fernet` keyed by
  `settings.AGENT_BYOK_ENCRYPTION_KEY` (`config/agent_settings.py`, empty by
  default). `ByokKeyError` raised at encrypt/decrypt time (not import time)
  if the env var is unset/invalid or a stored blob fails to decrypt - a
  deployment that never uses BYOK needs no configuration.
  `cryptography==45.0.4` was already a resolved dependency (transitively,
  per the ADR's expectation) - confirmed, no new requirement added.
- `PATCH /agents/me` gains write-only `gemini_api_key: str | None`
  (`AgentConfigPatchIn`) - handled separately from the JSONB shallow-merge
  patch in `modules/agents/router.py::patch_my_agent`: absent from the
  patch leaves the stored key untouched, `""`/`null` clears it
  (`encrypted_gemini_api_key = None`), a non-empty string encrypts and
  replaces it. A `ByokKeyError` here (misconfigured `AGENT_BYOK_ENCRYPTION_
  KEY`) maps to HTTP 500. `AgentOut.has_custom_key: bool` (never the key
  itself) is computed in a new `_agent_out` helper wrapping every response
  from `GET`/`POST`/`PATCH /agents/me`.
- `modules/agents/gemini_client.py::generate_turn` takes an optional
  `api_key` param (`_require_api_key` now takes the override and falls back
  to `settings.GEMINI_API_KEY`); `invoke_worker.py::_run_turn` decrypts
  `agent.encrypted_gemini_api_key` once per turn (not per call) when
  present and threads it through every `generate_turn` call in that turn.
  A decrypt failure (`ByokKeyError`) logs and abandons the turn, same as any
  other turn failure.
- Rate-limit interaction (per the ADR): the hourly activation quota and the
  daily active-time budget still apply unconditionally regardless of whose
  key is spent (they protect our own infra). The shared-key Gemini-call
  throttle (`_check_gemini_call_budget`) is skipped when a BYOK key
  is present - `invoke_worker.py`'s round-trip loop checks `api_key is
  None` before calling it.
- `deploy/env.production.example` documents `AGENT_BYOK_ENCRYPTION_KEY`
  (empty by default, with the `Fernet.generate_key()` one-liner to produce
  one).
- No new tests (same gap as prior steps). Import-smoke-tested + a manual
  encrypt/decrypt round-trip verified directly against a generated Fernet
  key.

**ADR 0046, decision 6 (BYOK frontend) DONE.** UI to set/clear the key and
show status, over decision 5's backend above. See
`.claude_docs/ai_agent_frontend.md`'s "BYOK Gemini key" section for the
component/composable detail.
- ADR 0046 decisions 1-6 are all DONE - no further follow-ups from that ADR.

## Planned pieces (ADR 0045)

1. ~~Trigger Rule Engine hook~~ DONE - see Step 2 above.
2. ~~`agent_invoke_stream` consumer + `agent_worker` container~~ DONE - see
   Step 3 above.
3. ~~Tool registry + `execute_tool_call` + Gemini client, wired into
   `_run_turn`~~ DONE - see Step 4 above.
4. ~~`max_messages_per_day` enforcement~~ DONE - `_check_daily_send_quota`
   in `tools.py`, fixed-window via `infra.ratelimit`.
5. ~~Backend CRUD + config API~~ DONE - see Step 5 above. All ADR 0045
   implementation steps are complete; remaining items are the follow-ups
   listed above.
