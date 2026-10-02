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
# (agent:{agent_id}:activations). Exceeding it drops the trigger (the message
# is still delivered normally) and posts one system-message notice per window
# into the owner's agent chat (see trigger_engine._notify_activation_quota_exceeded).
AGENT_ACTIVATION_QUOTA_PER_HOUR = int(os.environ.get("AGENT_ACTIVATION_QUOTA_PER_HOUR", "100"))
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
# (asyncio.Semaphore, actually enforced in AgentInvokeConsumer._drain_shard's
# per-entry task dispatch - ADR 0094) - independent of the per-agent Gemini/
# tool rate limits, this just caps this process's own memory/DB-connection
# footprint. Default 3: conservative starting point for the 1GB single-host
# demo deploy (ADR 0007) - each concurrent turn holds a DB session/connection
# plus an in-flight Gemini call.
AGENT_WORKER_CONCURRENCY = int(os.environ.get("AGENT_WORKER_CONCURRENCY", "3"))

# --- Message-batch debounce + per-chat turn mutex (ADR 0063) ---
# A matched trigger no longer enqueues onto agent_invoke_stream directly -
# it ZADDs {agent_id}:{chat_id} onto this due-ZSET, score = now + the
# debounce window below. A second match for the same pair before it fires
# just overwrites the score (coalescing a burst of fast messages/self-
# corrections into one turn). Same due-ZSET pattern as
# AGENT_SCHEDULE_DUE_ZSET_KEY, polled by its own tight loop in invoke_worker.
AGENT_INVOKE_DEBOUNCE_ZSET_KEY = os.environ.get(
    "AGENT_INVOKE_DEBOUNCE_ZSET_KEY", "agent_invoke_debounce_due"
)
AGENT_INVOKE_DEBOUNCE_SECONDS = float(os.environ.get("AGENT_INVOKE_DEBOUNCE_SECONDS", "2"))
AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS = float(
    os.environ.get("AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS", "1")
)
# Per-(agent_id, chat_id) mutex held for the duration of one _run_turn call
# (process_entry) so a debounced fire that lands while a previous turn for
# the same pair is still running (up to AGENT_TURN_TIMEOUT_SECONDS) doesn't
# start a second concurrent turn - it re-arms the debounce ZSET instead. TTL
# equals the turn timeout, so a crashed worker holding the lock self-heals on
# the same bound the turn itself is already capped at.
AGENT_TURN_LOCK_KEY_PREFIX = os.environ.get("AGENT_TURN_LOCK_KEY_PREFIX", "agent_turn_lock")

# ADR 00732: set alongside the re-arm above when a new message lands while a
# previous turn for the same pair is already running - the in-flight turn
# checks this flag and ends without delivering its (now-stale) reply instead
# of letting it reach the chat. TTL equals the turn timeout, same self-healing
# reasoning as the turn lock itself.
AGENT_TURN_SUPERSEDED_KEY_PREFIX = os.environ.get(
    "AGENT_TURN_SUPERSEDED_KEY_PREFIX", "agent_turn_superseded"
)

# ADR 0075: SET NX marker so the peer-visible typing loop's lifetime spans
# "there is unanswered agent activity for this chat" rather than "this one
# turn object is still alive" - a superseded turn's replacement can pick up
# an already-running indicator instead of leaving a gap while it waits for
# its own loop to start. TTL equals the turn timeout, same self-healing
# reasoning as the turn lock/supersede flag above.
AGENT_TYPING_ACTIVE_KEY_PREFIX = os.environ.get(
    "AGENT_TYPING_ACTIVE_KEY_PREFIX", "agent_typing_active"
)

# ADR 0077: flat token-usage penalty charged when a turn is cancelled while
# a Gemini call is genuinely in flight (_TurnSuperseded in invoke_worker.py) -
# no real TurnResult/usageMetadata comes back from a cancelled call, so this
# is an estimate standing in for tokens plausibly still spent on Google's
# side, not a measurement. Small relative to AGENT_TOKEN_BUDGET_5H/_7D below.
AGENT_SUPERSEDED_CALL_TOKEN_PENALTY = int(
    os.environ.get("AGENT_SUPERSEDED_CALL_TOKEN_PENALTY", "500")
)

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

