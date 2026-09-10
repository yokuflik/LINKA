//! WebSocket endpoint: handshake, per-connection reader/writer tasks, and the
//! inbound dispatch table. Step 4 scope — no Redis fan-in yet (Step 5), only
//! `heartbeat` and `send_message` are wired end to end.

use std::sync::Arc;

use tokio::sync::Notify;

use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Query, State,
    },
    http::HeaderMap,
    response::Response,
};
use linka_common::{
    events::{receipt_kind, ClientFrame, MarkFrame},
    redis_keys,
};
use serde::Deserialize;
use tokio::sync::mpsc;

use crate::fanin::SubCmd;
use crate::handlers;
use crate::state::{AppState, ConnHandle, ConnId, ServerFrame};

// Application close codes — must match `realtime/ws_router.py`.
const CLOSE_UNAUTHORIZED: u16 = 4401;
const CLOSE_FORBIDDEN_ORIGIN: u16 = 4403;
const CLOSE_HANDSHAKE_CHURN: u16 = 4429;
pub const CLOSE_CONNECTION_LIMIT: u16 = 4409;

#[derive(Deserialize)]
pub struct TokenQuery {
    token: String,
}

pub async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
    Query(q): Query<TokenQuery>,
    headers: HeaderMap,
) -> Response {
    // Origin (CSWSH) — an attack, not a user error: reject before upgrading.
    let origin = headers.get("origin").and_then(|v| v.to_str().ok());
    let origin_ok = match origin {
        Some(o) => state.config.origin_allowed(o),
        // A browser always sends Origin; absence => non-browser client.
        None => state.config.cors_allow_origins.iter().any(|o| o == "*"),
    };
    if !origin_ok {
        return close_after_upgrade(ws, state, CLOSE_FORBIDDEN_ORIGIN);
    }

    let client_ip = client_ip(&headers);
    let token = q.token;

    ws.on_upgrade(move |socket| async move {
        if let Err(code) = run_connection(socket, state, token, client_ip).await {
            tracing::debug!(code, "connection rejected");
        }
    })
}

/// Upgrade then immediately close with `code`, so the client sees the same
/// 4xxx close it gets from the Python endpoint rather than a bare HTTP error.
fn close_after_upgrade(ws: WebSocketUpgrade, _state: Arc<AppState>, code: u16) -> Response {
    ws.on_upgrade(move |mut socket| async move {
        let _ = socket
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code,
                reason: "".into(),
            })))
            .await;
    })
}

fn client_ip(headers: &HeaderMap) -> String {
    // Behind Caddy the direct peer is the proxy; trust the first XFF hop.
    // (The Python `client_ip` gates this on TRUSTED_PROXY_IPS; the gateway
    // only ever sits behind the same proxy, so the first hop is taken.)
    headers
        .get("x-forwarded-for")
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.split(',').next())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".to_string())
}

