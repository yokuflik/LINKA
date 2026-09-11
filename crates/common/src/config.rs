//! Runtime config, read from the environment. Defaults track `config/`.

use std::env;

#[derive(Debug, Clone)]
pub struct Config {
    pub redis_url: String,
    pub jwt_secret: String,
    /// Allowed WS `Origin` values; `["*"]` allows any (dev only).
    pub cors_allow_origins: Vec<String>,
    pub bind_addr: String,
    /// Base URL of the Python app for internal calls (`/internal/ws-bootstrap`).
    pub app_internal_url: String,
    /// Python app's `SERVER_ID` (ADR 0041) — keys `app_worker_alive:{id}`,
    /// checked before acking `send_message` / `mark_*`. Single-process
    /// deploy (ADR 0007) so this is static config, not looked up.
    pub app_server_id: String,
    pub send_stream_shards: u64,
    pub send_stream_maxlen: usize,
    /// `receipt_log_stream` approximate MAXLEN (Python `RECEIPT_STREAM_MAXLEN`).
    pub receipt_stream_maxlen: usize,
    /// `chat_instances` TTL, seconds (Python `CHAT_INSTANCE_TTL_SECONDS`).
    pub chat_instance_ttl_secs: u64,
    pub routing_heartbeat_secs: u64,
    /// `presence:{uid}` TTL, seconds (Python `_PRESENCE_TTL_SECONDS`).
    pub presence_ttl_secs: u64,
    /// Per-user concurrent WS connection cap (`WS_CONN_MAX_CONNECTIONS`).
    pub ws_conn_max: u64,
    /// Crash-leak sweep age for `ws:conns` members (`WS_CONN_MAX_AGE_SECONDS`).
    pub ws_conn_max_age_secs: u64,
    pub limits: Limits,
}

/// Rate-limit knobs, defaults from `config/security_settings.py`.
#[derive(Debug, Clone)]
pub struct Limits {
    pub frame_max: u64,
    pub frame_window_secs: f64,
    pub frame_flood_strikes: u32,
    pub upgrade_ip_max: u64,
    pub upgrade_ip_window_secs: f64,
    pub upgrade_user_max: u64,
    pub upgrade_user_window_secs: f64,
    pub send_max: u64,
    pub send_window_secs: f64,
    pub send_burst_max: u64,
    pub send_burst_window_secs: f64,
    pub receipts_max: u64,
    pub receipts_window_secs: f64,
    pub typing_max: u64,
    pub typing_window_secs: f64,
    pub sub_presence_max: u64,
    pub sub_presence_window_secs: f64,
    pub edit_max: u64,
    pub edit_window_secs: f64,
}

impl Limits {
    fn from_env() -> Self {
        Limits {
            frame_max: parse("WS_FRAME_RATE_MAX", 30),
            frame_window_secs: parse("WS_FRAME_RATE_WINDOW_SECONDS", 10.0),
            frame_flood_strikes: parse("WS_FRAME_FLOOD_STRIKES", 60),
            upgrade_ip_max: parse("WS_UPGRADE_IP_RATE_LIMIT_MAX", 20),
            upgrade_ip_window_secs: parse("WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS", 10.0),
            upgrade_user_max: parse("WS_UPGRADE_USER_RATE_LIMIT_MAX", 10),
            upgrade_user_window_secs: parse("WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS", 10.0),
            send_max: parse("WS_SEND_MESSAGE_RATE_MAX", 3),
            send_window_secs: parse("WS_SEND_MESSAGE_RATE_WINDOW_SECONDS", 1.0),
            send_burst_max: parse("WS_SEND_MESSAGE_BURST_MAX", 40),
            send_burst_window_secs: parse("WS_SEND_MESSAGE_BURST_WINDOW_SECONDS", 60.0),
            receipts_max: parse("WS_RECEIPTS_RATE_MAX", 60),
            receipts_window_secs: parse("WS_RECEIPTS_RATE_WINDOW_SECONDS", 10.0),
            typing_max: parse("WS_TYPING_RATE_MAX", 10),
            typing_window_secs: parse("WS_TYPING_RATE_WINDOW_SECONDS", 10.0),
            sub_presence_max: parse("WS_SUBSCRIBE_PRESENCE_RATE_MAX", 20),
            sub_presence_window_secs: parse("WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS", 10.0),
            edit_max: parse("WS_EDIT_RATE_MAX", 20),
            edit_window_secs: parse("WS_EDIT_RATE_WINDOW_SECONDS", 60.0),
        }
    }
}

fn var(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

fn parse<T: std::str::FromStr>(key: &str, default: T) -> T {
    env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

impl Config {
    /// Panics only on a missing `JWT_SECRET` outside dev.
    pub fn from_env() -> Self {
        let jwt_secret = var("JWT_SECRET", "");
        Config {
            redis_url: var("REDIS_URL", "redis://127.0.0.1:6379/0"),
            jwt_secret,
            cors_allow_origins: var("CORS_ALLOW_ORIGINS", "*")
                .split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect(),
            bind_addr: var("WS_GATEWAY_BIND", "0.0.0.0:8081"),
            app_internal_url: var("APP_INTERNAL_URL", "http://app:8000"),
            app_server_id: var("APP_SERVER_ID", "app"),
            send_stream_shards: parse("SEND_STREAM_SHARDS", 4),
            send_stream_maxlen: parse("MESSAGE_SEND_STREAM_MAXLEN", 1_000_000),
            receipt_stream_maxlen: parse("RECEIPT_STREAM_MAXLEN", 1_000_000),
            chat_instance_ttl_secs: parse("CHAT_INSTANCE_TTL_SECONDS", 90),
            routing_heartbeat_secs: parse("ROUTING_HEARTBEAT_INTERVAL_SECONDS", 30),
            presence_ttl_secs: parse("PRESENCE_TTL_SECONDS", 60),
            ws_conn_max: parse("WS_CONN_MAX_CONNECTIONS", 5),
            ws_conn_max_age_secs: parse("WS_CONN_MAX_AGE_SECONDS", 26 * 3600),
            limits: Limits::from_env(),
        }
    }

    pub fn origin_allowed(&self, origin: &str) -> bool {
        self.cors_allow_origins
            .iter()
            .any(|o| o == "*" || o == origin)
    }
}
