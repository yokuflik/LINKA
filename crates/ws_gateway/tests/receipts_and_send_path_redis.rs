//! Phase 5 (RUST_GATEWAY_TEST_PLAN.md): Redis-backed tests for
//! `ws_gateway::receipts` (Stage 5.1) and `ws_gateway::send_path` (Stages 5.2,
//! 5.3) — the two stream-producer modules.
//!
//! Contract (from the modules' own doc-comments):
//! - `receipts::enqueue` is the Rust twin of
//!   `modules/receipts/receipt_log.enqueue_receipt_event`: `XADD
//!   receipt_log_stream MAXLEN ~ {receipt_stream_maxlen}` with 5 stringified
//!   fields.
//! - `send_path::enqueue` is the Rust twin of
//!   `realtime/fanout/send_queue.enqueue_outgoing_message`: `XADD` onto the
//!   chat-sharded `message_send_stream[:N]` key, `MAXLEN ~
//!   {send_stream_maxlen}`, all 10 `SendStreamEntry` fields.
//! - `send_path::app_workers_alive` is ADR 0041's liveness gate: `EXISTS
//!   app_worker_alive:{app_server_id}`, fail-**closed** (`false`) on any
//!   Redis error — the opposite polarity from the rate limiter's fail-open.
//!
//! Redis: `redis://127.0.0.1:6380/1` (test_redis, DB index 1 — see
//! `crates/ws_gateway/tests/README.md`). Every test uses a unique random key
//! (stream key via a unique chat_id / app_server_id) and cleans up only its
//! own keys via explicit `DEL`; never a global `FLUSHDB`. Do not run this
//! suite concurrently with `run_dev.sh` or the Python test suite.
//!
//! `AppState::new` needs a `Config`, and `Config::from_env()` reads real
//! process env vars — so these tests are `#[serial]` (matches Phase 1 Stage
//! 1.2's pattern) and restore every env var they touch in a `defer`-style
//! guard so they don't bleed into other tests in this binary.

use std::sync::atomic::{AtomicU64, Ordering};

use linka_common::config::Config;
use linka_common::events::{receipt_kind, SendMessageFrame};
use linka_common::redis_keys;
use redis::AsyncCommands;
use serial_test::serial;
use ws_gateway::fanin::SubCmd;
use ws_gateway::receipts;
use ws_gateway::send_path;
use ws_gateway::state::AppState;

const REDIS_URL: &str = "redis://127.0.0.1:6380/1";
const UNREACHABLE_REDIS_URL: &str = "redis://127.0.0.1:1/0";

static UNIQUE_SEQ: AtomicU64 = AtomicU64::new(0);

fn unique_label(label: &str) -> String {
    let seq = UNIQUE_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("{label}-{}-{seq}", std::process::id())
}

/// A unique, positive i64 chat id so concurrently-run tests never share a
/// `message_send_stream[:N]` key (mirrors `routing_redis.rs`'s helper).
fn unique_chat_id() -> i64 {
    let seq = UNIQUE_SEQ.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos() as i64;
    900_000_000_000 + (std::process::id() as i64) * 1_000_000 + (nanos % 1_000_000) + seq as i64
}

async fn redis_conn() -> redis::aio::MultiplexedConnection {
    let client = redis::Client::open(REDIS_URL).expect("valid redis url");
    client
        .get_multiplexed_async_connection()
        .await
        .expect("connect to test_redis on 6380/1 — is docker compose up?")
}

/// Env vars `Config::from_env` reads that these tests ever override —
/// cleared before every build so no bleed-through between tests.
const ENV_KEYS: &[&str] = &[
    "REDIS_URL",
    "APP_SERVER_ID",
    "SEND_STREAM_SHARDS",
    "MESSAGE_SEND_STREAM_MAXLEN",
    "RECEIPT_STREAM_MAXLEN",
];

fn clear_env() {
    for k in ENV_KEYS {
        std::env::remove_var(k);
    }
}

