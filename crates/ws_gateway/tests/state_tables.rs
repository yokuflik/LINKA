//! Phase 6 (RUST_GATEWAY_TEST_PLAN.md): `ws_gateway::state::AppState`'s
//! in-memory routing tables (`conns`/`chat_subs`/`user_conns`/
//! `presence_subs`/`presence_watches`/`dropped_chats`) — pure logic, no Redis
//! commands are ever issued by these tests.
//!
//! Stage 6.0 blocker: `AppState::new` takes a real `MultiplexedConnection` (no
//! Redis-free constructor exists), so a live `test_redis` on 6380/1 is still
//! required just to build the struct, even though every assertion below is
//! pure in-memory bookkeeping. Already unblocked from the crate side by
//! Phase 3's `crates/ws_gateway/src/lib.rs` (binary -> lib+bin split).

use std::sync::Arc;

use tokio::sync::{mpsc, Notify};
use ws_gateway::fanin::SubCmd;
use ws_gateway::state::{AppState, ConnHandle, ServerFrame};

const REDIS_URL: &str = "redis://127.0.0.1:6380/1";

/// Builds a real `AppState` against `test_redis`. No env parsing needed here
/// (unlike Phase 5) since these tests never touch `Config`-driven behavior —
/// `Config::from_env()` with no overrides is fine, defaults are irrelevant.
async fn build_state() -> AppState {
    let redis = redis::Client::open(REDIS_URL)
        .expect("valid redis url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect to test_redis on 6380/1 — is docker compose up?");
    let http = reqwest::Client::new();
    let (sub_tx, _sub_rx) = mpsc::channel::<SubCmd>(8);
    AppState::new(linka_common::config::Config::from_env(), redis, http, sub_tx)
}

/// Registers a real connection in `state` (via `add_conn`) and returns its id
/// plus the receiving end of its frame channel and cancel handle.
fn register_conn(state: &AppState, user_id: i64) -> (u64, mpsc::Receiver<ServerFrame>, Arc<Notify>) {
    let id = state.alloc_conn_id();
    let (tx, rx) = mpsc::channel(8);
    let cancel = Arc::new(Notify::new());
    state.add_conn(
        id,
        ConnHandle { user_id, uuid: format!("uuid-{id}"), tx, cancel: cancel.clone() },
    );
    (id, rx, cancel)
}

// ---------------------------------------------------------------------
// Stage 6.1 — connection lifecycle (add_conn / remove_conn)
// ---------------------------------------------------------------------

#[tokio::test]
async fn add_conn_first_for_user_returns_true() {
    let state = build_state().await;
    let id = state.alloc_conn_id();
    let (tx, _rx) = mpsc::channel(8);
    let first = state.add_conn(
        id,
        ConnHandle { user_id: 1, uuid: "u1".into(), tx, cancel: Arc::new(Notify::new()) },
    );
    assert!(first, "the first connection for a fresh user must report user_first=true");
}

#[tokio::test]
async fn add_conn_second_device_returns_false() {
    let state = build_state().await;
    let id1 = state.alloc_conn_id();
    let (tx1, _rx1) = mpsc::channel(8);
    let first = state.add_conn(
        id1,
        ConnHandle { user_id: 7, uuid: "a".into(), tx: tx1, cancel: Arc::new(Notify::new()) },
    );
    assert!(first);

    let id2 = state.alloc_conn_id();
    let (tx2, _rx2) = mpsc::channel(8);
    let second = state.add_conn(
        id2,
        ConnHandle { user_id: 7, uuid: "b".into(), tx: tx2, cancel: Arc::new(Notify::new()) },
    );
    assert!(!second, "a second device for the same user must not report user_first again");
}

#[tokio::test]
async fn remove_conn_on_last_connection_reports_user_gone() {
    let state = build_state().await;
    let id = state.alloc_conn_id();
    let (tx, _rx) = mpsc::channel(8);
    state.add_conn(id, ConnHandle { user_id: 9, uuid: "a".into(), tx, cancel: Arc::new(Notify::new()) });

    let (_, user_gone, _) = state.remove_conn(id);
    assert!(user_gone, "removing a user's only connection must report user_gone=true");
}

#[tokio::test]
async fn remove_conn_on_one_of_two_reports_user_not_gone() {
    let state = build_state().await;
    let id1 = state.alloc_conn_id();
    let (tx1, _rx1) = mpsc::channel(8);
    state.add_conn(id1, ConnHandle { user_id: 9, uuid: "a".into(), tx: tx1, cancel: Arc::new(Notify::new()) });
    let id2 = state.alloc_conn_id();
    let (tx2, _rx2) = mpsc::channel(8);
    state.add_conn(id2, ConnHandle { user_id: 9, uuid: "b".into(), tx: tx2, cancel: Arc::new(Notify::new()) });

    let (_, user_gone, _) = state.remove_conn(id1);
    assert!(!user_gone, "removing one of two connections must not report user_gone");
}

