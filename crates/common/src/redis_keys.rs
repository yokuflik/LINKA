//! Redis key / stream / channel names. Every string here has a Python twin —
//! keep them in lockstep (Python sources noted per item).

// --- Streams (producer side lives in the gateway) ---------------------------

/// `realtime/fanout/send_queue.py` — `MESSAGE_SEND_STREAM_KEY`.
pub const MESSAGE_SEND_STREAM_KEY: &str = "message_send_stream";
/// `config/messaging_settings.py` — `SEND_STREAM_SHARDS` (default).
pub const SEND_STREAM_SHARDS_DEFAULT: u64 = 4;
/// `config/messaging_settings.py` — `MESSAGE_SEND_STREAM_MAXLEN` (default).
pub const MESSAGE_SEND_STREAM_MAXLEN_DEFAULT: usize = 1_000_000;

/// `modules/receipts/receipt_log.py` — `settings.RECEIPT_STREAM_KEY`.
pub const RECEIPT_STREAM_KEY: &str = "receipt_log_stream";

// --- Routing layer (FANOUT_REWRITE_PLAN step 3) -----------------------------

/// SET of `server_id` serving a chat. `realtime/fanout/routing.py`.
pub fn chat_instances(chat_id: i64) -> String {
    format!("chat_instances:{chat_id}")
}
/// Reverse map: SET of chat ids a process serves.
pub fn instance_chats(server_id: &str) -> String {
    format!("instance_chats:{server_id}")
}
/// Pub/sub channel a process listens on for fan-in.
pub fn instance_inbox(server_id: &str) -> String {
    format!("instance_inbox:{server_id}")
}

// --- Per-user channels -----------------------------------------------------

pub fn user_events(user_id: i64) -> String {
    format!("user_events:{user_id}")
}
pub fn presence_events(user_id: i64) -> String {
    format!("presence_events:{user_id}")
}

// --- App liveness (ADR 0041) -----------------------------------------------

/// Refreshed by every BaseStreamConsumer loop iteration
/// (`realtime/fanout/base_worker.py::touch_app_liveness`) and by the receipt
/// worker. Missing/expired ⇒ nothing is draining `message_send_stream` /
/// `receipt_log_stream` right now.
pub fn app_worker_alive(app_server_id: &str) -> String {
    format!("app_worker_alive:{app_server_id}")
}

// --- Presence state ------------------------------------------------------

/// SET of foreground connection members (`{server_id}:{connection_id}`), TTL'd.
pub fn presence(user_id: i64) -> String {
    format!("presence:{user_id}")
}
/// ISO-8601 string, no TTL.
pub fn presence_last_seen(user_id: i64) -> String {
    format!("presence_last_seen:{user_id}")
}

// --- Connection cap (COMMS_SECURITY_PLAN step 5) --------------------------

/// ZSET, member `{server_id}:{connection_id}`, score = connect epoch-ms.
pub fn ws_conns(user_id: i64) -> String {
    format!("ws:conns:{user_id}")
}

// --- Sharding helpers (mirror send_queue.py) ------------------------------

/// All of a chat's traffic maps to one shard so per-chat order survives.
pub fn shard_for_chat(chat_id: i64, shards: u64) -> u64 {
    (chat_id.rem_euclid(shards as i64)) as u64
}

/// Shard 0 keeps the bare key (upgrade-safe), others get a `:{n}` suffix.
pub fn stream_key(base: &str, shard: u64) -> String {
    if shard == 0 {
        base.to_string()
    } else {
        format!("{base}:{shard}")
    }
}

/// The send-stream key a given chat's messages must be XADDed to.
pub fn send_stream_key_for_chat(chat_id: i64, shards: u64) -> String {
    stream_key(MESSAGE_SEND_STREAM_KEY, shard_for_chat(chat_id, shards))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shard_matches_python_modulo() {
        // Python: chat_id % shards, chat_id is a positive Snowflake.
        assert_eq!(shard_for_chat(10, 4), 2);
        assert_eq!(shard_for_chat(8, 4), 0);
        assert_eq!(send_stream_key_for_chat(8, 4), "message_send_stream");
        assert_eq!(send_stream_key_for_chat(9, 4), "message_send_stream:1");
    }

    #[test]
    fn chat_instances_key_matches_python() {
        // realtime/fanout/routing.py
        assert_eq!(chat_instances(42), "chat_instances:42");
    }

    #[test]
    fn instance_chats_key_matches_python() {
        assert_eq!(instance_chats("srv1"), "instance_chats:srv1");
    }

    #[test]
    fn instance_inbox_key_matches_python() {
        assert_eq!(instance_inbox("srv1"), "instance_inbox:srv1");
    }

    #[test]
    fn user_events_key_matches_python() {
        assert_eq!(user_events(42), "user_events:42");
    }

    #[test]
    fn presence_events_key_matches_python() {
        assert_eq!(presence_events(42), "presence_events:42");
    }

    #[test]
    fn app_worker_alive_key_matches_python() {
        assert_eq!(app_worker_alive("app"), "app_worker_alive:app");
    }

    #[test]
    fn presence_key_matches_python() {
        assert_eq!(presence(42), "presence:42");
    }

    #[test]
    fn presence_last_seen_key_matches_python() {
        assert_eq!(presence_last_seen(42), "presence_last_seen:42");
    }

    #[test]
    fn ws_conns_key_matches_legacy_python_naming() {
        // ws_connection_registry.py was deleted by ADR 0038 (connection-cap
        // enforcement is now Rust-only), but the key name is preserved from
        // that era per .claude_docs/security_and_rate_limiting.md — pin the
        // literal so a refactor doesn't silently rename it.
        assert_eq!(ws_conns(42), "ws:conns:42");
    }

    #[test]
    fn shard_for_chat_negative_id_never_negative_via_rem_euclid() {
        // Snowflakes are documented as always positive, but pin the
        // rem_euclid behavior explicitly so a future signed-id regression
        // (or a switch back to plain `%`, which CAN return negative in Rust)
        // is caught immediately.
        assert_eq!(shard_for_chat(-1, 4), 3);
        assert_eq!(shard_for_chat(-4, 4), 0);
        assert_eq!(shard_for_chat(-5, 4), 3);
    }

    #[test]
    fn single_shard_maps_every_chat_to_shard_zero_no_suffix() {
        assert_eq!(shard_for_chat(123456789, 1), 0);
        assert_eq!(shard_for_chat(-1, 1), 0);
        assert_eq!(send_stream_key_for_chat(999, 1), "message_send_stream");
    }

    #[test]
    fn stream_key_constants_match_python_literals() {
        assert_eq!(MESSAGE_SEND_STREAM_KEY, "message_send_stream");
        assert_eq!(RECEIPT_STREAM_KEY, "receipt_log_stream");
    }
}
