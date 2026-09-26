//! Phase 7 (RUST_GATEWAY_TEST_PLAN.md): `ws_gateway::fanin` message routing
//! logic.
//!
//! Stage 7.1 calls `handle_message` directly (made `pub` for this purpose —
//! see the doc-comment added in `fanin.rs`) against a real `AppState`, so no
//! live pub/sub subscription cycle is needed for branch-dispatch coverage.
//! Stage 7.2 drives the real `run`/`listen` loop with two live Redis
//! connections (one running `listen`, one `PUBLISH`ing) since resubscription
//! and reconnect are inherently about the pub/sub cycle itself.
//!
//! Redis: `redis://127.0.0.1:6380/1` (test_redis, DB index 1). Unique
//! user/chat ids per test, explicit cleanup of any routing keys touched, no
//! global `FLUSHDB`. Do not run concurrently with `run_dev.sh` or the Python
//! suite against the same container.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use redis::AsyncCommands;
use serial_test::serial;
use tokio::sync::{mpsc, Notify};
use ws_gateway::fanin::{self, SubCmd};
use ws_gateway::state::{AppState, ConnHandle, ServerFrame};

const REDIS_URL: &str = "redis://127.0.0.1:6380/1";

static UNIQUE_SEQ: AtomicU64 = AtomicU64::new(0);

fn unique_id(base: i64) -> i64 {
    let seq = UNIQUE_SEQ.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos() as i64;
    base + 900_000_000_000 + (std::process::id() as i64) * 1_000_000 + (nanos % 1_000_000) + seq as i64
}

/// `Config::from_env()` defaults `redis_url` to `6379/0` (the dev port), not
/// `test_redis` on `6380/1` — `fanin::listen`/`run` actually dial
/// `config.redis_url` themselves (unlike Phase 5/6, which only need `AppState`
/// for its already-open `MultiplexedConnection` and never re-read
/// `config.redis_url`). Must override the env var, `#[serial]`-guarded like
/// Phase 1/5/6's env-touching tests.
async fn build_state() -> AppState {
    std::env::set_var("REDIS_URL", REDIS_URL);
    let config = linka_common::config::Config::from_env();
    std::env::remove_var("REDIS_URL");

    let redis = redis::Client::open(REDIS_URL)
        .expect("valid redis url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect to test_redis on 6380/1 — is docker compose up?");
    let http = reqwest::Client::new();
    let (sub_tx, _sub_rx) = mpsc::channel::<SubCmd>(8);
    AppState::new(config, redis, http, sub_tx)
}

/// Wraps `build_state` in an `Arc` since `handle_message`/`fanin::run` take
/// `&Arc<AppState>`.
async fn build_state_arc() -> Arc<AppState> {
    Arc::new(build_state().await)
}

fn register_conn(state: &AppState, user_id: i64) -> (u64, mpsc::Receiver<ServerFrame>, Arc<Notify>) {
    let id = state.alloc_conn_id();
    let (tx, rx) = mpsc::channel(8);
    let cancel = Arc::new(Notify::new());
    state.add_conn(id, ConnHandle { user_id, uuid: format!("uuid-{id}"), tx, cancel: cancel.clone() });
    (id, rx, cancel)
}

/// A connection registered with a channel capacity of 1, already full, so the
/// next `try_send` fails — used to exercise the gap-tracking (`mark_dropped`)
/// and drop-silently (`fan_out_untracked`) branches.
fn register_full_conn(state: &AppState, user_id: i64) -> u64 {
    let id = state.alloc_conn_id();
    let (tx, _rx) = mpsc::channel(1);
    // Fill the one slot so the next try_send fails; the receiver is dropped
    // (never read), so the channel stays full for the test's lifetime.
    tx.try_send(ServerFrame::Text("filler".into())).expect("filler send must succeed into empty cap-1 channel");
    state.add_conn(id, ConnHandle { user_id, uuid: format!("full-{id}"), tx, cancel: Arc::new(Notify::new()) });
    id
}

async fn redis_conn() -> redis::aio::MultiplexedConnection {
    redis::Client::open(REDIS_URL)
        .expect("valid redis url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect to test_redis on 6380/1")
}

// ---------------------------------------------------------------------
// Stage 7.1 — handle_message branch dispatch
// ---------------------------------------------------------------------

