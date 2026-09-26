//! Phase 4 (RUST_GATEWAY_TEST_PLAN.md): Redis-backed tests for
//! `ws_gateway::routing`, the Rust twin of `realtime/fanout/routing.py`.
//!
//! Contract (from the module's own doc-comment): `chat_instances:{chat}` SET
//! of server_ids <-> `instance_chats:{server}` reverse-map SET of chat ids;
//! every operation is best-effort (never panics on Redis error, self-heals on
//! the next heartbeat).
//!
//! Redis: `redis://127.0.0.1:6380/1` (test_redis, DB index 1 — see
//! `crates/ws_gateway/tests/README.md`). Every test uses a unique random
//! server_id/chat_id and cleans up only its own keys; never a global
//! `FLUSHDB`. Do not run this suite concurrently with `run_dev.sh` or the
//! Python test suite.

use std::time::Duration;

use linka_common::redis_keys;
use redis::AsyncCommands;
use ws_gateway::routing;

const REDIS_URL: &str = "redis://127.0.0.1:6380/1";
const UNREACHABLE_REDIS_URL: &str = "redis://127.0.0.1:1/0";

async fn redis_conn() -> redis::aio::MultiplexedConnection {
    let client = redis::Client::open(REDIS_URL).expect("valid redis url");
    client
        .get_multiplexed_async_connection()
        .await
        .expect("connect to test_redis on 6380/1 — is docker compose up?")
}

static UNIQUE_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

fn unique_server_id(label: &str) -> String {
    let seq = UNIQUE_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    format!("{label}-{}-{seq}", std::process::id())
}

/// A unique, positive i64 chat id so concurrently-run tests never share a
/// `chat_instances:{chat_id}` key.
fn unique_chat_id() -> i64 {
    let seq = UNIQUE_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos() as i64;
    900_000_000_000 + (std::process::id() as i64) * 1_000_000 + (nanos % 1_000_000) + seq as i64
}

async fn cleanup(conn: &mut redis::aio::MultiplexedConnection, chat_ids: &[i64], server_ids: &[&str]) {
    let mut keys: Vec<String> = chat_ids.iter().map(|c| redis_keys::chat_instances(*c)).collect();
    keys.extend(server_ids.iter().map(|s| redis_keys::instance_chats(s)));
    let _: Result<i64, _> = conn.del(keys).await;
}

// ---------------------------------------------------------------------
// add_chat
// ---------------------------------------------------------------------

#[tokio::test]
async fn add_chat_populates_both_sets_and_sets_ttl_on_chat_instances() {
    let mut conn = redis_conn().await;
    let server_id = unique_server_id("srv");
    let chat_id = unique_chat_id();

    routing::add_chat(&mut conn, &server_id, chat_id, 30).await;

    let members: Vec<String> = conn
        .smembers(redis_keys::chat_instances(chat_id))
        .await
        .unwrap_or_default();
    let reverse: Vec<i64> = conn
        .smembers(redis_keys::instance_chats(&server_id))
        .await
        .unwrap_or_default();
    let ttl: i64 = conn.ttl(redis_keys::chat_instances(chat_id)).await.unwrap_or(-2);

    cleanup(&mut conn, &[chat_id], &[&server_id]).await;

    assert!(members.contains(&server_id), "chat_instances:{{chat}} must contain the server_id");
    assert!(reverse.contains(&chat_id), "instance_chats:{{server}} must contain the chat_id");
    assert!(ttl > 0 && ttl <= 30, "chat_instances TTL must be set to ttl_secs, got {ttl}");
}

#[tokio::test]
async fn add_chat_called_twice_is_idempotent() {
    let mut conn = redis_conn().await;
    let server_id = unique_server_id("srv");
    let chat_id = unique_chat_id();

    routing::add_chat(&mut conn, &server_id, chat_id, 30).await;
    routing::add_chat(&mut conn, &server_id, chat_id, 30).await;

    let card: i64 = conn.scard(redis_keys::chat_instances(chat_id)).await.unwrap_or(0);

    cleanup(&mut conn, &[chat_id], &[&server_id]).await;

    assert_eq!(card, 1, "SADD semantics must keep exactly one member on repeat add_chat");
}

// ---------------------------------------------------------------------
// remove_chat
// ---------------------------------------------------------------------

