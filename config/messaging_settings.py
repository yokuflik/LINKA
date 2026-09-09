import os

# --- Detailed receipt log (per-user delivered/read/played history) ---
# On top of the O(1) watermark rollup that drives the sent/delivered/read/
# played tick (see database/models/message.py MessageStatus), every genuine
# watermark advance is also appended to message_receipt_log with its
# timestamp. That log answers "when exactly did user U read message X" and,
# in a group, "who has read/played message X" - neither of which the
# watermark model can. It is never touched by the chat list or the
# per-bubble check mark.
#
# Integer kind values deliberately mirror MessageStatus (2/3/4).
RECEIPT_KIND_DELIVERED = 2
RECEIPT_KIND_READ = 3
RECEIPT_KIND_PLAYED = 4
RECEIPT_KINDS = {RECEIPT_KIND_DELIVERED, RECEIPT_KIND_READ, RECEIPT_KIND_PLAYED}

# A message's "seen by" / "played by" detail view returns a per-member name
# list only for chats at or below this participant count; above it, only
# aggregate counts ("read by 812 of 1200"). Keeps the detail query and its
# payload bounded for very large groups.
RECEIPT_NAMED_LIST_MAX_MEMBERS = int(os.environ.get("RECEIPT_NAMED_LIST_MAX_MEMBERS", "256"))

# How long the append-only detailed log is retained. Older rows are dropped
# a whole partition at a time (scripts/prune_receipt_log.py); the coarse
# Participant.last_delivered_at/last_read_at/last_played_at columns are not
# on this clock and stay as a "last activity" fallback for old messages.
RECEIPT_LOG_RETENTION_DAYS = int(os.environ.get("RECEIPT_LOG_RETENTION_DAYS", "30"))

# The write path never INSERTs into message_receipt_log inline: it XADDs a
# tiny event onto this Redis Stream, and a background worker
# (services/receipts/worker.py) batch-drains it, collapsing many events for
# the same (chat, user, kind) into one row - so a 1000-member group opening
# a chat is a single multi-row INSERT, not 1000 transactions on a
# billion-row-scale table.
RECEIPT_STREAM_KEY = os.environ.get("RECEIPT_STREAM_KEY", "receipt_log_stream")
RECEIPT_STREAM_GROUP = os.environ.get("RECEIPT_STREAM_GROUP", "receipt_writers")
# Approximate MAXLEN cap (backpressure safety valve - a wedged worker can't
# grow the stream without bound).
RECEIPT_STREAM_MAXLEN = int(os.environ.get("RECEIPT_STREAM_MAXLEN", "1000000"))
RECEIPT_WORKER_BATCH = int(os.environ.get("RECEIPT_WORKER_BATCH", "500"))
RECEIPT_WORKER_BLOCK_MS = int(os.environ.get("RECEIPT_WORKER_BLOCK_MS", "2000"))
# Pending entries idle longer than this (a worker crashed mid-batch) are
# reclaimed by another worker via XAUTOCLAIM.
RECEIPT_STREAM_CLAIM_IDLE_MS = int(os.environ.get("RECEIPT_STREAM_CLAIM_IDLE_MS", "60000"))