# A turn that hits the per-minute call budget mid-turn backs off and retries
# in-place (fixed-window resets within a minute, so this is a transient
# stall, not a real failure) instead of ending the turn with no notice and no
# retry. Bounded by a small retry cap so a persistently-exhausted budget (a
# real abuse/bug case, not just a burst) still falls through to the existing
# owner-facing notice rather than looping forever.
AGENT_GEMINI_BUDGET_RETRY_SECONDS = float(os.environ.get("AGENT_GEMINI_BUDGET_RETRY_SECONDS", "5"))
AGENT_GEMINI_BUDGET_MAX_RETRIES = int(os.environ.get("AGENT_GEMINI_BUDGET_MAX_RETRIES", "6"))

# Function-calling round-trips per turn. ADR 0047 decision 1: raised 4 -> 8 -
# Agentic RAG (get_knowledge_index followed by one or more fetch_chunk calls)
# legitimately needs more than 4 round-trips in one turn; the old cap was
# sized specifically to fit under the old 5/min limit above, which no longer
# applies. Raised 12 -> 16 to give more headroom for longer agentic turns.
AGENT_TURN_MAX_TOOL_ROUNDTRIPS = int(os.environ.get("AGENT_TURN_MAX_TOOL_ROUNDTRIPS", "16"))

# ADR 0102: max `continue_message` calls per turn (a long answer split across
# up to this many follow-up messages), enforced in code in dispatch_tool_call.
AGENT_MAX_CONTINUATION_MESSAGES = int(os.environ.get("AGENT_MAX_CONTINUATION_MESSAGES", "3"))

# Whole-turn wall-clock timeout (asyncio.wait_for) - aborts a stuck turn
# cleanly instead of holding a worker slot indefinitely.
AGENT_TURN_TIMEOUT_SECONDS = float(os.environ.get("AGENT_TURN_TIMEOUT_SECONDS", "90"))

# --- Shared owner send_message budget (ADR 0058) ---
# The agent impersonates its owner (sender_id=agent.owner_user_id on every
# send - see modules/agents/tools/execution.py), so it must draw from the
# SAME per-user WS send_message sliding-window budget the owner's own client
# consumes (config/security_settings.py's WS_SEND_MESSAGE_RATE_MAX/_BURST_MAX
# - the agent worker checks those exact Redis keys directly, bypassing the WS
# gateway only as a transport, never as a limit). On rejection the agent
# retries with backoff instead of failing the tool call - these two constants
# size that backoff, independent of the frontend's useOutbox.js equivalents
# (RATE_LIMIT_BACKOFF_MS/cap) so either side can be tuned separately.
AGENT_SEND_RATE_LIMIT_BACKOFF_MS = int(os.environ.get("AGENT_SEND_RATE_LIMIT_BACKOFF_MS", "1500"))
AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS = int(os.environ.get("AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS", "20000"))

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

# --- on_ephemeral_task trigger (ADR 0061) ---
# Cap on concurrent ephemeral reply-collection tasks one agent can have
# in-flight - enforced in the spawn_ephemeral_task config tool, same
# ScheduleQuotaExceededError-style guard as AGENT_MAX_SCHEDULE_ENTRIES.
AGENT_MAX_EPHEMERAL_TASKS = int(os.environ.get("AGENT_MAX_EPHEMERAL_TASKS", "5"))
# Hard ceiling on a spawned task's timeout_minutes, regardless of what the
# Supervisor/Builder asks for - keeps a forgotten task from lingering forever.
AGENT_EPHEMERAL_TASK_MAX_MINUTES = int(
    os.environ.get("AGENT_EPHEMERAL_TASK_MAX_MINUTES", str(60 * 24 * 3))
)
# Default timeout when the tool call omits timeout_minutes.
AGENT_EPHEMERAL_TASK_DEFAULT_MINUTES = int(
    os.environ.get("AGENT_EPHEMERAL_TASK_DEFAULT_MINUTES", str(60 * 24))
)

