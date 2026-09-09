//! The non-trivial WS action handlers (Step 6): typing/recording and
//! subscribe-on-demand presence. The dispatch `match` stays in `ws.rs`; this
//! keeps that file from ballooning.

use linka_common::redis_keys;

use crate::bootstrap;
use crate::fanin::SubCmd;
use crate::state::{AppState, ConnId, ServerFrame};

fn send(state: &AppState, conn_id: ConnId, frame: serde_json::Value) {
    if let Some(h) = state.conns.get(&conn_id) {
        let _ = h.tx.try_send(ServerFrame::Text(frame.to_string()));
    }
}

/// `typing` / `recording` — mirrors `realtime/ws_router.py::_publish_typing`.
/// The full participant + 1:1-privacy gate runs server-side in
/// `GET /internal/typing-allowed`; a denied or failed check drops silently
/// (never leak a typing indicator).
pub async fn typing(state: &AppState, conn_uuid: &str, chat_id: i64, user_id: i64, kind: &str) {
    let l = &state.config.limits;
    let ok = state
        .limiter
        .check_sliding_window(conn_uuid, "ws_typing", l.typing_max, l.typing_window_secs)
        .await;
    if !ok {
        return;
    }

    let allowed = bootstrap::typing_allowed(
        &state.http,
        &state.config.app_internal_url,
        chat_id,
        user_id,
    )
    .await;
    if allowed != Some(true) {
        return;
    }

    state
        .publish_event(
            chat_id,
            serde_json::json!({
                "event": "typing",
                "kind": kind,
                "user_id": user_id.to_string(),
            }),
        )
        .await;
}

/// `subscribe_presence {user_id}` — re-run on every client heartbeat, so this
/// both first-subscribes and revokes on a later privacy change (worst-case
/// staleness ≈ one heartbeat).
pub async fn subscribe_presence(
    state: &AppState,
    conn_id: ConnId,
    conn_uuid: &str,
    user_id: i64,
    target: i64,
) {
    let l = &state.config.limits;
    let ok = state
        .limiter
        .check_sliding_window(
            conn_uuid,
            "ws_sub_presence",
            l.sub_presence_max,
            l.sub_presence_window_secs,
        )
        .await;
    if !ok {
        return;
    }

    if target == user_id {
        send(
            state,
            conn_id,
            serde_json::json!({
                "type": "error",
                "code": "bad_request",
                "message": "Cannot subscribe to your own presence",
            }),
        );
        return;
    }

    let authorized = bootstrap::presence_authorized(
        &state.http,
        &state.config.app_internal_url,
        user_id,
        target,
    )
    .await;

    if authorized != Some(true) {
        // Denied, or the internal check failed (fail closed). Drop any existing
        // watch and tell the client to clear a stale "online".
        if state.remove_presence_watch(conn_id, target) {
            let _ = state
                .sub_tx
                .send(SubCmd::Unsubscribe(redis_keys::presence_events(target)))
                .await;
        }
        send(
            state,
            conn_id,
            serde_json::json!({
                "type": "presence_revoked",
                "user_id": target.to_string(),
                "message": "That user does not share their online status with you",
            }),
        );
        return;
    }

    if state.add_presence_watch(conn_id, target) {
        let _ = state
            .sub_tx
            .send(SubCmd::Subscribe(redis_keys::presence_events(target)))
            .await;
    }

    let mut conn = state.redis.clone();
    let (online, last_seen) = crate::presence::get_status(&mut conn, target).await;
    send(
        state,
        conn_id,
        serde_json::json!({
            "type": "presence_status",
            "user_id": target.to_string(),
            "status": if online { "online" } else { "offline" },
            "last_seen_at": last_seen,
        }),
    );
}
