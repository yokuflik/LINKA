//! The single pub/sub task (ADR 0033: one dedicated pub/sub connection for the
//! whole process). It always listens on `instance_inbox:{server_id}` and, on
//! command, dynamically (un)subscribes `user_events:{uid}` as users connect and
//! disconnect. Incoming messages are parsed and routed to local connections.
//!
//! `presence_events:{uid}` subscriptions land in Step 6.

use std::sync::Arc;

use futures_util::StreamExt;
use linka_common::{events::event_chat_id, redis_keys};
use tokio::sync::mpsc;

use crate::state::{AppState, ServerFrame};

/// Dynamic-subscription commands sent from connection setup/teardown.
#[derive(Debug)]
pub enum SubCmd {
    Subscribe(String),
    Unsubscribe(String),
}

/// Run forever, resubscribing with a short backoff on any unexpected failure
/// (mirrors `connection_manager._run_resilient_listener`).
pub async fn run(state: Arc<AppState>, mut cmd_rx: mpsc::Receiver<SubCmd>) {
    loop {
        if let Err(e) = listen(&state, &mut cmd_rx).await {
            tracing::warn!(error = %e, "fan-in listener failed; resubscribing");
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        } else {
            return; // command channel closed => process shutting down
        }
    }
}

async fn listen(
    state: &Arc<AppState>,
    cmd_rx: &mut mpsc::Receiver<SubCmd>,
) -> anyhow::Result<()> {
    let client = redis::Client::open(state.config.redis_url.clone())?;
    let mut pubsub = client.get_async_pubsub().await?;
    let inbox = redis_keys::instance_inbox(&state.server_id);
    pubsub.subscribe(&inbox).await?;

    // Re-assert the currently-connected users' channels after a reconnect.
    for entry in state.user_conns.iter() {
        let _ = pubsub
            .subscribe(redis_keys::user_events(*entry.key()))
            .await;
    }
    for entry in state.presence_subs.iter() {
        let _ = pubsub
            .subscribe(redis_keys::presence_events(*entry.key()))
            .await;
    }

    let (mut sink, mut stream) = pubsub.split();
    loop {
        tokio::select! {
            cmd = cmd_rx.recv() => match cmd {
                Some(SubCmd::Subscribe(ch)) => { let _ = sink.subscribe(&ch).await; }
                Some(SubCmd::Unsubscribe(ch)) => { let _ = sink.unsubscribe(&ch).await; }
                None => return Ok(()),
            },
            msg = stream.next() => {
                let Some(msg) = msg else {
                    anyhow::bail!("pub/sub stream ended");
                };
                let channel = msg.get_channel_name().to_string();
                let payload: String = match msg.get_payload() {
                    Ok(p) => p,
                    Err(_) => continue,
                };
                handle_message(state, &channel, &payload).await;
            }
        }
    }
}

async fn handle_message(state: &Arc<AppState>, channel: &str, payload: &str) {
    let Ok(event) = serde_json::from_str::<serde_json::Value>(payload) else {
        return;
    };

    // A directive aimed at one local connection (no chat_id).
    if event.get("event").and_then(|v| v.as_str()) == Some("force_disconnect") {
        if let Some(cid) = event.get("connection_id").and_then(|v| v.as_str()) {
            state.force_disconnect(cid);
        }
        return;
    }

    if let Some(uid) = channel
        .strip_prefix("user_events:")
        .and_then(|s| s.parse::<i64>().ok())
    {
        handle_user_event(state, uid, &event).await;
        return;
    }

    // Presence update for a watched user -> its local watchers.
    if let Some(uid) = channel
        .strip_prefix("presence_events:")
        .and_then(|s| s.parse::<i64>().ok())
    {
        fan_out(state.senders_for_presence(uid), payload);
        return;
    }

    // Chat-scoped event off `instance_inbox`: route to local subscribers.
    if let Some(chat_id) = event_chat_id(&event) {
        fan_out(state.senders_for_chat(chat_id), payload);
    }
}

async fn handle_user_event(state: &Arc<AppState>, uid: i64, event: &serde_json::Value) {
    let kind = event.get("event").and_then(|v| v.as_str());
    let chat_id = event.get("chat_id").and_then(|v| match v {
        serde_json::Value::String(s) => s.parse::<i64>().ok(),
        serde_json::Value::Number(n) => n.as_i64(),
        _ => None,
    });

    if let (Some("added_to_chat"), Some(chat_id)) = (kind, chat_id) {
        for conn_id in state.conn_ids_for_user(uid) {
            if state.add_chat_sub(chat_id, conn_id) {
                routing_add(state, chat_id).await;
            }
        }
    } else if let (Some("removed_from_chat"), Some(chat_id)) = (kind, chat_id) {
        for conn_id in state.conn_ids_for_user(uid) {
            let (emptied, _) = remove_one_chat_sub(state, chat_id, conn_id);
            if emptied {
                routing_remove(state, chat_id).await;
            }
        }
    }

    // Forwarded to the client too (UI reacts without polling).
    fan_out(state.senders_for_user(uid), &event.to_string());
}

fn remove_one_chat_sub(state: &AppState, chat_id: i64, conn_id: u64) -> (bool, ()) {
    let mut emptied = false;
    if let Some(mut set) = state.chat_subs.get_mut(&chat_id) {
        set.remove(&conn_id);
        emptied = set.is_empty();
    }
    if emptied {
        state.chat_subs.remove_if(&chat_id, |_, v| v.is_empty());
    }
    (emptied, ())
}

async fn routing_add(state: &Arc<AppState>, chat_id: i64) {
    let mut conn = state.redis.clone();
    crate::routing::add_chat(
        &mut conn,
        &state.server_id,
        chat_id,
        state.config.chat_instance_ttl_secs,
    )
    .await;
}

async fn routing_remove(state: &Arc<AppState>, chat_id: i64) {
    let mut conn = state.redis.clone();
    crate::routing::remove_chat(&mut conn, &state.server_id, chat_id).await;
}

/// `try_send` (never `.await`) to each sender so one slow client can't stall
/// fan-in; a full buffer just drops the frame for that client.
fn fan_out(senders: Vec<mpsc::Sender<ServerFrame>>, payload: &str) {
    for tx in senders {
        let _ = tx.try_send(ServerFrame::Text(payload.to_string()));
    }
}