# ADR 0099: goal-driven conversational tasks (start_goal_task) - hard cap on
# agent turns per task, and on consecutive turns that neither send a message
# nor call complete_task/fail_task (both force-close the task as failed).
AGENT_GOAL_TASK_MAX_TURNS = int(os.environ.get("AGENT_GOAL_TASK_MAX_TURNS", "20"))
AGENT_GOAL_TASK_MAX_IDLE_TURNS = int(os.environ.get("AGENT_GOAL_TASK_MAX_IDLE_TURNS", "2"))

# ADR 0054: pause_and_escalate freezes a chat for at most this many hours
# before it auto-resumes on its own (lazy expiry, checked on read in the
# trigger engine - no cron/sweep). A human resume via
# POST /agents/me/resume-chat/{id}, or the owner's own resume_paused_chat
# config tool (ADR 0055 - the only resume paths; there is no implicit resume
# on an owner reply), still lifts the pause earlier.
AGENT_ESCALATION_PAUSE_HOURS = int(os.environ.get("AGENT_ESCALATION_PAUSE_HOURS", "24"))

# ADR 0072: hard ceiling on a single bulk_fetch_messages call - a chat with
# more messages than this in the requested range cannot be bulk-summarized at
# all (the owner must narrow by date range or count), never silently
# truncated. Enforced server-side in the tool handler, not just the prompt.
AGENT_BULK_FETCH_MAX_MESSAGES = int(os.environ.get("AGENT_BULK_FETCH_MAX_MESSAGES", "1000"))

# Shared upper bound on the optional per-call `limit` argument the model can
# pass to read_history/search_messages/search_semantic - lets the model ask
# for fewer or more results than each tool's own default in one call, without
# ever exceeding this ceiling regardless of what it requests. Does not apply
# to bulk_fetch_messages, which has its own much larger, confirmation-gated
# ceiling above.
AGENT_TOOL_RESULT_MAX_LIMIT = int(os.environ.get("AGENT_TOOL_RESULT_MAX_LIMIT", "50"))

# ADR 0072: how long a stashed bulk_fetch_messages confirmation request
# (Agent.pending_confirmation) stays valid before it lazily expires unanswered.
AGENT_PENDING_CONFIRMATION_TTL_MINUTES = int(
    os.environ.get("AGENT_PENDING_CONFIRMATION_TTL_MINUTES", "60")
)

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

# ADR 0085: how much real chunk content seeds the owner-notice turn after a
# successful upload, so the agent can actually describe what it learned
# instead of only echoing filename/mime. Capped, not the whole document -
# this is a turn-seed, not a second copy of the knowledge base.
AGENT_KNOWLEDGE_NOTICE_PREVIEW_CHUNKS = int(
    os.environ.get("AGENT_KNOWLEDGE_NOTICE_PREVIEW_CHUNKS", "5")
)
AGENT_KNOWLEDGE_NOTICE_PREVIEW_MAX_CHARS = int(
    os.environ.get("AGENT_KNOWLEDGE_NOTICE_PREVIEW_MAX_CHARS", "4000")
)

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
# Total char budget of the formatted "sender: content" transcript seeded into
# a turn's first Gemini prompt (_build_initial_contents/_build_schedule_contents).
# Enforced in whole messages (oldest dropped first, never cut mid-message).
AGENT_HISTORY_TRANSCRIPT_MAX_CHARS = int(
    os.environ.get("AGENT_HISTORY_TRANSCRIPT_MAX_CHARS", "6000")
)
# Per-message cap (head+tail kept) so one long message can't crowd out the
# rest of the window; the newest customer message gets the higher cap.
AGENT_HISTORY_MESSAGE_MAX_CHARS = int(
    os.environ.get("AGENT_HISTORY_MESSAGE_MAX_CHARS", "1000")
)
AGENT_HISTORY_LATEST_MESSAGE_MAX_CHARS = int(
    os.environ.get("AGENT_HISTORY_LATEST_MESSAGE_MAX_CHARS", "3000")
)

# --- LLM Judge / Semantic Router gate (ADR 0053, backend split in ADR 0076) ---
# Classification backend: TypeSafe `jev` (modules/agents/typesafe_client.py) -
# a dedicated structured-classification model, not a generative one. Own API
# key, already provisioned in .env.
JEV_API_KEY = os.environ.get("JEV_API_KEY", "")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")
JEV_HTTP_TIMEOUT_SECONDS = float(os.environ.get("JEV_HTTP_TIMEOUT_SECONDS", "10"))

