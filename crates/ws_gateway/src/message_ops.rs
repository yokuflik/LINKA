//! WS-only message mutations (ADR 0038): `edit_message` / `delete_message` /
//! `restore_message` / `purge_message`. The gateway has no DB, so it relays
//! each to `POST /internal/message/*` (which runs the op and does its own
//! `message_*` fan-out) and turns the HTTP result into the ack / error frame
//! the client expects.

use linka_common::events::{EditFrame, MarkFrame};

use crate::bootstrap::{post_json, PostOutcome};
use crate::state::{AppState, ConnId};
use crate::ws::send_frame;

/// `ws_edit` bucket, shared by all four ops (mirrors the old `_ACTION_LIMITS`).
async fn edit_limit_ok(state: &AppState, conn_uuid: &str) -> bool {
    let l = &state.config.limits;
    state
        .limiter
        .check_sliding_window(conn_uuid, "ws_edit", l.edit_max, l.edit_window_secs)
        .await
}

fn relay(state: &AppState, conn_id: ConnId, action: &str, outcome: PostOutcome) {
    let frame = match outcome {
        PostOutcome::Ok(mut v) => {
            v["type"] = "ack".into();
            v["for"] = action.into();
            v
        }
        PostOutcome::ClientError(code, message) => serde_json::json!({
            "type": "error", "code": code, "for": action, "message": message,
        }),
        PostOutcome::Failed => serde_json::json!({
            "type": "error", "code": "internal_error", "for": action,
        }),
    };
    send_frame(state, conn_id, frame.to_string());
}

pub async fn edit(state: &AppState, conn_id: ConnId, conn_uuid: &str, user_id: i64, f: EditFrame) {
    if !edit_limit_ok(state, conn_uuid).await {
        return rate_limited(state, conn_id, "edit_message");
    }
    let body = serde_json::json!({
        "user_id": user_id, "chat_id": f.chat_id, "message_id": f.message_id,
        "content": f.content,
    });
    let out = post_json(&state.http, &state.config.app_internal_url, "/internal/message/edit", &body).await;
    relay(state, conn_id, "edit_message", out);
}

pub async fn delete(state: &AppState, conn_id: ConnId, conn_uuid: &str, user_id: i64, f: MarkFrame) {
    op(state, conn_id, conn_uuid, user_id, f, "/internal/message/delete", "delete_message").await;
}

pub async fn restore(state: &AppState, conn_id: ConnId, conn_uuid: &str, user_id: i64, f: MarkFrame) {
    op(state, conn_id, conn_uuid, user_id, f, "/internal/message/restore", "restore_message").await;
}

pub async fn purge(state: &AppState, conn_id: ConnId, conn_uuid: &str, user_id: i64, f: MarkFrame) {
    op(state, conn_id, conn_uuid, user_id, f, "/internal/message/purge", "purge_message").await;
}

async fn op(
    state: &AppState,
    conn_id: ConnId,
    conn_uuid: &str,
    user_id: i64,
    f: MarkFrame,
    path: &str,
    action: &str,
) {
    if !edit_limit_ok(state, conn_uuid).await {
        return rate_limited(state, conn_id, action);
    }
    let body = serde_json::json!({
        "user_id": user_id, "chat_id": f.chat_id, "message_id": f.message_id,
    });
    let out = post_json(&state.http, &state.config.app_internal_url, path, &body).await;
    relay(state, conn_id, action, out);
}

fn rate_limited(state: &AppState, conn_id: ConnId, action: &str) {
    send_frame(
        state,
        conn_id,
        serde_json::json!({"type": "error", "code": "rate_limited", "for": action}).to_string(),
    );
}
