import os

# --- AI agent (ADR 0045) ---
# Trigger Rule Engine: evaluated synchronously but cheaply inside the
# message-persistence path (modules/messaging/send.py), right after a
# message is saved, in parallel with the existing fan-out - never blocking
# it. A matched + quota-allowed trigger pushes an event onto this stream for
# the (not yet built) agent_worker to consume.
AGENT_INVOKE_STREAM_KEY = os.environ.get("AGENT_INVOKE_STREAM_KEY", "agent_invoke_stream")
AGENT_INVOKE_STREAM_MAXLEN = int(os.environ.get("AGENT_INVOKE_STREAM_MAXLEN", "100000"))

# Hourly activation quota - fixed-window counter via infra.ratelimit
# (agent:{agent_id}:activations). Exceeding it silently drops the trigger
# (the message is still delivered normally; the agent just doesn't respond).
AGENT_ACTIVATION_QUOTA_PER_HOUR = int(os.environ.get("AGENT_ACTIVATION_QUOTA_PER_HOUR", "20"))
AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS = int(
    os.environ.get("AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS", "3600")
)

# --- agent_worker consumer (step 3) ---
# Consumer group name for agent_invoke_stream. Single stream, not chat_id-
# sharded (unlike message_send_stream) - invocation volume per agent is
# already bounded by the hourly activation quota above.
AGENT_INVOKE_STREAM_GROUP = os.environ.get("AGENT_INVOKE_STREAM_GROUP", "agent_worker")
AGENT_INVOKE_STREAM_BATCH = int(os.environ.get("AGENT_INVOKE_STREAM_BATCH", "10"))
AGENT_INVOKE_STREAM_BLOCK_MS = int(os.environ.get("AGENT_INVOKE_STREAM_BLOCK_MS", "5000"))
AGENT_INVOKE_STREAM_CLAIM_IDLE_MS = int(
    os.environ.get("AGENT_INVOKE_STREAM_CLAIM_IDLE_MS", "60000")
)
# Bounds how many invocations one agent_worker process runs concurrently
# (asyncio.Semaphore) - independent of the per-agent Gemini/tool rate limits,
# this just caps this process's own memory/DB-connection footprint.
AGENT_WORKER_CONCURRENCY = int(os.environ.get("AGENT_WORKER_CONCURRENCY", "10"))

# Daily active-processing-time budget (seconds), ADR 0045. 1 hour default.
AGENT_DAILY_ACTIVE_SECONDS_BUDGET = int(
    os.environ.get("AGENT_DAILY_ACTIVE_SECONDS_BUDGET", str(60 * 60))
)

# --- Gemini turn (step 4) ---
# Strictly per-agent, fixed-window via infra.ratelimit (agent_gemini_calls:
# {agent_id}) - gates API usage once a turn is already running, independent
# of the hourly activation quota above (which gates queue entry).
# ADR 0047 decision 1: raised 5 -> 30 for the paid Gemini tier - the old
# number was sized to a shared free-tier quota that no longer applies; this
# is now cost/abuse protection (a bug or a prompt-injected loop running up a
# real bill), not quota protection.
AGENT_GEMINI_CALLS_PER_MINUTE = int(os.environ.get("AGENT_GEMINI_CALLS_PER_MINUTE", "30"))
AGENT_GEMINI_CALLS_WINDOW_SECONDS = int(os.environ.get("AGENT_GEMINI_CALLS_WINDOW_SECONDS", "60"))

# Function-calling round-trips per turn. ADR 0047 decision 1: raised 4 -> 8 -
# Agentic RAG (get_knowledge_index followed by one or more fetch_chunk calls)
# legitimately needs more than 4 round-trips in one turn; the old cap was
# sized specifically to fit under the old 5/min limit above, which no longer
# applies.
AGENT_TURN_MAX_TOOL_ROUNDTRIPS = int(os.environ.get("AGENT_TURN_MAX_TOOL_ROUNDTRIPS", "8"))

# Whole-turn wall-clock timeout (asyncio.wait_for) - aborts a stuck turn
# cleanly instead of holding a worker slot indefinitely.
AGENT_TURN_TIMEOUT_SECONDS = float(os.environ.get("AGENT_TURN_TIMEOUT_SECONDS", "90"))