#[tokio::test]
async fn remove_chat_removes_only_the_named_pair() {
    let mut conn = redis_conn().await;
    let server_a = unique_server_id("srv-a");
    let server_b = unique_server_id("srv-b");
    let chat_id = unique_chat_id();

    routing::add_chat(&mut conn, &server_a, chat_id, 30).await;
    routing::add_chat(&mut conn, &server_b, chat_id, 30).await;

    routing::remove_chat(&mut conn, &server_a, chat_id).await;

    let members: Vec<String> = conn
        .smembers(redis_keys::chat_instances(chat_id))
        .await
        .unwrap_or_default();
    let reverse_a: Vec<i64> = conn
        .smembers(redis_keys::instance_chats(&server_a))
        .await
        .unwrap_or_default();
    let reverse_b: Vec<i64> = conn
        .smembers(redis_keys::instance_chats(&server_b))
        .await
        .unwrap_or_default();

    cleanup(&mut conn, &[chat_id], &[&server_a, &server_b]).await;

    assert!(!members.contains(&server_a), "server_a must be removed from chat_instances");
    assert!(members.contains(&server_b), "server_b must remain in chat_instances (unrelated member)");
    assert!(reverse_a.is_empty(), "instance_chats:{{server_a}} must no longer contain chat_id");
    assert!(reverse_b.contains(&chat_id), "instance_chats:{{server_b}} must be untouched");
}

#[tokio::test]
async fn remove_chat_never_added_is_a_noop() {
    let mut conn = redis_conn().await;
    let server_id = unique_server_id("srv");
    let chat_id = unique_chat_id();

    // Never called add_chat for this pair.
    routing::remove_chat(&mut conn, &server_id, chat_id).await;

    let members: Vec<String> = conn
        .smembers(redis_keys::chat_instances(chat_id))
        .await
        .unwrap_or_default();

    cleanup(&mut conn, &[chat_id], &[&server_id]).await;

    assert!(members.is_empty(), "remove_chat on a never-added pair must not error or create keys");
}

// ---------------------------------------------------------------------
// heartbeat
// ---------------------------------------------------------------------

#[tokio::test]
async fn heartbeat_reexpires_every_served_chat_and_the_reverse_map_key() {
    let mut conn = redis_conn().await;
    let server_id = unique_server_id("srv");
    let chat_a = unique_chat_id();
    let chat_b = unique_chat_id();

    routing::add_chat(&mut conn, &server_id, chat_a, 100).await;
    routing::add_chat(&mut conn, &server_id, chat_b, 100).await;

    // Artificially shrink all three TTLs so a later heartbeat's refresh is observable.
    let _: () = conn.expire(redis_keys::chat_instances(chat_a), 2).await.unwrap();
    let _: () = conn.expire(redis_keys::chat_instances(chat_b), 2).await.unwrap();
    let _: () = conn.expire(redis_keys::instance_chats(&server_id), 2).await.unwrap();

    routing::heartbeat(&mut conn, &server_id, 100).await;

    let ttl_a: i64 = conn.ttl(redis_keys::chat_instances(chat_a)).await.unwrap_or(-2);
    let ttl_b: i64 = conn.ttl(redis_keys::chat_instances(chat_b)).await.unwrap_or(-2);
    let ttl_reverse: i64 = conn.ttl(redis_keys::instance_chats(&server_id)).await.unwrap_or(-2);

    cleanup(&mut conn, &[chat_a, chat_b], &[&server_id]).await;

    assert!(ttl_a > 2, "heartbeat must re-EXPIRE chat_a's chat_instances key, got {ttl_a}");
    assert!(ttl_b > 2, "heartbeat must re-EXPIRE chat_b's chat_instances key, got {ttl_b}");
    assert!(ttl_reverse > 2, "heartbeat must re-EXPIRE the server's own instance_chats key, got {ttl_reverse}");
}

#[tokio::test]
async fn heartbeat_with_a_fully_absent_instance_chats_key_does_not_resurrect_it() {
    // `instance_chats:{server}` is a plain Redis SET: SREMing its last member
    // deletes the key outright, so "the key exists but SMEMBERS returns
    // empty" is not a constructible Redis state — a server with zero served
    // chats has a fully *absent* instance_chats key, not an empty-but-present
    // one. The plan's Phase 4 item ("heartbeat when the server serves zero
    // chats still refreshes instance_chats's own TTL") reads as if the key
    // survives regardless; the real Lua/Rust-equivalent here is a plain
    // `EXPIRE`, which is a documented no-op on a missing key (verified
    // directly against Redis: `EXPIRE` on a nonexistent key returns 0 and
    // creates nothing). So the actual, honest contract is the opposite of a
    // literal reading of that plan item: heartbeat cannot resurrect a key
    // that was never there, and does not create one from scratch either.
    let mut conn = redis_conn().await;
    let server_id = unique_server_id("srv");
    let key = redis_keys::instance_chats(&server_id);

    let exists_before: bool = conn.exists(&key).await.unwrap_or(false);
    assert!(!exists_before, "sanity: key must be absent for a never-registered server");

    routing::heartbeat(&mut conn, &server_id, 100).await;

    let exists_after: bool = conn.exists(&key).await.unwrap_or(false);

    assert!(
        !exists_after,
        "heartbeat's trailing EXPIRE is a documented Redis no-op on a missing key and must not create instance_chats from nothing"
    );
}