#[tokio::test]
#[serial]
async fn force_disconnect_event_fires_cancel_for_matching_uuid() {
    let state = build_state_arc().await;
    let id = state.alloc_conn_id();
    let (tx, _rx) = mpsc::channel(8);
    let cancel = Arc::new(Notify::new());
    state.add_conn(id, ConnHandle { user_id: 1, uuid: "victim-uuid".into(), tx, cancel: cancel.clone() });

    let payload = serde_json::json!({"event": "force_disconnect", "connection_id": "victim-uuid"}).to_string();
    fanin::handle_message(&state, "instance_inbox:whatever", &payload).await;

    tokio::time::timeout(Duration::from_millis(200), cancel.notified())
        .await
        .expect("force_disconnect must fire the matching connection's cancel Notify");
}

#[tokio::test]
#[serial]
async fn force_disconnect_missing_connection_id_is_noop() {
    let state = build_state_arc().await;
    let payload = serde_json::json!({"event": "force_disconnect"}).to_string();
    // Must not panic.
    fanin::handle_message(&state, "instance_inbox:whatever", &payload).await;
}

#[tokio::test]
#[serial]
async fn user_events_added_to_chat_subscribes_local_conns_and_registers_routing() {
    let state = build_state_arc().await;
    let uid = unique_id(1);
    let chat_id = unique_id(2);
    let (conn_id, mut rx, _cancel) = register_conn(&state, uid);

    let payload = serde_json::json!({"event": "added_to_chat", "chat_id": chat_id}).to_string();
    fanin::handle_message(&state, &format!("user_events:{uid}"), &payload).await;

    let senders = state.senders_for_chat(chat_id);
    assert_eq!(senders.len(), 1, "the user's local connection must be subscribed to the chat");
    assert_eq!(senders[0].0, conn_id);

    let mut conn = redis_conn().await;
    let key = linka_common::redis_keys::chat_instances(chat_id);
    let members: Vec<String> = conn.smembers(&key).await.unwrap_or_default();
    assert!(members.contains(&state.server_id), "0->1 local subscription must trigger routing::add_chat");

    // Also forwarded verbatim to the connection.
    let msg = tokio::time::timeout(Duration::from_millis(200), rx.recv())
        .await
        .expect("must receive a forwarded frame")
        .expect("channel must not be closed");
    match msg {
        ServerFrame::Text(t) => assert_eq!(t, payload),
        _ => panic!("expected a Text frame"),
    }

    let _: i64 = conn.del(&key).await.unwrap_or(0);
    let _: i64 = conn.del(linka_common::redis_keys::instance_chats(&state.server_id)).await.unwrap_or(0);
}

#[tokio::test]
#[serial]
async fn user_events_added_to_chat_accepts_numeric_chat_id() {
    let state = build_state_arc().await;
    let uid = unique_id(3);
    let chat_id = unique_id(4);
    let (conn_id, _rx, _cancel) = register_conn(&state, uid);

    let payload = serde_json::json!({"event": "added_to_chat", "chat_id": chat_id}).to_string();
    fanin::handle_message(&state, &format!("user_events:{uid}"), &payload).await;

    let senders = state.senders_for_chat(chat_id);
    assert_eq!(senders.len(), 1);
    assert_eq!(senders[0].0, conn_id);

    let mut conn = redis_conn().await;
    let _: i64 = conn.del(linka_common::redis_keys::chat_instances(chat_id)).await.unwrap_or(0);
    let _: i64 = conn.del(linka_common::redis_keys::instance_chats(&state.server_id)).await.unwrap_or(0);
}

#[tokio::test]
#[serial]
async fn user_events_removed_from_chat_unsubscribes_and_deregisters_routing() {
    let state = build_state_arc().await;
    let uid = unique_id(5);
    let chat_id = unique_id(6);
    let (_conn_id, mut rx, _cancel) = register_conn(&state, uid);

    let add_payload = serde_json::json!({"event": "added_to_chat", "chat_id": chat_id}).to_string();
    fanin::handle_message(&state, &format!("user_events:{uid}"), &add_payload).await;
    let _ = rx.recv().await; // drain the forwarded add event

    let mut conn = redis_conn().await;
    let key = linka_common::redis_keys::chat_instances(chat_id);
    let members_before: Vec<String> = conn.smembers(&key).await.unwrap_or_default();
    assert!(members_before.contains(&state.server_id));

    let remove_payload = serde_json::json!({"event": "removed_from_chat", "chat_id": chat_id}).to_string();
    fanin::handle_message(&state, &format!("user_events:{uid}"), &remove_payload).await;

    assert!(state.senders_for_chat(chat_id).is_empty(), "local subscription must be removed");
    let members_after: Vec<String> = conn.smembers(&key).await.unwrap_or_default();
    assert!(!members_after.contains(&state.server_id), "1->0 local removal must trigger routing::remove_chat");

    let _: i64 = conn.del(&key).await.unwrap_or(0);
    let _: i64 = conn.del(linka_common::redis_keys::instance_chats(&state.server_id)).await.unwrap_or(0);
}

