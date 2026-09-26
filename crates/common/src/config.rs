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

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;

    /// All env vars `Config`/`Limits::from_env` reads — used to force a clean
    /// slate before each test so leftover vars from one test (or the host
    /// shell) can't leak into another.
    const ALL_ENV_KEYS: &[&str] = &[
        "REDIS_URL",
        "JWT_SECRET",
        "CORS_ALLOW_ORIGINS",
        "WS_GATEWAY_BIND",
        "APP_INTERNAL_URL",
        "APP_SERVER_ID",
        "SEND_STREAM_SHARDS",
        "MESSAGE_SEND_STREAM_MAXLEN",
        "RECEIPT_STREAM_MAXLEN",
        "CHAT_INSTANCE_TTL_SECONDS",
        "ROUTING_HEARTBEAT_INTERVAL_SECONDS",
        "PRESENCE_TTL_SECONDS",
        "WS_CONN_MAX_CONNECTIONS",
        "WS_CONN_MAX_AGE_SECONDS",
        "WS_FRAME_RATE_MAX",
        "WS_FRAME_RATE_WINDOW_SECONDS",
        "WS_FRAME_FLOOD_STRIKES",
        "WS_UPGRADE_IP_RATE_LIMIT_MAX",
        "WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS",
        "WS_UPGRADE_USER_RATE_LIMIT_MAX",
        "WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS",
        "WS_SEND_MESSAGE_RATE_MAX",
        "WS_SEND_MESSAGE_RATE_WINDOW_SECONDS",
        "WS_SEND_MESSAGE_BURST_MAX",
        "WS_SEND_MESSAGE_BURST_WINDOW_SECONDS",
        "WS_RECEIPTS_RATE_MAX",
        "WS_RECEIPTS_RATE_WINDOW_SECONDS",
        "WS_TYPING_RATE_MAX",
        "WS_TYPING_RATE_WINDOW_SECONDS",
        "WS_SUBSCRIBE_PRESENCE_RATE_MAX",
        "WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS",
        "WS_EDIT_RATE_MAX",
        "WS_EDIT_RATE_WINDOW_SECONDS",
    ];

    fn clear_env() {
        for k in ALL_ENV_KEYS {
            env::remove_var(k);
        }
    }

    #[test]
    #[serial]
    fn defaults_with_no_env_vars_set() {
        clear_env();
        let c = Config::from_env();
        assert_eq!(c.redis_url, "redis://127.0.0.1:6379/0");
        assert_eq!(c.jwt_secret, "");
        assert_eq!(c.cors_allow_origins, vec!["*".to_string()]);
        assert_eq!(c.bind_addr, "0.0.0.0:8081");
        assert_eq!(c.app_internal_url, "http://app:8000");
        assert_eq!(c.app_server_id, "app");
        assert_eq!(c.send_stream_shards, 4);
        assert_eq!(c.send_stream_maxlen, 1_000_000);
        assert_eq!(c.receipt_stream_maxlen, 1_000_000);
        assert_eq!(c.chat_instance_ttl_secs, 90);
        assert_eq!(c.routing_heartbeat_secs, 30);
        assert_eq!(c.presence_ttl_secs, 60);
        assert_eq!(c.ws_conn_max, 5);
        assert_eq!(c.ws_conn_max_age_secs, 26 * 3600);

        let l = &c.limits;
        assert_eq!(l.frame_max, 30);
        assert_eq!(l.frame_window_secs, 10.0);
        assert_eq!(l.frame_flood_strikes, 60);
        assert_eq!(l.upgrade_ip_max, 20);
        assert_eq!(l.upgrade_ip_window_secs, 10.0);
        assert_eq!(l.upgrade_user_max, 10);
        assert_eq!(l.upgrade_user_window_secs, 10.0);
        assert_eq!(l.send_max, 3);
        assert_eq!(l.send_window_secs, 1.0);
        assert_eq!(l.send_burst_max, 40);
        assert_eq!(l.send_burst_window_secs, 60.0);
        assert_eq!(l.receipts_max, 60);
        assert_eq!(l.receipts_window_secs, 10.0);
        assert_eq!(l.typing_max, 10);
        assert_eq!(l.typing_window_secs, 10.0);
        assert_eq!(l.sub_presence_max, 20);
        assert_eq!(l.sub_presence_window_secs, 10.0);
        assert_eq!(l.edit_max, 20);
        assert_eq!(l.edit_window_secs, 60.0);
    }

    #[test]
    #[serial]
    fn frame_rate_max_env_override() {
        clear_env();
        env::set_var("WS_FRAME_RATE_MAX", "99");
        assert_eq!(Config::from_env().limits.frame_max, 99);
        clear_env();
    }

    #[test]
    #[serial]
    fn malformed_env_var_falls_back_to_default() {
        clear_env();
        env::set_var("WS_FRAME_RATE_MAX", "notanumber");
        assert_eq!(Config::from_env().limits.frame_max, 30);
        clear_env();
    }

    #[test]
    #[serial]
    fn empty_cors_origins_produces_empty_list_not_single_empty_string() {
        clear_env();
        env::set_var("CORS_ALLOW_ORIGINS", "");
        let c = Config::from_env();
        assert!(c.cors_allow_origins.is_empty(), "{:?}", c.cors_allow_origins);
        clear_env();
    }

    #[test]
    #[serial]
    fn cors_origins_trimmed_and_matched() {
        clear_env();
        env::set_var("CORS_ALLOW_ORIGINS", "https://a.com, https://b.com");
        let c = Config::from_env();
        assert_eq!(c.cors_allow_origins, vec!["https://a.com", "https://b.com"]);
        assert!(c.origin_allowed("https://a.com"));
        assert!(c.origin_allowed("https://b.com"));
        assert!(!c.origin_allowed("https://evil.com"));
        clear_env();
    }

    #[test]
    #[serial]
    fn wildcard_cors_allows_anything() {
        clear_env();
        env::set_var("CORS_ALLOW_ORIGINS", "*");
        let c = Config::from_env();
        assert!(c.origin_allowed("https://anything.example"));
        assert!(c.origin_allowed(""));
        clear_env();
    }

    #[test]
    #[serial]
    fn origin_allowed_is_exact_match_not_suffix() {
        clear_env();
        env::set_var("CORS_ALLOW_ORIGINS", "https://a.com");
        let c = Config::from_env();
        assert!(c.origin_allowed("https://a.com"));
        assert!(!c.origin_allowed("https://a.com.evil.com"));
        assert!(!c.origin_allowed("http://a.com"));
        clear_env();
    }
}