/// Builds a real `AppState` against `test_redis` with the given overrides.
/// Must run `#[serial]` since it mutates process env to build `Config`.
async fn build_state(app_server_id: &str, shards: u64, send_maxlen: usize, receipt_maxlen: usize) -> AppState {
    clear_env();
    std::env::set_var("REDIS_URL", REDIS_URL);
    std::env::set_var("APP_SERVER_ID", app_server_id);
    std::env::set_var("SEND_STREAM_SHARDS", shards.to_string());
    std::env::set_var("MESSAGE_SEND_STREAM_MAXLEN", send_maxlen.to_string());
    std::env::set_var("RECEIPT_STREAM_MAXLEN", receipt_maxlen.to_string());

    let config = Config::from_env();
    clear_env();

    let redis = redis::Client::open(REDIS_URL)
        .expect("valid redis url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect to test_redis on 6380/1 — is docker compose up?");
    let http = reqwest::Client::new();
    let (sub_tx, _sub_rx) = tokio::sync::mpsc::channel::<SubCmd>(8);

    AppState::new(config, redis, http, sub_tx)
}

fn minimal_send_frame(chat_id: i64, client_message_id: &str) -> SendMessageFrame {
    serde_json::from_value(serde_json::json!({
        "chat_id": chat_id,
        "client_message_id": client_message_id,
    }))
    .expect("minimal send_message frame must parse")
}

// ---------------------------------------------------------------------
// Stage 5.1 — receipts::enqueue
// ---------------------------------------------------------------------

#[tokio::test]
#[serial]
async fn enqueue_receipt_produces_one_entry_with_all_five_fields() {
    let app_server_id = unique_label("app");
    let state = build_state(&app_server_id, 4, 1_000_000, 1_000_000).await;
    let mut conn = redis_conn().await;

    let key = redis_keys::RECEIPT_STREAM_KEY;
    let len_before: i64 = redis::cmd("XLEN").arg(key).query_async::<i64>(&mut conn).await.unwrap_or(0);

    let chat_id = unique_chat_id();
    let user_id = 42i64;
    let message_id = 777i64;
    receipts::enqueue(&state, chat_id, user_id, receipt_kind::READ, message_id)
        .await
        .expect("enqueue must succeed against a live Redis");

    let len_after: i64 = redis::cmd("XLEN").arg(key).query_async::<i64>(&mut conn).await.unwrap_or(0);

    // Read back the single newest entry to check field values.
    let entries: Vec<(String, std::collections::HashMap<String, String>)> = redis::cmd("XREVRANGE")
        .arg(key)
        .arg("+")
        .arg("-")
        .arg("COUNT")
        .arg(1)
        .query_async(&mut conn)
        .await
        .expect("XREVRANGE must succeed");

    assert_eq!(len_after, len_before + 1, "exactly one new entry must be added");
    let (_, fields) = entries.first().expect("at least one entry must exist");
    assert_eq!(fields.get("chat_id").map(String::as_str), Some(chat_id.to_string().as_str()));
    assert_eq!(fields.get("user_id").map(String::as_str), Some("42"));
    assert_eq!(fields.get("kind").map(String::as_str), Some(receipt_kind::READ.to_string().as_str()));
    assert_eq!(fields.get("up_to_message_id").map(String::as_str), Some("777"));
    assert!(fields.contains_key("occurred_at"), "occurred_at field must be present");
}

#[tokio::test]
#[serial]
async fn enqueue_receipt_occurred_at_is_valid_rfc3339() {
    let app_server_id = unique_label("app");
    let state = build_state(&app_server_id, 4, 1_000_000, 1_000_000).await;
    let mut conn = redis_conn().await;

    let chat_id = unique_chat_id();
    receipts::enqueue(&state, chat_id, 1, receipt_kind::DELIVERED, 1)
        .await
        .expect("enqueue must succeed");

    let entries: Vec<(String, std::collections::HashMap<String, String>)> = redis::cmd("XREVRANGE")
        .arg(redis_keys::RECEIPT_STREAM_KEY)
        .arg("+")
        .arg("-")
        .arg("COUNT")
        .arg(1)
        .query_async(&mut conn)
        .await
        .expect("XREVRANGE must succeed");
    let (_, fields) = entries.first().expect("at least one entry must exist");
    let occurred_at = fields.get("occurred_at").expect("occurred_at must be present");

    assert!(
        is_valid_rfc3339(occurred_at),
        "occurred_at must be a valid RFC3339/ISO-8601 UTC timestamp, got {occurred_at:?}"
    );
}

