//! Phase 3 (RUST_GATEWAY_TEST_PLAN.md): Redis-backed tests for
//! `ws_gateway::presence`, the Rust twin of `realtime/presence_service.py`.
//!
//! Contract (from the module's own doc-comment): `presence:{uid}` is a bare
//! connection-id SET (not `{server_id}:{conn}`), TTL'd; `presence_last_seen`
//! has no TTL; a `presence_update` publishes only on the 0<->1 device edge.
//!
//! Redis: `redis://127.0.0.1:6380/1` (test_redis, DB index 1 — see
//! `crates/ws_gateway/tests/README.md`). Every test uses a unique random user
//! id and cleans up only its own keys; never a global `FLUSHDB`. Do not run
//! this suite concurrently with `run_dev.sh` or the Python test suite.

use std::time::Duration;

use linka_common::redis_keys;
use redis::AsyncCommands;
use ws_gateway::presence;

const REDIS_URL: &str = "redis://127.0.0.1:6380/1";

async fn redis_conn() -> redis::aio::MultiplexedConnection {
    let client = redis::Client::open(REDIS_URL).expect("valid redis url");
    client
        .get_multiplexed_async_connection()
        .await
        .expect("connect to test_redis on 6380/1 — is docker compose up?")
}

static UNIQUE_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// A unique, positive i64 user id so concurrently-run tests never share a
/// `presence:{uid}` key.
fn unique_user_id() -> i64 {
    let seq = UNIQUE_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos() as i64;
    // Keep it well within i64 range and always positive.
    900_000_000_000 + (std::process::id() as i64) * 1_000_000 + (nanos % 1_000_000) + seq as i64
}

/// Minimal RFC3339 shape check (`YYYY-MM-DDTHH:MM:SS...Z`/offset) — avoids
/// pulling in `time`'s `parsing` feature just for one assertion.
fn is_valid_rfc3339(s: &str) -> bool {
    let bytes = s.as_bytes();
    bytes.len() >= 20
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && (bytes[10] == b'T' || bytes[10] == b't')
        && bytes[13] == b':'
        && bytes[16] == b':'
        && s.ends_with('Z')
}

fn unique_conn_id(label: &str) -> String {
    let seq = UNIQUE_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    format!("{label}-{}-{seq}", std::process::id())
}

async fn cleanup(conn: &mut redis::aio::MultiplexedConnection, user_id: i64) {
    let keys = vec![
        redis_keys::presence(user_id),
        redis_keys::presence_last_seen(user_id),
    ];
    let _: Result<i64, _> = conn.del(keys).await;
}

/// Subscribe to `presence_events:{uid}` and return the pubsub handle so the
/// caller can await messages published after this point. Must be created
/// (and the subscription established) *before* the action under test runs,
/// since PUBLISH has no history for late subscribers.
async fn subscribe_presence_events(user_id: i64) -> redis::aio::PubSub {
    let client = redis::Client::open(REDIS_URL).expect("valid redis url");
    let mut pubsub = client
        .get_async_pubsub()
        .await
        .expect("open pubsub connection");
    pubsub
        .subscribe(redis_keys::presence_events(user_id))
        .await
        .expect("subscribe to presence_events channel");
    pubsub
}

async fn recv_json(
    pubsub: &mut redis::aio::PubSub,
    timeout: Duration,
) -> Option<serde_json::Value> {
    use futures_util::StreamExt;
    let mut stream = pubsub.on_message();
    match tokio::time::timeout(timeout, stream.next()).await {
        Ok(Some(msg)) => {
            let payload: String = msg.get_payload().expect("payload is a string");
            Some(serde_json::from_str(&payload).expect("payload is valid JSON"))
        }
        _ => None,
    }
}

// ---------------------------------------------------------------------
// mark_online
// ---------------------------------------------------------------------

#[tokio::test]
async fn mark_online_fresh_user_sets_scard_1_last_seen_and_publishes_online() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_uuid = unique_conn_id("conn");
    let mut pubsub = subscribe_presence_events(user_id).await;

    presence::mark_online(&mut conn, user_id, &conn_uuid, 60).await;

    let scard: i64 = conn.scard(redis_keys::presence(user_id)).await.unwrap_or(0);
    let last_seen: Option<String> = conn.get(redis_keys::presence_last_seen(user_id)).await.unwrap_or(None);
    let event = recv_json(&mut pubsub, Duration::from_secs(2)).await;

    cleanup(&mut conn, user_id).await;

    assert_eq!(scard, 1);
    let last_seen = last_seen.expect("presence_last_seen must be set");
    assert!(
        is_valid_rfc3339(&last_seen),
        "last_seen must be a valid RFC3339 timestamp, got {last_seen}"
    );
    let event = event.expect("mark_online on a fresh user must publish a presence_update");
    assert_eq!(event["type"], "presence_update");
    assert_eq!(event["status"], "online");
    assert_eq!(event["user_id"], user_id.to_string());
}