# --- Outgoing message send queue (services/fanout) ---
# The WebSocket send path no longer writes the message or fans it out inline:
# it XADDs a tiny payload onto this Redis Stream and ACKs {"status":"queued"}
# immediately. A background worker (services/fanout/worker.py) drains it,
# persists each message and runs the fan-out. Two things this buys: the
# request path stops blocking on an N-participant fan-out, and a large group
# send no longer holds a pooled DB connection for the sender's whole ack.
# Unlike the receipt stream, an XADD failure here is NOT swallowed - a lost
# entry means a message the sender thinks was sent; the caller returns a sync
# error instead.
MESSAGE_SEND_STREAM_KEY = os.environ.get("MESSAGE_SEND_STREAM_KEY", "message_send_stream")
MESSAGE_SEND_STREAM_GROUP = os.environ.get("MESSAGE_SEND_STREAM_GROUP", "message_send_writers")
MESSAGE_SEND_STREAM_MAXLEN = int(os.environ.get("MESSAGE_SEND_STREAM_MAXLEN", "1000000"))
SEND_WORKER_BATCH = int(os.environ.get("SEND_WORKER_BATCH", "200"))
SEND_WORKER_BLOCK_MS = int(os.environ.get("SEND_WORKER_BLOCK_MS", "2000"))
SEND_STREAM_CLAIM_IDLE_MS = int(os.environ.get("SEND_STREAM_CLAIM_IDLE_MS", "60000"))
# Shard the send stream by chat_id (FANOUT_REWRITE_PLAN.md step 4) so a single
# write worker stops being the throughput ceiling. shard = chat_id % N keeps
# every message for one chat on one shard / one consumer, preserving order.
# The unsharded key stays the shard-0 key so an in-flight upgrade doesn't
# strand entries. 1 = effectively unsharded.
SEND_STREAM_SHARDS = int(os.environ.get("SEND_STREAM_SHARDS", "4"))


# --- Message fan-out queue (services/fanout, step 2) ---
# The send worker no longer fans a persisted message out inline: it XADDs a
# tiny reference (message id + chat + sender + client_message_id) onto this
# second stream and a separate fan-out worker (services/fanout/fanout_worker.py)
# builds the new_message event, publishes it, and pushes to offline members.
# Two streams on purpose: the DB write and the Redis/network fan-out fail and
# scale differently. Re-running fan-out for a message just re-publishes -
# clients dedupe by message_id - so a redelivered entry is harmless.
MESSAGE_FANOUT_STREAM_KEY = os.environ.get("MESSAGE_FANOUT_STREAM_KEY", "message_fanout_stream")
MESSAGE_FANOUT_STREAM_GROUP = os.environ.get("MESSAGE_FANOUT_STREAM_GROUP", "message_fanout_workers")
MESSAGE_FANOUT_STREAM_MAXLEN = int(os.environ.get("MESSAGE_FANOUT_STREAM_MAXLEN", "1000000"))
FANOUT_WORKER_BATCH = int(os.environ.get("FANOUT_WORKER_BATCH", "200"))
FANOUT_WORKER_BLOCK_MS = int(os.environ.get("FANOUT_WORKER_BLOCK_MS", "2000"))
FANOUT_STREAM_CLAIM_IDLE_MS = int(os.environ.get("FANOUT_STREAM_CLAIM_IDLE_MS", "60000"))
# Sharded like the send stream (FANOUT_REWRITE_PLAN.md step 4), same chat_id % N
# rule. Fan-out ordering per chat matters less than the write stream's but is
# cheap to keep. 1 = effectively unsharded.
FANOUT_STREAM_SHARDS = int(os.environ.get("FANOUT_STREAM_SHARDS", "4"))


# --- Routing layer (FANOUT_REWRITE_PLAN.md step 3) ---
# Instead of every process subscribing to every chat one of its clients is in,
# each process registers itself in Redis as serving a chat (chat_instances:{id}
# set), and the fan-out worker publishes a chat event only to the inbox
# channels (instance_inbox:{server_id}) of the processes that actually have a
# local member. chat_instances entries carry a TTL refreshed by a heartbeat so
# a crashed process's registrations expire instead of lingering forever.
CHAT_INSTANCE_TTL_SECONDS = int(os.environ.get("CHAT_INSTANCE_TTL_SECONDS", "90"))
ROUTING_HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("ROUTING_HEARTBEAT_INTERVAL_SECONDS", "30"))