# Noul score cutoffs converting jev's [0, 1] confidence into the judge's
# boolean decisions. Separate constants (not a single shared threshold) so
# on_topic vs. the three malicious sub-checks can be tuned independently
# after looking at AgentJudgeLog data.
JEV_ON_TOPIC_THRESHOLD = float(os.environ.get("JEV_ON_TOPIC_THRESHOLD", "0.5"))
JEV_MALICIOUS_THRESHOLD = float(os.environ.get("JEV_MALICIOUS_THRESHOLD", "0.5"))

# Redirect-text generation backend (Gemini, reject path only - ADR 0076): a
# minimal, tool-free, history-free call that only fires for the (small)
# rejected fraction of judged messages, to author a same-language polite
# reply. Was AGENT_JUDGE_MODEL pre-ADR-0076, when Gemini also did the
# classification itself; kept as its own constant so a future vendor
# rename/deprecation doesn't touch the main turn model
# (gemini_client.py::GEMINI_CHAT_MODEL) or vice versa.
AGENT_JUDGE_REDIRECT_MODEL = os.environ.get("AGENT_JUDGE_REDIRECT_MODEL", "gemini-flash-lite-latest")

# ADR 0093 Phase 1: same minimal-call pattern as AGENT_JUDGE_REDIRECT_MODEL
# above, for the new BuilderState.CLARIFY state - a cheap, tool-free,
# history-free call that authors the owner-facing disambiguating question
# (modules/agents/clarify.py::generate_clarify_question), never the main
# turn model (gemini_client.py::GEMINI_CHAT_MODEL).
AGENT_CLARIFY_MODEL = os.environ.get("AGENT_CLARIFY_MODEL", "gemini-flash-lite-latest")

# Own rate bucket (agent_judge_calls:{agent_id}), never shared with
# agent_gemini_calls (ADR 0047 decision 1) - a judge call consuming from the
# main budget would let hostile/off-topic traffic starve real turns, defeating
# the denial-of-wallet protection this gate exists for. Sized generously since
# each call is cheap/fast - this bucket exists for cost ceiling, not scarcity.
# Consumed once per judged message by the jev classification call (ADR 0076) -
# the conditional Gemini redirect-text call has no separate bucket, since it's
# inherently bounded by how often messages are actually rejected.
AGENT_JUDGE_CALLS_PER_MINUTE = int(os.environ.get("AGENT_JUDGE_CALLS_PER_MINUTE", "60"))
AGENT_JUDGE_CALLS_WINDOW_SECONDS = int(os.environ.get("AGENT_JUDGE_CALLS_WINDOW_SECONDS", "60"))

# --- Owner-chat jev router (ADR 0093 Phase 2) ---
# Own rate bucket (agent_router_calls:{agent_id}), same "never share the
# Gemini-calls budget" principle as AGENT_JUDGE_CALLS_PER_MINUTE above - a
# runaway router shouldn't starve the main turn budget, and vice versa. Runs
# once per config-mode owner-chat turn (not per message like the judge, since
# config-mode already only ever sees the owner's own messages), so sized
# lower than the judge bucket.
AGENT_ROUTER_CALLS_PER_MINUTE = int(os.environ.get("AGENT_ROUTER_CALLS_PER_MINUTE", "30"))
AGENT_ROUTER_CALLS_WINDOW_SECONDS = int(os.environ.get("AGENT_ROUTER_CALLS_WINDOW_SECONDS", "60"))

# How many prior turns of the owner-agent chat the router sees, alongside the
# new message - deliberately NOT the full history (builder_state already
# encodes long-running context, e.g. "mid Builder interview"; a full
# transcript would burn tokens on every owner message without improving
# routing accuracy). See ADR 0093's "Router contract" section.
AGENT_ROUTER_CONTEXT_TURNS = int(os.environ.get("AGENT_ROUTER_CONTEXT_TURNS", "5"))