/// Returns `Err(close_code)` if the connection was rejected before the read
/// loop, `Ok(())` on a normal disconnect.
async fn run_connection(
    socket: WebSocket,
    state: Arc<AppState>,
    token: String,
    client_ip: String,
) -> Result<(), u16> {
    let mut socket = socket;

    // --- Auth ---
    let claims = linka_common::auth::verify(&token, state.config.jwt_secret.as_bytes())
        .map_err(|_| CLOSE_UNAUTHORIZED)?;
    let user_id = linka_common::auth::user_id(&claims).map_err(|_| CLOSE_UNAUTHORIZED)?;

    // --- Handshake churn (per IP + per user) ---
    let l = &state.config.limits;
    let ip_ok = state
        .limiter
        .check_sliding_window(
            &client_ip,
            "ws_upgrade_ip",
            l.upgrade_ip_max,
            l.upgrade_ip_window_secs,
        )
        .await;
    let user_ok = state
        .limiter
        .check_sliding_window(
            &user_id.to_string(),
            "ws_upgrade_user",
            l.upgrade_user_max,
            l.upgrade_user_window_secs,
        )
        .await;
    if !(ip_ok && user_ok) {
        let _ = send_close(&mut socket, CLOSE_HANDSHAKE_CHURN).await;
        return Err(CLOSE_HANDSHAKE_CHURN);
    }

    // --- Accept: set up the write half ---
    let conn_id = state.alloc_conn_id();
    let conn_uuid = uuid::Uuid::new_v4().to_string();
    let (tx, mut rx) = mpsc::channel::<ServerFrame>(32);

    let (mut sink, mut stream) = {
        use futures_util::StreamExt;
        socket.split()
    };
    let writer = tokio::spawn(async move {
        use futures_util::SinkExt;
        while let Some(frame) = rx.recv().await {
            let out = match frame {
                ServerFrame::Text(t) => sink.send(Message::Text(t)).await,
                ServerFrame::Close(code) => {
                    let _ = sink
                        .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                            code,
                            reason: "".into(),
                        })))
                        .await;
                    break;
                }
            };
            if out.is_err() {
                break;
            }
        }
        let _ = sink.close().await;
    });

    // --- Concurrent-connection cap (cross-process) ---
    let evicted = state
        .limiter
        .register_connection(
            user_id,
            &state.server_id,
            &conn_uuid,
            state.config.ws_conn_max,
            state.config.ws_conn_max_age_secs,
        )
        .await;
    for member in &evicted {
        let (sid, evicted_cid) = linka_common::ratelimit::split_conn_member(member);
        state
            .publish_to_instance(
                &sid,
                &serde_json::json!({
                    "event": "force_disconnect",
                    "connection_id": evicted_cid,
                    "reason": "connection_limit",
                }),
            )
            .await;
    }

    let cancel = Arc::new(Notify::new());
    let user_first = state.add_conn(
        conn_id,
        ConnHandle {
            user_id,
            uuid: conn_uuid.clone(),
            tx,
            cancel: cancel.clone(),
        },
    );
    {
        let mut conn = state.redis.clone();
        crate::presence::mark_online(
            &mut conn,
            user_id,
            &conn_uuid,
            state.config.presence_ttl_secs,
        )
        .await;
    }

    // --- Routing fan-in: resolve chats, populate chat_subs, register ---
    let chat_ids =
        crate::bootstrap::fetch_chat_ids(&state.http, &state.config.app_internal_url, &token).await;
    for chat_id in &chat_ids {
        if state.add_chat_sub(*chat_id, conn_id) {
            let mut rconn = state.redis.clone();
            crate::routing::add_chat(
                &mut rconn,
                &state.server_id,
                *chat_id,
                state.config.chat_instance_ttl_secs,
            )
            .await;
        }
    }
    if user_first {
        let _ = state
            .sub_tx
            .send(SubCmd::Subscribe(redis_keys::user_events(user_id)))
            .await;
    }
    tracing::info!(user_id, conn_id, chats = chat_ids.len(), "ws connected");

    // --- Read loop ---
    let mut flood_strikes: u32 = 0;
    use futures_util::StreamExt;
    loop {
        let msg = tokio::select! {
            _ = cancel.notified() => {
                // Connection-cap eviction over instance_inbox: queue a 4409
                // close for the writer, then tear down.
                if let Some(h) = state.conns.get(&conn_id) {
                    let _ = h.tx.try_send(ServerFrame::Close(CLOSE_CONNECTION_LIMIT));
                }
                break;
            }
            m = stream.next() => match m {
                Some(Ok(m)) => m,
                _ => break,
            },
        };
        let text = match msg {
            Message::Text(t) => t,
            Message::Ping(_) | Message::Pong(_) => continue,
            Message::Close(_) => break,
            Message::Binary(_) => continue,
        };

        let frame_ok = state
            .limiter
            .check_sliding_window(&conn_uuid, "ws_frame", l.frame_max, l.frame_window_secs)
            .await;
        if !frame_ok {
            flood_strikes += 1;
            if flood_strikes >= l.frame_flood_strikes {
                break;
            }
            let cmid = serde_json::from_str::<serde_json::Value>(&text)
                .ok()
                .and_then(|v| v.get("client_message_id").cloned());
            let mut err = serde_json::json!({"type": "error", "code": "rate_limited"});
            if let Some(c) = cmid {
                err["client_message_id"] = c;
            }
            send(&state, conn_id, err.to_string());
            continue;
        }
        flood_strikes = 0;

        match serde_json::from_str::<ClientFrame>(&text) {
            Ok(frame) => dispatch(&state, conn_id, user_id, &conn_uuid, frame).await,
            Err(_) => send(
                &state,
                conn_id,
                r#"{"type":"error","code":"bad_frame"}"#.to_string(),
            ),
        }
    }

    // --- Cleanup (idempotent) ---
    let (emptied_chats, user_gone, emptied_presence) = state.remove_conn(conn_id);
    for chat_id in emptied_chats {
        let mut rconn = state.redis.clone();
        crate::routing::remove_chat(&mut rconn, &state.server_id, chat_id).await;
    }
    if user_gone {
        let _ = state
            .sub_tx
            .send(SubCmd::Unsubscribe(redis_keys::user_events(user_id)))
            .await;
    }
    for target in emptied_presence {
        let _ = state
            .sub_tx
            .send(SubCmd::Unsubscribe(redis_keys::presence_events(target)))
            .await;
    }
    {
        let mut conn = state.redis.clone();
        crate::presence::mark_offline(&mut conn, user_id, &conn_uuid).await;
    }
    state
        .limiter
        .unregister_connection(user_id, &state.server_id, &conn_uuid)
        .await;
    // Give the writer up to 1s to flush a queued close frame, then drop it.
    let _ = tokio::time::timeout(std::time::Duration::from_secs(1), writer).await;
    tracing::info!(user_id, conn_id, "ws disconnected");
    Ok(())
}

