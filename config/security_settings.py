import os

# --- WebSocket rate limiting ---
# Legacy fixed-window send limit (still read as a coarse fallback ceiling by
# nothing now that step 6 landed - kept only so an old env file doesn't break).
SEND_MESSAGE_RATE_LIMIT_MAX = int(os.environ.get("SEND_MESSAGE_RATE_LIMIT_MAX", "20"))
SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS", "10"))

# --- WebSocket per-frame + per-action limits (COMMS_SECURITY_PLAN step 6) ---
# All sliding-window, all keyed per-user (or per-connection for the frame rate)
# so one client's flood can't starve another and can't wedge the send stream.
#
# Global inbound frame rate, per connection, checked before dispatch. Over ->
# one rate_limited error + the frame is dropped (NOT a close - a laggy client
# that batches is not an attacker). Staying over for WS_FRAME_FLOOD_STRIKES
# consecutive over-limit frames -> close 4429.
WS_FRAME_RATE_MAX = int(os.environ.get("WS_FRAME_RATE_MAX", "30"))
WS_FRAME_RATE_WINDOW_SECONDS = int(os.environ.get("WS_FRAME_RATE_WINDOW_SECONDS", "10"))
WS_FRAME_FLOOD_STRIKES = int(os.environ.get("WS_FRAME_FLOOD_STRIKES", "60"))

# send_message: 3 / second / user (primary), plus a 40 / 60 s sustained-spam
# ceiling. Both must pass.
WS_SEND_MESSAGE_RATE_MAX = int(os.environ.get("WS_SEND_MESSAGE_RATE_MAX", "3"))
WS_SEND_MESSAGE_RATE_WINDOW_SECONDS = float(os.environ.get("WS_SEND_MESSAGE_RATE_WINDOW_SECONDS", "1"))
WS_SEND_MESSAGE_BURST_MAX = int(os.environ.get("WS_SEND_MESSAGE_BURST_MAX", "40"))
WS_SEND_MESSAGE_BURST_WINDOW_SECONDS = int(os.environ.get("WS_SEND_MESSAGE_BURST_WINDOW_SECONDS", "60"))

# Per-action buckets, per user. mark_delivered/read/played share one bucket;
# edit_message/delete_message/restore_message share one; typing/recording share
# one (the client already self-throttles to 1/3s - this just enforces it).
WS_RECEIPTS_RATE_MAX = int(os.environ.get("WS_RECEIPTS_RATE_MAX", "60"))
WS_RECEIPTS_RATE_WINDOW_SECONDS = int(os.environ.get("WS_RECEIPTS_RATE_WINDOW_SECONDS", "10"))
WS_SUBSCRIBE_PRESENCE_RATE_MAX = int(os.environ.get("WS_SUBSCRIBE_PRESENCE_RATE_MAX", "20"))
WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS = int(os.environ.get("WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS", "10"))
WS_TYPING_RATE_MAX = int(os.environ.get("WS_TYPING_RATE_MAX", "10"))
WS_TYPING_RATE_WINDOW_SECONDS = int(os.environ.get("WS_TYPING_RATE_WINDOW_SECONDS", "10"))
WS_EDIT_RATE_MAX = int(os.environ.get("WS_EDIT_RATE_MAX", "20"))
WS_EDIT_RATE_WINDOW_SECONDS = int(os.environ.get("WS_EDIT_RATE_WINDOW_SECONDS", "60"))

# --- WebSocket connection cap + handshake churn (COMMS_SECURITY_PLAN step 5) ---
# Hard cap on concurrent WS connections per user, cross-process. On the
# (cap+1)th connect the *oldest* connection is evicted (WhatsApp-style) - the
# new one is never rejected.
WS_CONN_MAX_CONNECTIONS = int(os.environ.get("WS_CONN_MAX_CONNECTIONS", "5"))
# A connection whose zset entry is older than this is swept on the next
# connect for the same user - covers a process that crashed without running
# its unregister. Must comfortably exceed any real session length; 26h leaves
# room for a day-long session plus clock skew.
WS_CONN_MAX_AGE_SECONDS = int(os.environ.get("WS_CONN_MAX_AGE_SECONDS", str(26 * 3600)))
# Handshake churn: cap on *successful* /ws upgrades, checked right after auth
# (before the per-connect DB query). Over the limit closes 4429 without
# accepting. Sliding window so a boundary burst can't slip through.
WS_UPGRADE_IP_RATE_LIMIT_MAX = int(os.environ.get("WS_UPGRADE_IP_RATE_LIMIT_MAX", "20"))
WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS", "10"))
WS_UPGRADE_USER_RATE_LIMIT_MAX = int(os.environ.get("WS_UPGRADE_USER_RATE_LIMIT_MAX", "10"))
WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS", "10"))
# Defensive ceiling on the per-connect "every chat this user is in" query.
WS_MAX_CHAT_IDS_ON_CONNECT = int(os.environ.get("WS_MAX_CHAT_IDS_ON_CONNECT", "2000"))