# --- Trigger pre-filter cache (ADR 0046, decision 1) ---
# SET of owner_user_ids with Agent.is_enabled=true - SISMEMBER lets
# evaluate_triggers skip Postgres entirely for the common case (no agent in
# this chat). STRING per owner holds the JSON trigger_cfg (agent id +
# triggers). Both are cache-aside over Postgres, rewritten on every
# trigger-affecting write and self-healing on a cache miss.
AGENT_ENABLED_OWNERS_SET_KEY = os.environ.get("AGENT_ENABLED_OWNERS_SET_KEY", "agent:enabled_owners")
AGENT_TRIGGER_CFG_KEY_PREFIX = os.environ.get("AGENT_TRIGGER_CFG_KEY_PREFIX", "agent:trigger_cfg:")

# --- on_unknown_sender trigger (ADR 0046, decision 2) ---
# Per-sender daily cap, on top of - not instead of - AGENT_ACTIVATION_QUOTA_
# PER_HOUR above: caps how many times a single unknown sender can wake the
# agent per day, independent of (but still bounded by) the hourly budget.
# Fixed-window key: ratelimit:agent_unknown_sender:{agent_id}:{sender_user_id}
AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY = int(
    os.environ.get("AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY", "20")
)
AGENT_UNKNOWN_SENDER_QUOTA_WINDOW_SECONDS = int(
    os.environ.get("AGENT_UNKNOWN_SENDER_QUOTA_WINDOW_SECONDS", str(24 * 60 * 60))
)

# ADR 0051: once on_unknown_sender fires for a chat and the turn is actually
# enqueued, that chat is auto-registered into on_specific_chats (empty
# keywords, tagged with _auto_added_at) so the agent keeps responding to the
# same person afterward. Cap on how many such auto-added entries one agent
# can hold at once - oldest _auto_added_at evicted first (FIFO) on overflow.
# Manually-added on_specific_chats entries (no _auto_added_at) never count
# against this cap and are never evicted by it.
AGENT_MAX_AUTO_CHATS = int(os.environ.get("AGENT_MAX_AUTO_CHATS", "200"))

# --- on_schedule trigger (ADR 0046, decision 3) ---
# Cap on how many schedule entries one agent can hold - enforced at
# PATCH /agents/me and in the update_own_triggers tool (a self-editing agent
# cannot schedule its way past the cap).
AGENT_MAX_SCHEDULE_ENTRIES = int(os.environ.get("AGENT_MAX_SCHEDULE_ENTRIES", "10"))

# Redis ZSET name for the "when to next check" index (member = "{agent_id}:
# {schedule_id}", score = next-fire unix timestamp). The schedule definition
# itself lives in Agent.triggers.on_schedule (Postgres, source of truth).
AGENT_SCHEDULE_DUE_ZSET_KEY = os.environ.get("AGENT_SCHEDULE_DUE_ZSET_KEY", "agent_schedule_due")

# How often the agent_worker's schedule poll loop checks the ZSET for due
# entries (ZRANGEBYSCORE -inf now).
AGENT_SCHEDULE_POLL_INTERVAL_SECONDS = int(
    os.environ.get("AGENT_SCHEDULE_POLL_INTERVAL_SECONDS", "30")
)

# --- Knowledge base / RAG (ADR 0046, decision 4) ---
# Server-side chunker for text/Markdown uploads (fixed-size/overlap, no NLP -
# cheap on the 1GB host). The client-side PDF chunker mirrors these same
# numbers so retrieval quality doesn't depend on which path a document took.
AGENT_KNOWLEDGE_CHUNK_MAX_CHARS = int(os.environ.get("AGENT_KNOWLEDGE_CHUNK_MAX_CHARS", "1500"))
AGENT_KNOWLEDGE_CHUNK_OVERLAP_CHARS = int(
    os.environ.get("AGENT_KNOWLEDGE_CHUNK_OVERLAP_CHARS", "200")
)

# Guardrails enforced at upload (modules/agents/knowledge_service.py).
AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT = int(
    os.environ.get("AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT", "20")
)
AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT = int(
    os.environ.get("AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT", "2000")
)

# Top-N chunks returned by search_knowledge_chunks (still used elsewhere;
# get_knowledge_index/fetch_chunk, ADR 0047 decision 5, don't take a limit -
# they're capped by AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT instead).
AGENT_KNOWLEDGE_SEARCH_LIMIT = int(os.environ.get("AGENT_KNOWLEDGE_SEARCH_LIMIT", "5"))