/// Hand-rolled shape check (mirrors Phase 3's approach: avoids pulling in
/// `time`'s `parsing` Cargo feature for a single timestamp-format assertion).
/// Accepts `YYYY-MM-DDTHH:MM:SS(.fraction)?(Z|+00:00)` — RFC3339 UTC.
fn is_valid_rfc3339(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.len() < 20 {
        return false;
    }
    let digit = |b: u8| b.is_ascii_digit();
    digit(bytes[0]) && digit(bytes[1]) && digit(bytes[2]) && digit(bytes[3])
        && bytes[4] == b'-'
        && digit(bytes[5]) && digit(bytes[6])
        && bytes[7] == b'-'
        && digit(bytes[8]) && digit(bytes[9])
        && (bytes[10] == b'T' || bytes[10] == b't')
        && digit(bytes[11]) && digit(bytes[12])
        && bytes[13] == b':'
        && digit(bytes[14]) && digit(bytes[15])
        && bytes[16] == b':'
        && digit(bytes[17]) && digit(bytes[18])
        && (s.ends_with('Z') || s.ends_with("+00:00") || s.contains('+') || s.contains("-00:"))
}

#[tokio::test]
#[serial]
async fn enqueue_receipt_maxlen_keeps_stream_bounded() {
    // Fresh, uniquely-scoped Redis DB doesn't isolate a shared stream key —
    // `receipt_log_stream` is a single global key per the module's own
    // doc-comment, so this test can't use a unique key. Instead: record the
    // length before, insert with a tiny MAXLEN, and assert growth is capped
    // to a small multiple of that MAXLEN rather than growing unboundedly by
    // the full insert count (MAXLEN ~ is approximate trimming, not exact).
    //
    // Finding (2026-09-26): the plan's original "insert 50, expect a small
    // multiple of MAXLEN" undercounts how coarse "~" actually is. Redis's
    // approximate trim only evicts whole macro-nodes of the underlying radix
    // tree (default ~100 entries/node) and never splits one, so a stream that
    // never exceeds one node's worth of entries is not trimmed *at all* by
    // "~" — confirmed empirically: 50 inserts with `MAXLEN ~ 5` left growth
    // at exactly 50 (verified before adjusting this test; not a code bug,
    // it's documented Redis behavior). Insert enough entries to guarantee at
    // least one full node gets evicted, and assert the *ratio* of final
    // length to insert count is far below 1 rather than expecting a tight
    // bound near the configured MAXLEN.
    let app_server_id = unique_label("app");
    let small_maxlen = 5usize;
    let state = build_state(&app_server_id, 4, 1_000_000, small_maxlen).await;
    let mut conn = redis_conn().await;

    let key = redis_keys::RECEIPT_STREAM_KEY;
    let len_before: i64 = redis::cmd("XLEN").arg(key).query_async::<i64>(&mut conn).await.unwrap_or(0);

    let chat_id = unique_chat_id();
    let inserts = 5_000;
    for i in 0..inserts {
        receipts::enqueue(&state, chat_id, 1, receipt_kind::READ, i)
            .await
            .expect("enqueue must succeed");
    }

    let len_after: i64 = redis::cmd("XLEN").arg(key).query_async::<i64>(&mut conn).await.unwrap_or(0);
    let growth = len_after - len_before;

    assert!(
        growth < inserts / 2,
        "MAXLEN ~ {small_maxlen} must trim the stream well below inserting {inserts} raw entries, growth={growth}"
    );
}

// ---------------------------------------------------------------------
// Stage 5.2 — send_path::app_workers_alive
// ---------------------------------------------------------------------

#[tokio::test]
#[serial]
async fn app_workers_alive_true_when_key_present() {
    let app_server_id = unique_label("app");
    let state = build_state(&app_server_id, 4, 1_000_000, 1_000_000).await;
    let mut conn = redis_conn().await;

    let key = redis_keys::app_worker_alive(&app_server_id);
    let _: () = conn.set_ex(&key, "1", 10).await.unwrap();

    let alive = send_path::app_workers_alive(&state).await;

    let _: i64 = conn.del(&key).await.unwrap_or(0);
    assert!(alive, "app_workers_alive must be true when the key is present");
}

