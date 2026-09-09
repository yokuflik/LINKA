//! The rate-limit + connection-cap Lua, copied **verbatim** from the Python
//! side so a mixed Python/Rust fleet shares one enforcement state:
//!   - sliding window: `infra/ratelimit/service.py` `_SLIDING_WINDOW_LUA`
//!   - connection cap: `realtime/ws_connection_registry.py` `_REGISTER_LUA`
//!
//! Key prefixes and member formats must match exactly.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use redis::aio::MultiplexedConnection;

/// `rlsw:` in Python.
const SLIDING_PREFIX: &str = "rlsw:";

// Verbatim from infra/ratelimit/service.py
const SLIDING_WINDOW_LUA: &str = r#"
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    return {1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry = window
if oldest[2] then
    retry = (tonumber(oldest[2]) + window) - now
    if retry < 1 then retry = 1 end
end
redis.call('PEXPIRE', key, window)
return {0, retry}
"#;

// Verbatim from realtime/ws_connection_registry.py
const CONN_REGISTER_LUA: &str = r#"
local key = KEYS[1]
local now = tonumber(ARGV[1])
local max_age = tonumber(ARGV[2])
local max_conns = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - max_age)
redis.call('ZADD', key, now, member)

local evicted = {}
local count = redis.call('ZCARD', key)
while count > max_conns do
    local popped = redis.call('ZPOPMIN', key)
    if popped[1] == nil then break end
    table.insert(evicted, popped[1])
    count = count - 1
end

if max_age > 0 then
    redis.call('PEXPIRE', key, max_age)
end
return evicted
"#;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

static MEMBER_SEQ: AtomicU64 = AtomicU64::new(0);

fn next_member(now: i64) -> String {
    let seq = MEMBER_SEQ.fetch_add(1, Ordering::Relaxed) % 1_000_000;
    format!("{now}-{seq}")
}

/// `{server_id}:{connection_id}` — `ws_connection_registry.member`.
pub fn conn_member(server_id: &str, connection_id: &str) -> String {
    format!("{server_id}:{connection_id}")
}

/// Inverse of [`conn_member`]. `connection_id` is a UUID (no colons).
pub fn split_conn_member(m: &str) -> (String, String) {
    match m.rsplit_once(':') {
        Some((sid, cid)) => (sid.to_string(), cid.to_string()),
        None => (String::new(), m.to_string()),
    }
}

#[derive(Clone)]
pub struct RateLimiter {
    conn: MultiplexedConnection,
}

impl RateLimiter {
    pub fn new(conn: MultiplexedConnection) -> Self {
        Self { conn }
    }

    /// `true` = allowed (hit recorded); `false` = over the limit.
    /// Mirrors `rate_limit_service.check_sliding_window`. A Redis error is
    /// fail-open (`true`) — matching the Python callers that treat the limiter
    /// as best-effort.
    pub async fn check_sliding_window(
        &self,
        identifier: &str,
        action: &str,
        max_per_window: u64,
        window_secs: f64,
    ) -> bool {
        let now = now_ms();
        let window_ms = (window_secs * 1000.0) as i64;
        let key = format!("{SLIDING_PREFIX}{action}:{identifier}");
        let script = redis::Script::new(SLIDING_WINDOW_LUA);
        let mut conn = self.conn.clone();
        let res: Result<(i64, i64), _> = script
            .key(key)
            .arg(now)
            .arg(window_ms)
            .arg(max_per_window)
            .arg(next_member(now))
            .invoke_async(&mut conn)
            .await;
        match res {
            Ok((allowed, _retry)) => allowed == 1,
            Err(e) => {
                tracing::warn!(%action, error = %e, "sliding-window limiter Redis error; failing open");
                true
            }
        }
    }

    /// Record a new connection under `ws:conns:{user_id}`; return evicted
    /// members (`{server_id}:{connection_id}`). Best-effort: a Redis error
    /// returns `[]` and the connection proceeds.
    pub async fn register_connection(
        &self,
        user_id: i64,
        server_id: &str,
        connection_id: &str,
        max_connections: u64,
        max_age_secs: u64,
    ) -> Vec<String> {
        let key = format!("ws:conns:{user_id}");
        let script = redis::Script::new(CONN_REGISTER_LUA);
        let mut conn = self.conn.clone();
        let res: Result<Vec<String>, _> = script
            .key(key)
            .arg(now_ms())
            .arg((max_age_secs as i64) * 1000)
            .arg(max_connections)
            .arg(conn_member(server_id, connection_id))
            .invoke_async(&mut conn)
            .await;
        res.unwrap_or_else(|e| {
            tracing::warn!(user_id, error = %e, "ws:conns register failed; no eviction");
            Vec::new()
        })
    }

    /// Drop a connection's slot. Best-effort.
    pub async fn unregister_connection(&self, user_id: i64, server_id: &str, connection_id: &str) {
        let mut conn = self.conn.clone();
        let _: Result<i64, _> = redis::cmd("ZREM")
            .arg(format!("ws:conns:{user_id}"))
            .arg(conn_member(server_id, connection_id))
            .query_async(&mut conn)
            .await;
    }
}