# If the top two destination probabilities are one_off_action and builder and
# their margin is under this value, route to BuilderState.CLARIFY instead of
# guessing (ADR 0093). Starts wide/conservative (favors clarify over a wrong
# guess) - tightened in Phase 5 once AgentRouterLog has real routing data to
# tune against.
AGENT_ROUTER_CLARIFY_MARGIN = float(os.environ.get("AGENT_ROUTER_CLARIFY_MARGIN", "0.25"))

# ADR 0093 Phase 6a (session stickiness/hysteresis): if the owner's current
# builder_state is NOT the top-scoring destination this turn, but its own
# probability is within this margin of the top score, stay in the current
# state instead of switching - stops a low-signal, short message (e.g. mid
# Builder interview) from yanking the owner out of an in-progress
# conversation just because it scored marginally higher for a different
# destination. Only applies when the top destination differs from the
# current state; irrelevant when they already match. Same conservative-
# starting-point reasoning as AGENT_ROUTER_CLARIFY_MARGIN - tightened in
# Phase 5 once AgentRouterLog's new `sticky` column has real data to tune
# against.
AGENT_ROUTER_STICKINESS_MARGIN = float(os.environ.get("AGENT_ROUTER_STICKINESS_MARGIN", "0.15"))

# Length cap on the agent.system_prompt prefix folded into the judge's domain
# description - an oversized owner-authored prompt must not turn the judge
# itself into a second injection surface.
AGENT_JUDGE_SYSTEM_PROMPT_PREVIEW_CHARS = int(
    os.environ.get("AGENT_JUDGE_SYSTEM_PROMPT_PREVIEW_CHARS", "500")
)

# "Pronoun problem" fix (ADR 0053 section 3): a chat counts as an active
# conversation - and short/generic follow-ups get approved by instruction -
# if the agent's last reply in it lands within this many seconds.
AGENT_JUDGE_FOLLOW_UP_WINDOW_SECONDS = int(
    os.environ.get("AGENT_JUDGE_FOLLOW_UP_WINDOW_SECONDS", str(5 * 60))
)
# ...or is within the last N agent messages, whichever check is cheaper to
# run first short-circuits (recent-timestamp check before this row count).
AGENT_JUDGE_FOLLOW_UP_RECENT_MESSAGES = int(
    os.environ.get("AGENT_JUDGE_FOLLOW_UP_RECENT_MESSAGES", "2")
)

# --- Attachment-relevance judge (ADR 0086) ---
# A separate, dedicated jev classification call gating send_attached_file -
# NOT the message judge above: different question, different call site (mid
# tool-call, not pre-turn), own rate bucket so a burst of attachment resends
# can never starve or be starved by the message judge's own budget.
ATTACHMENT_JUDGE_CALLS_PER_MINUTE = int(os.environ.get("ATTACHMENT_JUDGE_CALLS_PER_MINUTE", "60"))
ATTACHMENT_JUDGE_CALLS_WINDOW_SECONDS = int(os.environ.get("ATTACHMENT_JUDGE_CALLS_WINDOW_SECONDS", "60"))

# Noul cutoff for "this file plausibly matches what was asked for" - separate
# from JEV_ON_TOPIC_THRESHOLD (a different proposition entirely), tunable
# independently once AgentJudgeLog data comes in.
ATTACHMENT_JUDGE_MATCH_THRESHOLD = float(os.environ.get("ATTACHMENT_JUDGE_MATCH_THRESHOLD", "0.5"))

# Noul cutoff for the message judge's needs_human_review question on a
# media-only triggering message (ADR 0088) - independent of
# JEV_ON_TOPIC_THRESHOLD/JEV_MALICIOUS_THRESHOLD, a different proposition.
AGENT_MEDIA_ESCALATION_THRESHOLD = float(os.environ.get("AGENT_MEDIA_ESCALATION_THRESHOLD", "0.5"))

# --- Tool-outcome-mismatch judge (ADR 0096) ---
# A separate, dedicated jev classification call gating the owner-notify path
# when a turn ends right after an unresolved tool failure - NOT the message
# judge or the attachment judge above: different question ("does this
# failure plausibly mean the goal wasn't met"), different call site (only at
# the two points a turn can end on an unresolved tool error), own rate
# bucket so it can never starve or be starved by either of those budgets.
AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE = int(os.environ.get("AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE", "60"))
AGENT_OUTCOME_JUDGE_CALLS_WINDOW_SECONDS = int(os.environ.get("AGENT_OUTCOME_JUDGE_CALLS_WINDOW_SECONDS", "60"))

