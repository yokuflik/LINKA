import os
import random
import uuid

# --- Snowflake ID generation ---
# Must be unique per running app instance/process in production (ideally
# derived from a pod ordinal or a Redis-issued lease). A *fixed* default
# (e.g. always "1") would be actively dangerous here: forgetting to set this
# when scaling out to multiple instances - an easy mistake, since a single
# instance works fine either way - would make every instance mint colliding
# ids in lockstep. A random per-process default (10 bits => 1024 possible
# values) doesn't guarantee uniqueness across instances either, but turns a
# guaranteed collision into a low-probability one instead of the worst case.
SNOWFLAKE_MACHINE_ID = int(os.environ.get("SNOWFLAKE_MACHINE_ID", str(random.randint(0, 1023))))

# --- Rust ID service (ADR 0011) ---
# gRPC address of the standalone Rust Snowflake service, e.g. "id_service:50051".
# Empty (the default) => keep minting ids in-process with SNOWFLAKE_MACHINE_ID.
# When set, utils.id_client.next_id() calls the service (unary, one id per RPC -
# never batch, the id timestamp is Postgres' created_at partition-routing key).
ID_SERVICE_ADDR = os.environ.get("ID_SERVICE_ADDR", "")
# Per-call deadline; on timeout/unavailable id_client falls back to the local
# generator so id minting never hard-stops.
ID_SERVICE_TIMEOUT_SECONDS = float(os.environ.get("ID_SERVICE_TIMEOUT_SECONDS", "0.5"))

# --- Server instance identity ---
# Used to tag presence entries with which instance a connection is on.
# Falls back to a random id per process start when not set (e.g. by the
# orchestrator/pod name in production).
SERVER_ID = os.environ.get("SERVER_ID", str(uuid.uuid4()))

# --- Message content size cap ---
# Applies to both new messages and edits. Without this, a single message is
# bounded only by Postgres's TEXT column (~1GB) and whatever the ASGI
# server's own WebSocket frame-size default happens to be - both are
# accidents of infrastructure, not a real limit, and a huge payload here
# gets replicated to every subscriber via Redis PUBLISH and to every
# recipient's WebSocket, in addition to bloating storage at billion-row scale.
MAX_MESSAGE_CONTENT_LENGTH = int(os.environ.get("MAX_MESSAGE_CONTENT_LENGTH", "4096"))

# --- Group creation cap ---
# create_group_chat() adds members one at a time (one DB round trip each);
# an unbounded initial_member_ids list is an easy way to turn one API call
# into millions of sequential inserts. Bulk-importing a huge membership list
# needs its own batched/background flow, not this one.
MAX_INITIAL_GROUP_MEMBERS = int(os.environ.get("MAX_INITIAL_GROUP_MEMBERS", "256"))

__all__ = [
    "SNOWFLAKE_MACHINE_ID",
    "ID_SERVICE_ADDR",
    "ID_SERVICE_TIMEOUT_SECONDS",
    "SERVER_ID",
    "MAX_MESSAGE_CONTENT_LENGTH",
    "MAX_INITIAL_GROUP_MEMBERS",
]