/// Rate-limit + `XADD receipt_log_stream` + ack (ADR 0037).
async fn mark(
    state: &AppState,
    conn_id: ConnId,
    conn_uuid: &str,
    user_id: i64,
    m: MarkFrame,
    kind: i32,
    ack_for: &str,
) {
    let l = &state.config.limits;
    let ok = state
        .limiter
        .check_sliding_window(conn_uuid, "ws_receipts", l.receipts_max, l.receipts_window_secs)
        .await;
    if !ok {
        let mut frame = serde_json::json!({"type": "error", "code": "rate_limited"});
        frame["for"] = ack_for.into();
        send(state, conn_id, frame.to_string());
        return;
    }
    if let Err(e) = crate::receipts::enqueue(state, m.chat_id, user_id, kind, m.message_id).await {
        tracing::warn!(error = %e, "receipt enqueue failed");
        return; // fire-and-forget: no error frame, client re-sends on next scroll
    }
    send(
        state,
        conn_id,
        serde_json::json!({"type": "ack", "for": ack_for}).to_string(),
    );
}

/// Enqueue a text frame to a connection's writer; drop it if the buffer is
/// full (a stuck client must never block the caller).
fn send(state: &AppState, conn_id: ConnId, frame: String) {
    if let Some(h) = state.conns.get(&conn_id) {
        let _ = h.tx.try_send(ServerFrame::Text(frame));
    }
}

/// `send` for other modules (`handlers`, `message_ops`).
pub(crate) fn send_frame(state: &AppState, conn_id: ConnId, frame: String) {
    send(state, conn_id, frame);
}

async fn send_close(socket: &mut WebSocket, code: u16) -> Result<(), axum::Error> {
    socket
        .send(Message::Close(Some(axum::extract::ws::CloseFrame {
            code,
            reason: "".into(),
        })))
        .await
}