# Noul cutoff for "this tool failure plausibly means the goal wasn't met" -
# independent of every other judge threshold above, a different proposition,
# tunable once AgentOutcomeJudgeLog data comes in.
AGENT_OUTCOME_JUDGE_MISMATCH_THRESHOLD = float(
    os.environ.get("AGENT_OUTCOME_JUDGE_MISMATCH_THRESHOLD", "0.5")
)

# --- Capacity estimation (ADR 0057) ---
# Rough single-turn wall-clock cost used ONLY to project "roughly how many
# conversations/hour" for the Builder's get_capacity_status tool - never
# used in any real enforcement (a real turn's cost varies with tool calls,
# e.g. Agentic RAG round-trips). Sized from the existing 90s turn timeout
# and typical single-tool-call turns being much faster than the worst case.
AGENT_ESTIMATED_SECONDS_PER_TURN = int(os.environ.get("AGENT_ESTIMATED_SECONDS_PER_TURN", "8"))

# --- Token usage windows (ADR 0059) ---
# Combined input+output token budgets, tracked as two independent Redis
# fixed-window counters (own module, modules/agents/token_budget.py - same
# INCRBY-on-a-weighted-amount shape as AGENT_DAILY_ACTIVE_SECONDS_BUDGET's
# time_budget.py, since infra.ratelimit.service.check_and_increment always
# adds exactly 1 and can't be reused for a weighted counter). Independent of
# AGENT_GEMINI_CALLS_PER_MINUTE/AGENT_DAILY_ACTIVE_SECONDS_BUDGET - those
# gate call count/wall-clock time, this gates token volume.
AGENT_TOKEN_BUDGET_5H = int(os.environ.get("AGENT_TOKEN_BUDGET_5H", "1000000"))
AGENT_TOKEN_BUDGET_5H_WINDOW_SECONDS = int(
    os.environ.get("AGENT_TOKEN_BUDGET_5H_WINDOW_SECONDS", str(5 * 60 * 60))
)
AGENT_TOKEN_BUDGET_7D = int(os.environ.get("AGENT_TOKEN_BUDGET_7D", "5000000"))
AGENT_TOKEN_BUDGET_7D_WINDOW_SECONDS = int(
    os.environ.get("AGENT_TOKEN_BUDGET_7D_WINDOW_SECONDS", str(7 * 24 * 60 * 60))
)

# Pre-flight gate: if the remaining budget in either window is below this
# many tokens, skip the Gemini call entirely rather than spend an API call
# that's very likely to fail/truncate to nothing - a turn's fixed overhead
# (system prompt + tool schemas + at least some transcript) realistically
# starts around 1,500-2,500 input tokens alone.
AGENT_TOKEN_MIN_VIABLE_BUDGET = int(os.environ.get("AGENT_TOKEN_MIN_VIABLE_BUDGET", "2000"))