# --- Scheduled messages (ADR 0031) ---
# A scheduled message lives in the `scheduled_messages` table and is turned
# into a real message at `scheduled_for` by an in-process poll worker
# (realtime/fanout/scheduled_worker.py) that re-uses the normal async send
# path. Postgres is the source of truth; the Redis ZSET `scheduled_messages:due`
# is a fast index rebuilt by the reconcile scan.
SCHEDULED_DUE_SET_KEY = os.environ.get("SCHEDULED_DUE_SET_KEY", "scheduled_messages:due")
SCHEDULED_POLL_INTERVAL_SECONDS = int(os.environ.get("SCHEDULED_POLL_INTERVAL_SECONDS", "5"))
SCHEDULED_WORKER_BATCH = int(os.environ.get("SCHEDULED_WORKER_BATCH", "100"))
SCHEDULED_RECONCILE_INTERVAL_SECONDS = int(os.environ.get("SCHEDULED_RECONCILE_INTERVAL_SECONDS", "60"))
# Transient (DB/Redis) fire failures re-queue with a short backoff; after this
# many attempts the row is marked failed.
SCHEDULED_MAX_FIRE_ATTEMPTS = int(os.environ.get("SCHEDULED_MAX_FIRE_ATTEMPTS", "5"))
SCHEDULED_FIRE_BACKOFF_SECONDS = int(os.environ.get("SCHEDULED_FIRE_BACKOFF_SECONDS", "30"))
# Validation limits for the schedule endpoint.
SCHEDULED_MAX_PENDING_PER_USER = int(os.environ.get("SCHEDULED_MAX_PENDING_PER_USER", "100"))
SCHEDULED_MIN_LEAD_SECONDS = int(os.environ.get("SCHEDULED_MIN_LEAD_SECONDS", "10"))
SCHEDULED_MAX_LEAD_DAYS = int(os.environ.get("SCHEDULED_MAX_LEAD_DAYS", "365"))
# `scheduled_write` sliding rate bucket (per user).
SCHEDULED_WRITE_RATE_MAX = int(os.environ.get("SCHEDULED_WRITE_RATE_MAX", "20"))
SCHEDULED_WRITE_RATE_WINDOW_SECONDS = int(os.environ.get("SCHEDULED_WRITE_RATE_WINDOW_SECONDS", "60"))

__all__ = [
    "RECEIPT_KIND_DELIVERED",
    "RECEIPT_KIND_READ",
    "RECEIPT_KIND_PLAYED",
    "RECEIPT_KINDS",
    "RECEIPT_NAMED_LIST_MAX_MEMBERS",
    "RECEIPT_LOG_RETENTION_DAYS",
    "RECEIPT_STREAM_KEY",
    "RECEIPT_STREAM_GROUP",
    "RECEIPT_STREAM_MAXLEN",
    "RECEIPT_WORKER_BATCH",
    "RECEIPT_WORKER_BLOCK_MS",
    "RECEIPT_STREAM_CLAIM_IDLE_MS",
    "MESSAGE_SEND_STREAM_KEY",
    "MESSAGE_SEND_STREAM_GROUP",
    "MESSAGE_SEND_STREAM_MAXLEN",
    "SEND_WORKER_BATCH",
    "SEND_WORKER_BLOCK_MS",
    "SEND_STREAM_CLAIM_IDLE_MS",
    "SEND_STREAM_SHARDS",
    "MESSAGE_FANOUT_STREAM_KEY",
    "MESSAGE_FANOUT_STREAM_GROUP",
    "MESSAGE_FANOUT_STREAM_MAXLEN",
    "FANOUT_WORKER_BATCH",
    "FANOUT_WORKER_BLOCK_MS",
    "FANOUT_STREAM_CLAIM_IDLE_MS",
    "FANOUT_STREAM_SHARDS",
    "CHAT_INSTANCE_TTL_SECONDS",
    "ROUTING_HEARTBEAT_INTERVAL_SECONDS",
    "SCHEDULED_DUE_SET_KEY",
    "SCHEDULED_POLL_INTERVAL_SECONDS",
    "SCHEDULED_WORKER_BATCH",
    "SCHEDULED_RECONCILE_INTERVAL_SECONDS",
    "SCHEDULED_MAX_FIRE_ATTEMPTS",
    "SCHEDULED_FIRE_BACKOFF_SECONDS",
    "SCHEDULED_MAX_PENDING_PER_USER",
    "SCHEDULED_MIN_LEAD_SECONDS",
    "SCHEDULED_MAX_LEAD_DAYS",
    "SCHEDULED_WRITE_RATE_MAX",
    "SCHEDULED_WRITE_RATE_WINDOW_SECONDS",
]