/// Step 4 dispatch: only `heartbeat` and `send_message` are live. Metered
/// actions still consume their bucket so limits are correct once the handlers
/// land (Steps 5–6).
async fn dispatch(
    state: &AppState,
    conn_id: ConnId,
    user_id: i64,
    conn_uuid: &str,
    frame: ClientFrame,
) {
    let l = &state.config.limits;
    match frame {
        ClientFrame::Heartbeat => {
            let mut conn = state.redis.clone();
            crate::presence::heartbeat(&mut conn, user_id, state.config.presence_ttl_secs).await;
            send(state, conn_id, r#"{"type":"heartbeat_ack"}"#.to_string());
        }
        ClientFrame::SendMessage(m) => {
            let primary = state
                .limiter
                .check_sliding_window(
                    &user_id.to_string(),
                    "send_message",
                    l.send_max,
                    l.send_window_secs,
                )
                .await;
            let burst = state
                .limiter
                .check_sliding_window(
                    &user_id.to_string(),
                    "send_message_burst",
                    l.send_burst_max,
                    l.send_burst_window_secs,
                )
                .await;
            if !(primary && burst) {
                send(
                    state,
                    conn_id,
                    serde_json::json!({
                        "type": "error",
                        "code": "rate_limited",
                        "client_message_id": m.client_message_id,
                    })
                    .to_string(),
                );
                return;
            }
            if let Err(e) = crate::send_path::enqueue(state, &m, user_id).await {
                tracing::warn!(error = %e, "send enqueue failed");
                send(
                    state,
                    conn_id,
                    serde_json::json!({
                        "type": "error",
                        "code": "internal_error",
                        "client_message_id": m.client_message_id,
                    })
                    .to_string(),
                );
                return;
            }
            send(
                state,
                conn_id,
                r#"{"type":"ack","for":"send_message","status":"queued"}"#.to_string(),
            );
        }
        // Receipts (ADR 0037): XADD receipt_log_stream and ack. The Python
        // `receipt_log` worker does the watermark + ADR 0003 mask + live event.
        ClientFrame::MarkDelivered(m) => {
            mark(state, conn_id, conn_uuid, user_id, m, receipt_kind::DELIVERED, "mark_delivered").await;
        }
        ClientFrame::MarkRead(m) => {
            mark(state, conn_id, conn_uuid, user_id, m, receipt_kind::READ, "mark_read").await;
        }
        ClientFrame::MarkPlayed(m) => {
            mark(state, conn_id, conn_uuid, user_id, m, receipt_kind::PLAYED, "mark_played").await;
        }
        ClientFrame::Typing(t) => {
            handlers::typing(state, conn_uuid, t.chat_id, user_id, "typing").await;
        }
        ClientFrame::Recording(t) => {
            handlers::typing(state, conn_uuid, t.chat_id, user_id, "recording_audio").await;
        }
        ClientFrame::SubscribePresence(p) => {
            handlers::subscribe_presence(state, conn_id, conn_uuid, user_id, p.user_id).await;
        }
        ClientFrame::PresenceActive { active } => {
            let _ = state
                .limiter
                .check_sliding_window(
                    conn_uuid,
                    "ws_sub_presence",
                    l.sub_presence_max,
                    l.sub_presence_window_secs,
                )
                .await;
            let mut conn = state.redis.clone();
            crate::presence::set_active(
                &mut conn,
                user_id,
                conn_uuid,
                active,
                state.config.presence_ttl_secs,
            )
            .await;
        }
        ClientFrame::EditMessage(f) => {
            crate::message_ops::edit(state, conn_id, conn_uuid, user_id, f).await;
        }
        ClientFrame::DeleteMessage(f) => {
            crate::message_ops::delete(state, conn_id, conn_uuid, user_id, f).await;
        }
        ClientFrame::RestoreMessage(f) => {
            crate::message_ops::restore(state, conn_id, conn_uuid, user_id, f).await;
        }
        ClientFrame::PurgeMessage(f) => {
            crate::message_ops::purge(state, conn_id, conn_uuid, user_id, f).await;
        }
        ClientFrame::UnsubscribePresence(p) => {
            // Unmetered, like the Python side.
            if state.remove_presence_watch(conn_id, p.user_id) {
                let _ = state
                    .sub_tx
                    .send(SubCmd::Unsubscribe(redis_keys::presence_events(p.user_id)))
                    .await;
            }
        }
        ClientFrame::Other => {}
    }
}