# --- Transport hardening & rate limiting (ADR 0012, COMMS_SECURITY_PLAN) ---
# Direct-peer IPs/CIDRs whose `X-Forwarded-For` first hop we trust as the real
# client IP. In the compose deploy the only thing in front of the app is the
# Caddy container on the default docker bridge. Anything not from here has its
# XFF ignored (a client could otherwise forge it).
TRUSTED_PROXY_IPS = [
    x.strip() for x in os.environ.get("TRUSTED_PROXY_IPS", "172.16.0.0/12,127.0.0.1/32").split(",") if x.strip()
]
# Hostnames the app will answer to (Starlette TrustedHostMiddleware).
# "*" disables the check. Default covers local dev only.
ALLOWED_HOSTS = [
    x.strip() for x in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver,test").split(",") if x.strip()
]
# Allowed browser Origins for CORS and the WebSocket handshake. Prod is
# same-origin (Caddy serves the PoC + API together) so this is normally the
# single site origin. "*" is dev-only and forces allow_credentials=False
# (the "*" + credentials combination is invalid and silently unsafe).
CORS_ALLOW_ORIGINS = [
    x.strip() for x in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",") if x.strip()
]
# Coarse per-IP ceiling across all REST (a backstop, not the primary control).
# Business answer 1 (2026-09-06): 1000 requests / 3 min / IP.
API_IP_BACKSTOP_MAX = int(os.environ.get("API_IP_BACKSTOP_MAX", "1000"))
API_IP_BACKSTOP_WINDOW_SECONDS = int(os.environ.get("API_IP_BACKSTOP_WINDOW_SECONDS", "180"))

# --- REST feature limits (COMMS_SECURITY_PLAN step 7) ---
# All per-user sliding windows (rlsw:) except the upload-ticket per-IP gate
# (fixed window). Raise RateLimited -> HTTP 429. The global per-IP REST
# backstop (API_IP_BACKSTOP_*) landed in step 3 and is the coarse ceiling
# above all of these.
#
# GET /chats/{id}/messages - history pagination. `limit` is clamped server-side.
MSG_HISTORY_RATE_MAX = int(os.environ.get("MSG_HISTORY_RATE_MAX", "30"))
MSG_HISTORY_RATE_WINDOW_SECONDS = int(os.environ.get("MSG_HISTORY_RATE_WINDOW_SECONDS", "60"))
MSG_HISTORY_MAX_LIMIT = int(os.environ.get("MSG_HISTORY_MAX_LIMIT", "100"))
# POST /chats/{id}/messages/upload-ticket - media upload ticket (no volume quota;
# size caps + MIME whitelist + checksum pinning already enforced, ADR 0010).
UPLOAD_TICKET_RATE_MAX = int(os.environ.get("UPLOAD_TICKET_RATE_MAX", "5"))
UPLOAD_TICKET_RATE_WINDOW_SECONDS = int(os.environ.get("UPLOAD_TICKET_RATE_WINDOW_SECONDS", "60"))
UPLOAD_TICKET_IP_RATE_LIMIT_MAX = int(os.environ.get("UPLOAD_TICKET_IP_RATE_LIMIT_MAX", "20"))
UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS", "60"))
# Per-message detail reads (GET .../{message_id}/receipts and similar).
DETAIL_READ_RATE_MAX = int(os.environ.get("DETAIL_READ_RATE_MAX", "60"))
DETAIL_READ_RATE_WINDOW_SECONDS = int(os.environ.get("DETAIL_READ_RATE_WINDOW_SECONDS", "60"))
# List reads (GET /chats, GET /users/*).
LIST_READ_RATE_MAX = int(os.environ.get("LIST_READ_RATE_MAX", "120"))
LIST_READ_RATE_WINDOW_SECONDS = int(os.environ.get("LIST_READ_RATE_WINDOW_SECONDS", "60"))

__all__ = [
    "SEND_MESSAGE_RATE_LIMIT_MAX",
    "SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS",
    "WS_FRAME_RATE_MAX",
    "WS_FRAME_RATE_WINDOW_SECONDS",
    "WS_FRAME_FLOOD_STRIKES",
    "WS_SEND_MESSAGE_RATE_MAX",
    "WS_SEND_MESSAGE_RATE_WINDOW_SECONDS",
    "WS_SEND_MESSAGE_BURST_MAX",
    "WS_SEND_MESSAGE_BURST_WINDOW_SECONDS",
    "WS_RECEIPTS_RATE_MAX",
    "WS_RECEIPTS_RATE_WINDOW_SECONDS",
    "WS_SUBSCRIBE_PRESENCE_RATE_MAX",
    "WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS",
    "WS_TYPING_RATE_MAX",
    "WS_TYPING_RATE_WINDOW_SECONDS",
    "WS_EDIT_RATE_MAX",
    "WS_EDIT_RATE_WINDOW_SECONDS",
    "WS_CONN_MAX_CONNECTIONS",
    "WS_CONN_MAX_AGE_SECONDS",
    "WS_UPGRADE_IP_RATE_LIMIT_MAX",
    "WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS",
    "WS_UPGRADE_USER_RATE_LIMIT_MAX",
    "WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS",
    "WS_MAX_CHAT_IDS_ON_CONNECT",
    "TRUSTED_PROXY_IPS",
    "ALLOWED_HOSTS",
    "CORS_ALLOW_ORIGINS",
    "API_IP_BACKSTOP_MAX",
    "API_IP_BACKSTOP_WINDOW_SECONDS",
    "MSG_HISTORY_RATE_MAX",
    "MSG_HISTORY_RATE_WINDOW_SECONDS",
    "MSG_HISTORY_MAX_LIMIT",
    "UPLOAD_TICKET_RATE_MAX",
    "UPLOAD_TICKET_RATE_WINDOW_SECONDS",
    "UPLOAD_TICKET_IP_RATE_LIMIT_MAX",
    "UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS",
    "DETAIL_READ_RATE_MAX",
    "DETAIL_READ_RATE_WINDOW_SECONDS",
    "LIST_READ_RATE_MAX",
    "LIST_READ_RATE_WINDOW_SECONDS",
]