#[tokio::test]
async fn remove_conn_called_twice_is_idempotent() {
    let state = build_state().await;
    let id = state.alloc_conn_id();
    let (tx, _rx) = mpsc::channel(8);
    state.add_conn(id, ConnHandle { user_id: 1, uuid: "a".into(), tx, cancel: Arc::new(Notify::new()) });

    let first = state.remove_conn(id);
    assert!(first.1, "first removal must report user_gone");

    let second = state.remove_conn(id);
    assert_eq!(second, (Vec::new(), false, Vec::new()), "a second removal of an already-gone conn must be a clean no-op, not a double-decrement");
}

#[tokio::test]
async fn remove_conn_never_added_is_clean_noop() {
    let state = build_state().await;
    let result = state.remove_conn(999_999);
    assert_eq!(result, (Vec::new(), false, Vec::new()));
}

// ---------------------------------------------------------------------
// Stage 6.2 — chat subscriptions
// ---------------------------------------------------------------------

#[tokio::test]
async fn add_chat_sub_first_subscriber_returns_true() {
    let state = build_state().await;
    let (id, _rx, _cancel) = register_conn(&state, 1);
    let first = state.add_chat_sub(100, id);
    assert!(first, "the first subscriber to a chat must report the 0->1 edge");
}

#[tokio::test]
async fn add_chat_sub_second_subscriber_returns_false() {
    let state = build_state().await;
    let (id1, _rx1, _c1) = register_conn(&state, 1);
    let (id2, _rx2, _c2) = register_conn(&state, 2);
    assert!(state.add_chat_sub(100, id1));
    assert!(!state.add_chat_sub(100, id2), "a second subscriber must not re-report the edge");
}

#[tokio::test]
async fn senders_for_chat_returns_exactly_subscribed_connections() {
    let state = build_state().await;
    let (id1, _rx1, _c1) = register_conn(&state, 1);
    let (_id2, _rx2, _c2) = register_conn(&state, 2);

    state.add_chat_sub(200, id1);
    let senders = state.senders_for_chat(200);
    let ids: Vec<u64> = senders.iter().map(|(cid, _)| *cid).collect();
    assert_eq!(ids, vec![id1]);
}

#[tokio::test]
async fn senders_for_chat_zero_subscribers_is_empty_not_panic() {
    let state = build_state().await;
    let senders = state.senders_for_chat(9_999_999);
    assert!(senders.is_empty());
}

#[tokio::test]
async fn removing_last_subscriber_reports_chat_emptied() {
    let state = build_state().await;
    let id = state.alloc_conn_id();
    let (tx, _rx) = mpsc::channel(8);
    state.add_conn(id, ConnHandle { user_id: 1, uuid: "a".into(), tx, cancel: Arc::new(Notify::new()) });
    state.add_chat_sub(300, id);

    let (emptied, _, _) = state.remove_conn(id);
    assert_eq!(emptied, vec![300]);
}

#[tokio::test]
async fn removing_one_of_two_subscribers_does_not_empty_chat() {
    let state = build_state().await;
    let id1 = state.alloc_conn_id();
    let (tx1, _rx1) = mpsc::channel(8);
    state.add_conn(id1, ConnHandle { user_id: 1, uuid: "a".into(), tx: tx1, cancel: Arc::new(Notify::new()) });
    let id2 = state.alloc_conn_id();
    let (tx2, _rx2) = mpsc::channel(8);
    state.add_conn(id2, ConnHandle { user_id: 2, uuid: "b".into(), tx: tx2, cancel: Arc::new(Notify::new()) });

    state.add_chat_sub(300, id1);
    state.add_chat_sub(300, id2);

    let (emptied, _, _) = state.remove_conn(id1);
    assert!(emptied.is_empty(), "chat with a remaining subscriber must not be reported as emptied");
}

#[tokio::test]
async fn multi_chat_connection_only_empties_chats_where_it_was_sole_subscriber() {
    let state = build_state().await;
    let id1 = state.alloc_conn_id();
    let (tx1, _rx1) = mpsc::channel(8);
    state.add_conn(id1, ConnHandle { user_id: 1, uuid: "a".into(), tx: tx1, cancel: Arc::new(Notify::new()) });
    let id2 = state.alloc_conn_id();
    let (tx2, _rx2) = mpsc::channel(8);
    state.add_conn(id2, ConnHandle { user_id: 2, uuid: "b".into(), tx: tx2, cancel: Arc::new(Notify::new()) });

    // id1 alone in chat 401, shares chat 402 with id2.
    state.add_chat_sub(401, id1);
    state.add_chat_sub(402, id1);
    state.add_chat_sub(402, id2);

    let (emptied, _, _) = state.remove_conn(id1);
    assert_eq!(emptied, vec![401], "only the chat where id1 was the sole subscriber must be reported emptied");

    // chat 402 must still have id2 as a subscriber.
    let remaining = state.senders_for_chat(402);
    assert_eq!(remaining.len(), 1);
    assert_eq!(remaining[0].0, id2);
}