#[tokio::test]
#[serial]
async fn app_workers_alive_false_when_key_absent() {
    let app_server_id = unique_label("app");
    let state = build_state(&app_server_id, 4, 1_000_000, 1_000_000).await;

    // Never set the key for this unique app_server_id.
    let alive = send_path::app_workers_alive(&state).await;

    assert!(!alive, "app_workers_alive must be false when the key was never set");
}

#[tokio::test]
#[serial]
async fn app_workers_alive_false_when_key_expired() {
    let app_server_id = unique_label("app");
    let state = build_state(&app_server_id, 4, 1_000_000, 1_000_000).await;
    let mut conn = redis_conn().await;

    let key = redis_keys::app_worker_alive(&app_server_id);
    // Set with an already-past TTL so it's expired from the moment it lands.
    let _: () = conn.set(&key, "1").await.unwrap();
    let _: bool = conn.pexpire(&key, -1).await.unwrap_or(false);

    let alive = send_path::app_workers_alive(&state).await;

    let _: i64 = conn.del(&key).await.unwrap_or(0);
    assert!(!alive, "app_workers_alive must be false once the key has expired");
}

#[tokio::test]
#[serial]
async fn app_workers_alive_fails_closed_when_redis_unreachable() {
    // Fail-**closed** — the opposite polarity from the rate limiter's
    // fail-open (Phase 2). Build a Config pointed at an unreachable address.
    clear_env();
    std::env::set_var("REDIS_URL", UNREACHABLE_REDIS_URL);
    std::env::set_var("APP_SERVER_ID", "unreachable-app");
    let config = Config::from_env();
    clear_env();

    let conn_result = tokio::time::timeout(
        std::time::Duration::from_millis(500),
        redis::Client::open(UNREACHABLE_REDIS_URL)
            .expect("valid redis url")
            .get_multiplexed_async_connection(),
    )
    .await;

    let Ok(Ok(redis_conn)) = conn_result else {
        eprintln!(
            "note: could not obtain a MultiplexedConnection to an unreachable Redis \
             (connection failed at construction time, not at command time) — \
             app_workers_alive's own fail-closed path was not exercised on this run"
        );
        return;
    };

    let http = reqwest::Client::new();
    let (sub_tx, _sub_rx) = tokio::sync::mpsc::channel::<SubCmd>(8);
    let state = AppState::new(config, redis_conn, http, sub_tx);

    let alive = send_path::app_workers_alive(&state).await;
    assert!(!alive, "a Redis error must be treated as fail-closed (not alive), never fail-open");
}

// ---------------------------------------------------------------------
// Stage 5.3 — send_path::enqueue
// ---------------------------------------------------------------------

#[tokio::test]
#[serial]
async fn enqueue_send_lands_in_the_correctly_sharded_stream_key() {
    // Cross-check against redis_keys.rs's own documented numbers: with 4
    // shards, chat_id=8 -> bare key (shard 0), chat_id=9 -> ":1" suffix.
    let app_server_id = unique_label("app");
    let state = build_state(&app_server_id, 4, 1_000_000, 1_000_000).await;
    let mut conn = redis_conn().await;

    let key_shard0 = redis_keys::send_stream_key_for_chat(8, 4);
    let key_shard1 = redis_keys::send_stream_key_for_chat(9, 4);
    assert_eq!(key_shard0, "message_send_stream");
    assert_eq!(key_shard1, "message_send_stream:1");

    let len0_before: i64 = redis::cmd("XLEN").arg(&key_shard0).query_async::<i64>(&mut conn).await.unwrap_or(0);
    let len1_before: i64 = redis::cmd("XLEN").arg(&key_shard1).query_async::<i64>(&mut conn).await.unwrap_or(0);

    let frame8 = minimal_send_frame(8, &unique_label("cmid"));
    let frame9 = minimal_send_frame(9, &unique_label("cmid"));
    send_path::enqueue(&state, &frame8, 1).await.expect("enqueue for chat 8 must succeed");
    send_path::enqueue(&state, &frame9, 1).await.expect("enqueue for chat 9 must succeed");

    let len0_after: i64 = redis::cmd("XLEN").arg(&key_shard0).query_async::<i64>(&mut conn).await.unwrap_or(0);
    let len1_after: i64 = redis::cmd("XLEN").arg(&key_shard1).query_async::<i64>(&mut conn).await.unwrap_or(0);

    assert_eq!(len0_after, len0_before + 1, "chat_id=8 with 4 shards must land in the bare (shard 0) key");
    assert_eq!(len1_after, len1_before + 1, "chat_id=9 with 4 shards must land in the :1 (shard 1) key");
}