#[tokio::test]
async fn mark_online_second_device_increments_scard_without_a_second_publish() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_a = unique_conn_id("conn-a");
    let conn_b = unique_conn_id("conn-b");

    presence::mark_online(&mut conn, user_id, &conn_a, 60).await;

    let mut pubsub = subscribe_presence_events(user_id).await;
    presence::mark_online(&mut conn, user_id, &conn_b, 60).await;

    let scard: i64 = conn.scard(redis_keys::presence(user_id)).await.unwrap_or(0);
    let event = recv_json(&mut pubsub, Duration::from_millis(500)).await;

    cleanup(&mut conn, user_id).await;

    assert_eq!(scard, 2, "second device must join the same presence set");
    assert!(event.is_none(), "no publish must fire on a 1->2 device edge (already online)");
}

// ---------------------------------------------------------------------
// mark_offline
// ---------------------------------------------------------------------

#[tokio::test]
async fn mark_offline_one_of_two_devices_drops_scard_without_publish() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_a = unique_conn_id("conn-a");
    let conn_b = unique_conn_id("conn-b");

    presence::mark_online(&mut conn, user_id, &conn_a, 60).await;
    presence::mark_online(&mut conn, user_id, &conn_b, 60).await;

    let mut pubsub = subscribe_presence_events(user_id).await;
    presence::mark_offline(&mut conn, user_id, &conn_a).await;

    let scard: i64 = conn.scard(redis_keys::presence(user_id)).await.unwrap_or(0);
    let event = recv_json(&mut pubsub, Duration::from_millis(500)).await;

    cleanup(&mut conn, user_id).await;

    assert_eq!(scard, 1, "removing one of two devices must leave one behind");
    assert!(event.is_none(), "still-online-elsewhere must not publish");
}

#[tokio::test]
async fn mark_offline_last_device_drops_scard_to_zero_and_publishes_offline() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_uuid = unique_conn_id("conn");

    presence::mark_online(&mut conn, user_id, &conn_uuid, 60).await;

    let mut pubsub = subscribe_presence_events(user_id).await;
    presence::mark_offline(&mut conn, user_id, &conn_uuid).await;

    let scard: i64 = conn.scard(redis_keys::presence(user_id)).await.unwrap_or(0);
    let event = recv_json(&mut pubsub, Duration::from_secs(2)).await;

    cleanup(&mut conn, user_id).await;

    assert_eq!(scard, 0);
    let event = event.expect("last device leaving must publish a presence_update");
    assert_eq!(event["type"], "presence_update");
    assert_eq!(event["status"], "offline");
    assert_eq!(event["user_id"], user_id.to_string());
    assert!(
        event.get("last_seen_at").and_then(|v| v.as_str()).is_some(),
        "offline publish must carry last_seen_at"
    );
}

// ---------------------------------------------------------------------
// last_seen is always refreshed, regardless of edge firing
// ---------------------------------------------------------------------

#[tokio::test]
async fn mark_online_and_offline_always_refresh_last_seen_even_without_an_edge() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_a = unique_conn_id("conn-a");
    let conn_b = unique_conn_id("conn-b");

    presence::mark_online(&mut conn, user_id, &conn_a, 60).await;
    let first_seen: String = conn
        .get(redis_keys::presence_last_seen(user_id))
        .await
        .expect("last_seen set after first mark_online");

    tokio::time::sleep(Duration::from_millis(20)).await;

    // Second device joining does not publish (no edge), but must still touch last_seen.
    presence::mark_online(&mut conn, user_id, &conn_b, 60).await;
    let second_seen: String = conn
        .get(redis_keys::presence_last_seen(user_id))
        .await
        .expect("last_seen still set after second mark_online");

    tokio::time::sleep(Duration::from_millis(20)).await;

    // Removing one of two devices does not publish either, but must still touch last_seen.
    presence::mark_offline(&mut conn, user_id, &conn_a).await;
    let third_seen: String = conn
        .get(redis_keys::presence_last_seen(user_id))
        .await
        .expect("last_seen still set after mark_offline without an edge");

    cleanup(&mut conn, user_id).await;

    assert_ne!(first_seen, second_seen, "last_seen must advance on the no-edge mark_online too");
    assert_ne!(second_seen, third_seen, "last_seen must advance on the no-edge mark_offline too");
}

// ---------------------------------------------------------------------
// TTL refresh on every mark_online call
// ---------------------------------------------------------------------

#[tokio::test]
async fn mark_online_refreshes_ttl_on_every_call_not_just_the_first() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_a = unique_conn_id("conn-a");
    let conn_b = unique_conn_id("conn-b");
    let key = redis_keys::presence(user_id);

    presence::mark_online(&mut conn, user_id, &conn_a, 100).await;
    let ttl_after_first: i64 = conn.ttl(&key).await.unwrap_or(-2);

    // Artificially shrink the TTL, then verify a second mark_online call
    // (second device) restores it to the full value rather than leaving the
    // shrunk one in place.
    let _: () = conn.expire(&key, 2).await.unwrap();
    let ttl_shrunk: i64 = conn.ttl(&key).await.unwrap_or(-2);

    presence::mark_online(&mut conn, user_id, &conn_b, 100).await;
    let ttl_after_second: i64 = conn.ttl(&key).await.unwrap_or(-2);

    cleanup(&mut conn, user_id).await;

    assert!(ttl_after_first > 0, "TTL must be set on the first mark_online");
    assert!(ttl_shrunk <= 2, "sanity: the artificial shrink took effect");
    assert!(
        ttl_after_second > ttl_shrunk,
        "a later mark_online must refresh (EXPIRE) the TTL again, not leave the shrunk value: shrunk={ttl_shrunk} after_second={ttl_after_second}"
    );
}