# Technical ceiling on generationConfig.maxOutputTokens, independent of the
# user's remaining budget - never ask Gemini for an absurdly large completion
# just because a window happens to be nearly full.
AGENT_MAX_OUTPUT_TOKENS_CEILING = int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS_CEILING", "8192"))

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
    "AGENT_INVOKE_DEBOUNCE_ZSET_KEY",
    "AGENT_INVOKE_DEBOUNCE_SECONDS",
    "AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS",
    "AGENT_TURN_LOCK_KEY_PREFIX",
    "AGENT_TURN_SUPERSEDED_KEY_PREFIX",
    "AGENT_TYPING_ACTIVE_KEY_PREFIX",
    "AGENT_SUPERSEDED_CALL_TOKEN_PENALTY",
    "AGENT_DAILY_ACTIVE_SECONDS_BUDGET",
    "AGENT_GEMINI_CALLS_PER_MINUTE",
    "AGENT_GEMINI_CALLS_WINDOW_SECONDS",
    "AGENT_GEMINI_BUDGET_RETRY_SECONDS",
    "AGENT_GEMINI_BUDGET_MAX_RETRIES",
    "AGENT_TURN_MAX_TOOL_ROUNDTRIPS",
    "AGENT_MAX_CONTINUATION_MESSAGES",
    "AGENT_TURN_TIMEOUT_SECONDS",
    "AGENT_SEND_RATE_LIMIT_BACKOFF_MS",
    "AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS",
    "AGENT_ENABLED_OWNERS_SET_KEY",
    "AGENT_TRIGGER_CFG_KEY_PREFIX",
    "AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY",
    "AGENT_UNKNOWN_SENDER_QUOTA_WINDOW_SECONDS",
    "AGENT_MAX_AUTO_CHATS",
    "AGENT_ESCALATION_PAUSE_HOURS",
    "AGENT_BULK_FETCH_MAX_MESSAGES",
    "AGENT_TOOL_RESULT_MAX_LIMIT",
    "AGENT_PENDING_CONFIRMATION_TTL_MINUTES",
    "AGENT_MAX_SCHEDULE_ENTRIES",
    "AGENT_HISTORY_TRANSCRIPT_MAX_CHARS",
    "AGENT_HISTORY_MESSAGE_MAX_CHARS",
    "AGENT_HISTORY_LATEST_MESSAGE_MAX_CHARS",
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
    "AGENT_KNOWLEDGE_NOTICE_PREVIEW_CHUNKS",
    "AGENT_KNOWLEDGE_NOTICE_PREVIEW_MAX_CHARS",
    "AGENT_ESTIMATED_SECONDS_PER_TURN",
    "JEV_API_KEY",
    "JEV_MODEL",
    "JEV_HTTP_TIMEOUT_SECONDS",
    "JEV_ON_TOPIC_THRESHOLD",
    "JEV_MALICIOUS_THRESHOLD",
    "AGENT_JUDGE_REDIRECT_MODEL",
    "AGENT_CLARIFY_MODEL",
    "AGENT_JUDGE_CALLS_PER_MINUTE",
    "AGENT_JUDGE_CALLS_WINDOW_SECONDS",
    "AGENT_ROUTER_CALLS_PER_MINUTE",
    "AGENT_ROUTER_CALLS_WINDOW_SECONDS",
    "AGENT_ROUTER_CONTEXT_TURNS",
    "AGENT_ROUTER_CLARIFY_MARGIN",
    "AGENT_ROUTER_STICKINESS_MARGIN",
    "AGENT_JUDGE_SYSTEM_PROMPT_PREVIEW_CHARS",
    "AGENT_JUDGE_FOLLOW_UP_WINDOW_SECONDS",
    "AGENT_JUDGE_FOLLOW_UP_RECENT_MESSAGES",
    "ATTACHMENT_JUDGE_CALLS_PER_MINUTE",
    "ATTACHMENT_JUDGE_CALLS_WINDOW_SECONDS",
    "ATTACHMENT_JUDGE_MATCH_THRESHOLD",
    "AGENT_MEDIA_ESCALATION_THRESHOLD",
    "AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE",
    "AGENT_OUTCOME_JUDGE_CALLS_WINDOW_SECONDS",
    "AGENT_OUTCOME_JUDGE_MISMATCH_THRESHOLD",
    "AGENT_TOKEN_BUDGET_5H",
    "AGENT_TOKEN_BUDGET_5H_WINDOW_SECONDS",
    "AGENT_TOKEN_BUDGET_7D",
    "AGENT_TOKEN_BUDGET_7D_WINDOW_SECONDS",
    "AGENT_TOKEN_MIN_VIABLE_BUDGET",
    "AGENT_MAX_OUTPUT_TOKENS_CEILING",
    "AGENT_EPHEMERAL_TASK_DEFAULT_MINUTES",
    "AGENT_EPHEMERAL_TASK_MAX_MINUTES",
    "AGENT_MAX_EPHEMERAL_TASKS",
    "AGENT_GOAL_TASK_MAX_TURNS",
    "AGENT_GOAL_TASK_MAX_IDLE_TURNS",
]