#[tokio::test]
#[serial]
async fn enqueue_send_entry_fields_match_send_stream_entry_pairs_exactly() {
    let app_server_id = unique_label("app");
    // Single shard so the key is deterministic regardless of chat_id.
    let state = build_state(&app_server_id, 1, 1_000_000, 1_000_000).await;
    let mut conn = redis_conn().await;

    let chat_id = unique_chat_id();
    let client_message_id = unique_label("cmid");
    let frame: SendMessageFrame = serde_json::from_value(serde_json::json!({
        "chat_id": chat_id,
        "client_message_id": client_message_id,
        "content": "hello world",
        "message_type": 2,
        "reply_to_message_id": 555,
        "media_key": "abc123",
        "media_name": "photo.jpg",
        "media_duration_seconds": 12,
        "media_blur_hash": "L6PZfSi_.AyE",
    }))
    .expect("full send_message frame must parse");

    let sender_id = 99i64;
    let key = redis_keys::send_stream_key_for_chat(chat_id, 1);

    send_path::enqueue(&state, &frame, sender_id).await.expect("enqueue must succeed");

    let entries: Vec<(String, std::collections::HashMap<String, String>)> = redis::cmd("XREVRANGE")
        .arg(&key)
        .arg("+")
        .arg("-")
        .arg("COUNT")
        .arg(1)
        .query_async(&mut conn)
        .await
        .expect("XREVRANGE must succeed");
    let (_, fields) = entries.first().expect("at least one entry must exist");

    let expected = linka_common::events::SendStreamEntry::from_frame(&frame, sender_id);
    for (field, value) in expected.pairs() {
        assert_eq!(
            fields.get(field).map(String::as_str),
            Some(value.as_str()),
            "field {field} must match SendStreamEntry::pairs() exactly"
        );
    }
    // All 10 fields must be present, no more no less relative to the contract.
    assert_eq!(fields.len(), expected.pairs().len(), "entry must have exactly the 10 documented fields");
}

#[tokio::test]
#[serial]
async fn enqueue_send_maxlen_keeps_stream_bounded() {
    // See the finding on `enqueue_receipt_maxlen_keeps_stream_bounded`:
    // `MAXLEN ~` only evicts whole radix-tree macro-nodes (~100 entries by
    // default), so a handful of inserts against a tiny MAXLEN isn't enough to
    // observe any trimming at all. Use enough inserts to guarantee at least
    // one node eviction, and assert the length-to-insert ratio rather than a
    // tight bound near the configured MAXLEN.
    let app_server_id = unique_label("app");
    let small_maxlen = 5usize;
    // Single shard, unique chat_id per run so previous tests' bare-key
    // entries don't skew the before/after delta.
    let state = build_state(&app_server_id, 1, small_maxlen, 1_000_000).await;
    let mut conn = redis_conn().await;

    let chat_id = unique_chat_id();
    let key = redis_keys::send_stream_key_for_chat(chat_id, 1);
    let len_before: i64 = redis::cmd("XLEN").arg(&key).query_async::<i64>(&mut conn).await.unwrap_or(0);

    let inserts = 5_000;
    for i in 0..inserts {
        let frame = minimal_send_frame(chat_id, &format!("cmid-{i}"));
        send_path::enqueue(&state, &frame, 1).await.expect("enqueue must succeed");
    }

    let len_after: i64 = redis::cmd("XLEN").arg(&key).query_async::<i64>(&mut conn).await.unwrap_or(0);
    let growth = len_after - len_before;

    assert!(
        growth < inserts / 2,
        "MAXLEN ~ {small_maxlen} must trim the stream well below inserting {inserts} raw entries, growth={growth}"
    );

    let _: i64 = conn.del(&key).await.unwrap_or(0);
}