// ---------------------------------------------------------------------
// heartbeat
// ---------------------------------------------------------------------

#[tokio::test]
async fn heartbeat_refreshes_ttl_and_last_seen_without_touching_membership_or_publishing() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_uuid = unique_conn_id("conn");
    let key = redis_keys::presence(user_id);

    presence::mark_online(&mut conn, user_id, &conn_uuid, 100).await;
    let _: () = conn.expire(&key, 2).await.unwrap();
    let last_seen_before: String = conn
        .get(redis_keys::presence_last_seen(user_id))
        .await
        .unwrap();

    tokio::time::sleep(Duration::from_millis(20)).await;

    let mut pubsub = subscribe_presence_events(user_id).await;
    presence::heartbeat(&mut conn, user_id, 100).await;

    let ttl_after: i64 = conn.ttl(&key).await.unwrap_or(-2);
    let scard: i64 = conn.scard(&key).await.unwrap_or(0);
    let last_seen_after: String = conn
        .get(redis_keys::presence_last_seen(user_id))
        .await
        .unwrap();
    let event = recv_json(&mut pubsub, Duration::from_millis(300)).await;

    cleanup(&mut conn, user_id).await;

    assert!(ttl_after > 2, "heartbeat must EXPIRE the key back up to the full ttl");
    assert_eq!(scard, 1, "heartbeat must not touch set membership");
    assert_ne!(last_seen_before, last_seen_after, "heartbeat must advance last_seen");
    assert!(event.is_none(), "heartbeat must never publish a presence_update");
}

// ---------------------------------------------------------------------
// set_active thin-wrapper contract
// ---------------------------------------------------------------------

#[tokio::test]
async fn set_active_true_behaves_like_mark_online() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_uuid = unique_conn_id("conn");
    let mut pubsub = subscribe_presence_events(user_id).await;

    presence::set_active(&mut conn, user_id, &conn_uuid, true, 60).await;

    let scard: i64 = conn.scard(redis_keys::presence(user_id)).await.unwrap_or(0);
    let event = recv_json(&mut pubsub, Duration::from_secs(2)).await;

    cleanup(&mut conn, user_id).await;

    assert_eq!(scard, 1);
    let event = event.expect("set_active(true) on a fresh user must publish online, like mark_online");
    assert_eq!(event["status"], "online");
}

#[tokio::test]
async fn set_active_false_behaves_like_mark_offline() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_uuid = unique_conn_id("conn");

    presence::set_active(&mut conn, user_id, &conn_uuid, true, 60).await;

    let mut pubsub = subscribe_presence_events(user_id).await;
    presence::set_active(&mut conn, user_id, &conn_uuid, false, 60).await;

    let scard: i64 = conn.scard(redis_keys::presence(user_id)).await.unwrap_or(0);
    let event = recv_json(&mut pubsub, Duration::from_secs(2)).await;

    cleanup(&mut conn, user_id).await;

    assert_eq!(scard, 0);
    let event = event.expect("set_active(false) on the last device must publish offline, like mark_offline");
    assert_eq!(event["status"], "offline");
}

// ---------------------------------------------------------------------
// get_status
// ---------------------------------------------------------------------

#[tokio::test]
async fn get_status_with_zero_connections_after_going_offline() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_uuid = unique_conn_id("conn");

    presence::mark_online(&mut conn, user_id, &conn_uuid, 60).await;
    presence::mark_offline(&mut conn, user_id, &conn_uuid).await;

    let (online, last_seen) = presence::get_status(&mut conn, user_id).await;

    cleanup(&mut conn, user_id).await;

    assert!(!online);
    assert!(last_seen.is_some(), "last_seen must survive even after going offline (no TTL on that key)");
}

#[tokio::test]
async fn get_status_with_at_least_one_connection() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();
    let conn_uuid = unique_conn_id("conn");

    presence::mark_online(&mut conn, user_id, &conn_uuid, 60).await;

    let (online, last_seen) = presence::get_status(&mut conn, user_id).await;

    cleanup(&mut conn, user_id).await;

    assert!(online);
    assert!(last_seen.is_some());
}

#[tokio::test]
async fn get_status_for_a_user_never_seen_at_all() {
    let mut conn = redis_conn().await;
    let user_id = unique_user_id();

    let (online, last_seen) = presence::get_status(&mut conn, user_id).await;

    assert!(!online);
    assert!(last_seen.is_none());
}