# Allowed upload MIME types for knowledge documents - text/Markdown are
# chunked server-side, PDF is parsed+chunked client-side (pdf.js) and only
# the resulting chunk array is POSTed; the raw PDF bytes still go to S3
# (reference/download only, never re-parsed server-side).
AGENT_KNOWLEDGE_ALLOWED_MIME = {"text/plain", "text/markdown", "application/pdf"}
AGENT_KNOWLEDGE_SERVER_CHUNKED_MIME = {"text/plain", "text/markdown"}

# Size ceiling for a single knowledge document upload, in bytes. Pinned into
# the presigned PUT (Content-Length) like every other upload kind.
AGENT_KNOWLEDGE_MAX_UPLOAD_BYTES = int(
    os.environ.get("AGENT_KNOWLEDGE_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))
)

# --- Turn history transcript (invoke_worker.py) ---
# Char cap on the formatted "sender: content" transcript seeded into a turn's
# first Gemini prompt (_build_initial_contents/_build_schedule_contents) -
# the 20-message window itself has no size bound, so a burst of long messages
# could otherwise blow up prompt size/cost. Truncated from the start (oldest
# lines dropped first) so the most recent context is always kept.
AGENT_HISTORY_TRANSCRIPT_MAX_CHARS = int(
    os.environ.get("AGENT_HISTORY_TRANSCRIPT_MAX_CHARS", "4000")
)

# --- BYOK: bring your own Gemini key (ADR 0046, decision 5) ---
# Fernet key used to encrypt Agent.encrypted_gemini_api_key at rest. Only
# required if any owner actually sets a custom key - modules/agents/crypto.py
# raises at encrypt/decrypt time (not at import time) if this is unset, so a
# deployment that never uses BYOK doesn't need it. Separate from
# GEMINI_API_KEY (the shared key) and JWT_SECRET_KEY.
AGENT_BYOK_ENCRYPTION_KEY = os.environ.get("AGENT_BYOK_ENCRYPTION_KEY", "")

__all__ = [
    "AGENT_INVOKE_STREAM_KEY",
    "AGENT_INVOKE_STREAM_MAXLEN",
    "AGENT_ACTIVATION_QUOTA_PER_HOUR",
    "AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS",
    "AGENT_INVOKE_STREAM_GROUP",
    "AGENT_INVOKE_STREAM_BATCH",
    "AGENT_INVOKE_STREAM_BLOCK_MS",
    "AGENT_INVOKE_STREAM_CLAIM_IDLE_MS",
    "AGENT_WORKER_CONCURRENCY",
    "AGENT_DAILY_ACTIVE_SECONDS_BUDGET",
    "AGENT_GEMINI_CALLS_PER_MINUTE",
    "AGENT_GEMINI_CALLS_WINDOW_SECONDS",
    "AGENT_TURN_MAX_TOOL_ROUNDTRIPS",
    "AGENT_TURN_TIMEOUT_SECONDS",
    "AGENT_ENABLED_OWNERS_SET_KEY",
    "AGENT_TRIGGER_CFG_KEY_PREFIX",
    "AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY",
    "AGENT_UNKNOWN_SENDER_QUOTA_WINDOW_SECONDS",
    "AGENT_MAX_AUTO_CHATS",
    "AGENT_MAX_SCHEDULE_ENTRIES",
    "AGENT_HISTORY_TRANSCRIPT_MAX_CHARS",
    "AGENT_SCHEDULE_DUE_ZSET_KEY",
    "AGENT_SCHEDULE_POLL_INTERVAL_SECONDS",
    "AGENT_KNOWLEDGE_CHUNK_MAX_CHARS",
    "AGENT_KNOWLEDGE_CHUNK_OVERLAP_CHARS",
    "AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT",
    "AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT",
    "AGENT_KNOWLEDGE_SEARCH_LIMIT",
    "AGENT_KNOWLEDGE_ALLOWED_MIME",
    "AGENT_KNOWLEDGE_SERVER_CHUNKED_MIME",
    "AGENT_KNOWLEDGE_MAX_UPLOAD_BYTES",
    "AGENT_BYOK_ENCRYPTION_KEY",
]