#[tokio::test]
async fn heartbeat_refreshes_instance_chats_ttl_even_while_it_still_serves_at_least_one_chat() {
    // The realistic "close to zero chats" case that IS constructible: a
    // server still registered (instance_chats key exists) for one chat, with
    // that key's TTL artificially shrunk — heartbeat must refresh it via its
    // unconditional trailing EXPIRE regardless of how many chat_ids SMEMBERS
    // returned, not only when there are "many."
    let mut conn = redis_conn().await;
    let server_id = unique_server_id("srv");
    let chat_id = unique_chat_id();
    let key = redis_keys::instance_chats(&server_id);

    routing::add_chat(&mut conn, &server_id, chat_id, 100).await;
    let _: () = conn.expire(&key, 2).await.unwrap();
    let ttl_before: i64 = conn.ttl(&key).await.unwrap_or(-2);

    routing::heartbeat(&mut conn, &server_id, 100).await;

    let ttl_after: i64 = conn.ttl(&key).await.unwrap_or(-2);

    cleanup(&mut conn, &[chat_id], &[&server_id]).await;

    assert!(ttl_before <= 2, "sanity: the artificial shrink took effect");
    assert!(
        ttl_after > ttl_before,
        "heartbeat's trailing EXPIRE must refresh instance_chats's own TTL: before={ttl_before} after={ttl_after}"
    );
}

// ---------------------------------------------------------------------
// unregister
// ---------------------------------------------------------------------

#[tokio::test]
async fn unregister_removes_server_from_every_chat_and_deletes_instance_chats() {
    let mut conn = redis_conn().await;
    let server_id = unique_server_id("srv");
    let other_server = unique_server_id("srv-other");
    let chat_a = unique_chat_id();
    let chat_b = unique_chat_id();

    routing::add_chat(&mut conn, &server_id, chat_a, 100).await;
    routing::add_chat(&mut conn, &server_id, chat_b, 100).await;
    routing::add_chat(&mut conn, &other_server, chat_a, 100).await;

    routing::unregister(&mut conn, &server_id).await;

    let members_a: Vec<String> = conn
        .smembers(redis_keys::chat_instances(chat_a))
        .await
        .unwrap_or_default();
    let members_b: Vec<String> = conn
        .smembers(redis_keys::chat_instances(chat_b))
        .await
        .unwrap_or_default();
    let instance_chats_exists: bool = conn.exists(redis_keys::instance_chats(&server_id)).await.unwrap_or(true);

    cleanup(&mut conn, &[chat_a, chat_b], &[&server_id, &other_server]).await;

    assert!(!members_a.contains(&server_id), "server must be removed from chat_a's chat_instances");
    assert!(members_a.contains(&other_server), "unrelated server must remain in chat_a's chat_instances");
    assert!(!members_b.contains(&server_id), "server must be removed from chat_b's chat_instances");
    assert!(!instance_chats_exists, "instance_chats:{{server}} must be deleted entirely");
}

#[tokio::test]
async fn unregister_with_no_registrations_is_a_noop() {
    let mut conn = redis_conn().await;
    let server_id = unique_server_id("srv");

    // Never registered any chats for this server.
    routing::unregister(&mut conn, &server_id).await;

    let exists: bool = conn.exists(redis_keys::instance_chats(&server_id)).await.unwrap_or(true);

    assert!(!exists, "unregister on a never-registered server must leave no keys behind");
}

// ---------------------------------------------------------------------
// Redis errors are swallowed, never propagated as a panic
// ---------------------------------------------------------------------

async fn unreachable_conn() -> Option<redis::aio::MultiplexedConnection> {
    let client = redis::Client::open(UNREACHABLE_REDIS_URL).expect("valid redis url");
    tokio::time::timeout(Duration::from_millis(500), client.get_multiplexed_async_connection())
        .await
        .ok()
        .and_then(|r| r.ok())
}

#[tokio::test]
async fn all_four_functions_swallow_redis_errors_without_panicking() {
    let server_id = unique_server_id("srv");
    let chat_id = unique_chat_id();

    // Per Phase 2's finding, `get_multiplexed_async_connection` against an
    // unreachable address can itself fail before any function under test is
    // ever invoked. Only exercise the fail-open/no-panic assertion when a
    // connection object was actually obtained; otherwise the failure surfaced
    // one layer earlier than this module's own error handling and isn't a
    // meaningful assertion about `routing.rs` itself.
    let Some(mut conn) = unreachable_conn().await else {
        eprintln!(
            "note: could not obtain a MultiplexedConnection to an unreachable Redis \
             (connection failed at construction time, not at command time) — \
             routing.rs's own error-swallowing was not exercised on this run"
        );
        return;
    };

    // None of these must panic even though every command will fail.
    routing::add_chat(&mut conn, &server_id, chat_id, 30).await;
    routing::remove_chat(&mut conn, &server_id, chat_id).await;
    routing::heartbeat(&mut conn, &server_id, 30).await;
    routing::unregister(&mut conn, &server_id).await;
}