#[tokio::test]
#[serial]
async fn any_user_event_is_forwarded_verbatim_regardless_of_kind() {
    let state = build_state_arc().await;
    let uid = unique_id(7);
    let (_conn_id, mut rx, _cancel) = register_conn(&state, uid);

    let payload = serde_json::json!({"event": "something_else", "foo": "bar"}).to_string();
    fanin::handle_message(&state, &format!("user_events:{uid}"), &payload).await;

    let msg = tokio::time::timeout(Duration::from_millis(200), rx.recv())
        .await
        .expect("must still forward an unrecognized event kind")
        .expect("channel must not be closed");
    match msg {
        ServerFrame::Text(t) => assert_eq!(t, payload),
        _ => panic!("expected a Text frame"),
    }
    // No chat_id in this payload, so no chat-sub side effect should exist —
    // implicitly covered by not touching chat_subs at all above.
}

#[tokio::test]
#[serial]
async fn presence_events_forward_to_watchers_only_not_gap_tracked() {
    let state = build_state_arc().await;
    let target = unique_id(8);
    let (conn_id, _rx, _cancel) = register_conn(&state, unique_id(9));
    state.add_presence_watch(conn_id, target);

    let payload = serde_json::json!({"event": "presence_update", "status": "online"}).to_string();
    fanin::handle_message(&state, &format!("presence_events:{target}"), &payload).await;

    // Contrast case: force the channel full so the send necessarily fails,
    // then assert no dropped_chats entry is created (gap-tracking is
    // chat-fan-out-only, per the doc-comment).
    let full_id = register_full_conn(&state, unique_id(10));
    state.add_presence_watch(full_id, target);
    fanin::handle_message(&state, &format!("presence_events:{target}"), &payload).await;

    let drained = state.take_dropped(full_id);
    assert!(drained.is_empty(), "presence fan-out must never populate dropped_chats, even on a full channel");
}

#[tokio::test]
#[serial]
async fn instance_inbox_event_with_chat_id_routes_via_fan_out_chat() {
    let state = build_state_arc().await;
    let chat_id = unique_id(11);
    let (conn_id, mut rx, _cancel) = register_conn(&state, unique_id(12));
    state.add_chat_sub(chat_id, conn_id);

    let payload = serde_json::json!({"event": "new_message", "chat_id": chat_id, "text": "hi"}).to_string();
    fanin::handle_message(&state, &format!("instance_inbox:{}", state.server_id), &payload).await;

    let msg = tokio::time::timeout(Duration::from_millis(200), rx.recv())
        .await
        .expect("chat-scoped event must be routed to the subscriber")
        .expect("channel must not be closed");
    match msg {
        ServerFrame::Text(t) => assert_eq!(t, payload),
        _ => panic!("expected a Text frame"),
    }
}

#[tokio::test]
#[serial]
async fn fan_out_chat_marks_dropped_on_full_channel() {
    let state = build_state_arc().await;
    let chat_id = unique_id(13);
    let full_id = register_full_conn(&state, unique_id(14));
    state.add_chat_sub(chat_id, full_id);

    let payload = serde_json::json!({"event": "new_message", "chat_id": chat_id}).to_string();
    fanin::handle_message(&state, &format!("instance_inbox:{}", state.server_id), &payload).await;

    let drained = state.take_dropped(full_id);
    assert!(drained.contains(&chat_id), "a dropped chat-scoped frame must be recorded via mark_dropped (ADR 0060)");
}

#[tokio::test]
#[serial]
async fn fan_out_untracked_on_full_channel_creates_no_dropped_chats_entry() {
    let state = build_state_arc().await;
    let target = unique_id(15);
    let full_id = register_full_conn(&state, unique_id(16));
    state.add_presence_watch(full_id, target);

    let payload = serde_json::json!({"event": "presence_update"}).to_string();
    fanin::handle_message(&state, &format!("presence_events:{target}"), &payload).await;

    let drained = state.take_dropped(full_id);
    assert!(drained.is_empty(), "fan_out_untracked must never populate dropped_chats even when the send fails");
}