// ---------------------------------------------------------------------
// Stage 6.3 — presence watches
// ---------------------------------------------------------------------

#[tokio::test]
async fn add_presence_watch_first_watcher_returns_true() {
    let state = build_state().await;
    let first = state.add_presence_watch(1, 500);
    assert!(first, "first watcher of a target must report the 0->1 edge");
}

#[tokio::test]
async fn add_presence_watch_second_watcher_returns_false() {
    let state = build_state().await;
    assert!(state.add_presence_watch(1, 500));
    assert!(!state.add_presence_watch(2, 500), "second watcher must not re-report the edge");
}

#[tokio::test]
async fn remove_presence_watch_last_watcher_returns_true() {
    let state = build_state().await;
    state.add_presence_watch(1, 500);
    let emptied = state.remove_presence_watch(1, 500);
    assert!(emptied, "removing the last watcher must report the 1->0 edge");
}

#[tokio::test]
async fn remove_presence_watch_nonexistent_returns_false_no_panic() {
    let state = build_state().await;
    let emptied = state.remove_presence_watch(42, 999);
    assert!(!emptied);
}

#[tokio::test]
async fn multi_target_connection_on_remove_conn_reports_only_targets_that_hit_zero() {
    let state = build_state().await;
    let id1 = state.alloc_conn_id();
    let (tx1, _rx1) = mpsc::channel(8);
    state.add_conn(id1, ConnHandle { user_id: 1, uuid: "a".into(), tx: tx1, cancel: Arc::new(Notify::new()) });
    let id2 = state.alloc_conn_id();
    let (tx2, _rx2) = mpsc::channel(8);
    state.add_conn(id2, ConnHandle { user_id: 2, uuid: "b".into(), tx: tx2, cancel: Arc::new(Notify::new()) });

    // id1 alone watches target 601, shares target 602 with id2.
    state.add_presence_watch(id1, 601);
    state.add_presence_watch(id1, 602);
    state.add_presence_watch(id2, 602);

    let (_, _, presence_emptied) = state.remove_conn(id1);
    assert_eq!(presence_emptied, vec![601], "only the target where id1 was sole watcher must be reported emptied");
}

#[tokio::test]
async fn dropped_chats_cleared_after_remove_conn() {
    let state = build_state().await;
    let id = state.alloc_conn_id();
    let (tx, _rx) = mpsc::channel(8);
    state.add_conn(id, ConnHandle { user_id: 1, uuid: "a".into(), tx, cancel: Arc::new(Notify::new()) });

    state.mark_dropped(id, 111);
    state.remove_conn(id);

    let drained = state.take_dropped(id);
    assert!(drained.is_empty(), "take_dropped for a just-removed connection must be empty, per remove_conn's doc-comment");
}

// ---------------------------------------------------------------------
// Stage 6.4 — gap tracking (ADR 0060)
// ---------------------------------------------------------------------

#[tokio::test]
async fn mark_dropped_then_take_dropped_contains_the_chat() {
    let state = build_state().await;
    state.mark_dropped(1, 222);
    let drained = state.take_dropped(1);
    assert!(drained.contains(&222));
}

#[tokio::test]
async fn take_dropped_drains_the_set() {
    let state = build_state().await;
    state.mark_dropped(1, 222);
    let _ = state.take_dropped(1);
    let second = state.take_dropped(1);
    assert!(second.is_empty(), "a second immediate take_dropped must be empty (drained)");
}

#[tokio::test]
async fn multiple_mark_dropped_calls_accumulate_into_one_set() {
    let state = build_state().await;
    state.mark_dropped(1, 222);
    state.mark_dropped(1, 333);
    let drained = state.take_dropped(1);
    assert_eq!(drained.len(), 2);
    assert!(drained.contains(&222));
    assert!(drained.contains(&333));
}

#[tokio::test]
async fn take_dropped_with_no_drops_is_empty_not_panic() {
    let state = build_state().await;
    let drained = state.take_dropped(555_555);
    assert!(drained.is_empty());
}

// ---------------------------------------------------------------------
// Stage 6.5 — force_disconnect
// ---------------------------------------------------------------------

#[tokio::test]
async fn force_disconnect_fires_notify_for_matching_uuid() {
    let state = build_state().await;
    let id = state.alloc_conn_id();
    let (tx, _rx) = mpsc::channel(8);
    let cancel = Arc::new(Notify::new());
    state.add_conn(id, ConnHandle { user_id: 1, uuid: "target-uuid".into(), tx, cancel: cancel.clone() });

    state.force_disconnect("target-uuid");

    // notified() must resolve immediately since notify_one() was already called.
    tokio::time::timeout(std::time::Duration::from_millis(200), cancel.notified())
        .await
        .expect("cancel Notify must have fired for the matching uuid");
}

#[tokio::test]
async fn force_disconnect_unknown_uuid_is_silent_noop() {
    let state = build_state().await;
    // No connections registered at all — must not panic.
    state.force_disconnect("nonexistent-uuid");
}