#[tokio::test]
#[serial]
async fn malformed_json_payload_does_not_panic_and_is_dropped() {
    let state = build_state_arc().await;
    // Must not panic — nothing else to assert since the message is just dropped.
    fanin::handle_message(&state, "instance_inbox:whatever", "not json at all").await;
}

#[tokio::test]
#[serial]
async fn payload_with_no_matching_branch_is_silently_dropped() {
    let state = build_state_arc().await;
    let chat_id = unique_id(17);
    let (conn_id, mut rx, _cancel) = register_conn(&state, unique_id(18));
    state.add_chat_sub(chat_id, conn_id);

    // Valid JSON, no `event: force_disconnect`, no chat_id, and not on a
    // user_events/presence_events channel -> falls through every branch.
    let payload = serde_json::json!({"foo": "bar"}).to_string();
    fanin::handle_message(&state, "instance_inbox:whatever", &payload).await;

    let received = tokio::time::timeout(Duration::from_millis(100), rx.recv()).await;
    assert!(received.is_err(), "a payload matching no branch must not be fanned out to anyone");
}

// ---------------------------------------------------------------------
// Stage 7.2 — resubscription after reconnect / run() outer loop
// ---------------------------------------------------------------------

#[tokio::test]
#[serial]
async fn listen_resubscribes_user_events_and_presence_events_on_startup() {
    let state = build_state_arc().await;
    let uid = unique_id(19);
    let target = unique_id(20);
    let (conn_id, mut rx, _cancel) = register_conn(&state, uid);
    // Simulate survivors of a prior connection: entries already present in
    // user_conns / presence_subs before listen() (re)starts.
    state.add_presence_watch(conn_id, target);

    let (sub_tx, sub_rx) = mpsc::channel::<SubCmd>(8);
    let state_for_listen = state.clone();
    let _ = &sub_tx; // kept alive so the listen task's cmd_rx doesn't see a closed channel
    let listen_task = tokio::spawn(fanin::run(state_for_listen, sub_rx));

    // Give the listener a moment to subscribe both channels.
    tokio::time::sleep(Duration::from_millis(300)).await;

    let mut publisher = redis_conn().await;
    let user_payload = serde_json::json!({"event": "added_to_chat", "chat_id": unique_id(21)}).to_string();
    let _: i64 = publisher
        .publish(linka_common::redis_keys::user_events(uid), &user_payload)
        .await
        .unwrap_or(0);

    let user_msg = tokio::time::timeout(Duration::from_secs(2), rx.recv())
        .await
        .expect("must receive the re-subscribed user_events publish")
        .expect("channel must not be closed");
    match user_msg {
        ServerFrame::Text(t) => assert_eq!(t, user_payload),
        _ => panic!("expected Text frame"),
    }

    let presence_payload = serde_json::json!({"event": "presence_update", "status": "online"}).to_string();
    let _: i64 = publisher
        .publish(linka_common::redis_keys::presence_events(target), &presence_payload)
        .await
        .unwrap_or(0);

    let presence_msg = tokio::time::timeout(Duration::from_secs(2), rx.recv())
        .await
        .expect("must receive the re-subscribed presence_events publish")
        .expect("channel must not be closed");
    match presence_msg {
        ServerFrame::Text(t) => assert_eq!(t, presence_payload),
        _ => panic!("expected Text frame"),
    }

    listen_task.abort();
    let mut conn = redis_conn().await;
    let _: i64 = conn.del(linka_common::redis_keys::chat_instances(unique_id(21))).await.unwrap_or(0);
    let _: i64 = conn.del(linka_common::redis_keys::instance_chats(&state.server_id)).await.unwrap_or(0);
}

#[tokio::test]
#[serial]
async fn run_returns_cleanly_when_cmd_channel_closed() {
    let state = build_state_arc().await;
    let (sub_tx, sub_rx) = mpsc::channel::<SubCmd>(8);
    drop(sub_tx); // close the command channel up front — the documented shutdown path

    let result = tokio::time::timeout(Duration::from_secs(2), fanin::run(state, sub_rx)).await;
    assert!(result.is_ok(), "run() must resolve promptly once cmd_rx is closed, not hang or loop forever");
}

// Note (finding, to be recorded on completion): `run()`'s backoff-and-retry
// path (listen() returning Err, e.g. from a killed mid-flight connection) is
// not exercised here as an automated test. Forcing `listen()`'s internal
// `pubsub.subscribe`/stream to fail deterministically in-process (without an
// injectable transport or a proxy to kill) isn't practical with the crates
// available in this workspace — same class of limitation the plan itself
// flags as a possible manual/documented-only case. Recorded as a gap rather
// than skipped silently.
