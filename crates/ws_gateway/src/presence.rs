//! Presence Redis ops — the Rust twin of `realtime/presence_service.py`.
//!
//! `presence:{uid}` is a TTL'd SET of bare connection ids (exactly like the
//! Python side — NOT the `{server_id}:{conn}` form used by `ws:conns`). The
//! separate `presence_srv:` key is deliberately not written: it has no reader.
//! `presence_last_seen:{uid}` is an ISO-8601 string with no TTL. A
//! `presence_update` is published on `presence_events:{uid}` only on the 0↔1
//! device edge.

use linka_common::redis_keys;
use redis::aio::MultiplexedConnection;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

fn now_iso() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_default()
}

async fn touch_last_seen(conn: &mut MultiplexedConnection, user_id: i64) -> String {
    let ts = now_iso();
    let _: redis::RedisResult<()> = redis::cmd("SET")
        .arg(redis_keys::presence_last_seen(user_id))
        .arg(&ts)
        .query_async(conn)
        .await;
    ts
}

/// Connect / foreground: add the connection, refresh TTL, publish `online` on
/// the 0→1 edge.
pub async fn mark_online(
    conn: &mut MultiplexedConnection,
    user_id: i64,
    conn_uuid: &str,
    ttl_secs: u64,
) {
    let key = redis_keys::presence(user_id);
    let count: i64 = redis::pipe()
        .cmd("SADD")
        .arg(&key)
        .arg(conn_uuid)
        .ignore()
        .cmd("EXPIRE")
        .arg(&key)
        .arg(ttl_secs)
        .ignore()
        .cmd("SCARD")
        .arg(&key)
        .query_async(conn)
        .await
        .unwrap_or((0,))
        .0;

    touch_last_seen(conn, user_id).await;

    if count == 1 {
        publish_update(
            conn,
            user_id,
            serde_json::json!({
                "type": "presence_update",
                "user_id": user_id.to_string(),
                "status": "online",
            }),
        )
        .await;
    }
}

/// Disconnect / background: remove the connection, publish `offline` once the
/// last device is gone.
pub async fn mark_offline(conn: &mut MultiplexedConnection, user_id: i64, conn_uuid: &str) {
    let key = redis_keys::presence(user_id);
    let count: i64 = redis::pipe()
        .cmd("SREM")
        .arg(&key)
        .arg(conn_uuid)
        .ignore()
        .cmd("SCARD")
        .arg(&key)
        .query_async(conn)
        .await
        .unwrap_or((0,))
        .0;

    let last_seen = touch_last_seen(conn, user_id).await;

    if count == 0 {
        publish_update(
            conn,
            user_id,
            serde_json::json!({
                "type": "presence_update",
                "user_id": user_id.to_string(),
                "status": "offline",
                "last_seen_at": last_seen,
            }),
        )
        .await;
    }
}

/// Client-driven foreground toggle (ADR 0025): `active` → `mark_online`, else
/// `mark_offline`. Thin wrapper so the edge publish + multi-device set +
/// last_seen stamping are all reused.
pub async fn set_active(
    conn: &mut MultiplexedConnection,
    user_id: i64,
    conn_uuid: &str,
    active: bool,
    ttl_secs: u64,
) {
    if active {
        mark_online(conn, user_id, conn_uuid, ttl_secs).await;
    } else {
        mark_offline(conn, user_id, conn_uuid).await;
    }
}

/// Heartbeat: keep the TTL alive and move last_seen forward.
pub async fn heartbeat(conn: &mut MultiplexedConnection, user_id: i64, ttl_secs: u64) {
    let _: redis::RedisResult<()> = redis::cmd("EXPIRE")
        .arg(redis_keys::presence(user_id))
        .arg(ttl_secs)
        .query_async(conn)
        .await;
    touch_last_seen(conn, user_id).await;
}

/// The pull half of subscribe-on-demand: `{status, last_seen_at}`.
pub async fn get_status(conn: &mut MultiplexedConnection, user_id: i64) -> (bool, Option<String>) {
    let online: i64 = redis::cmd("SCARD")
        .arg(redis_keys::presence(user_id))
        .query_async(conn)
        .await
        .unwrap_or(0);
    let last_seen: Option<String> = redis::cmd("GET")
        .arg(redis_keys::presence_last_seen(user_id))
        .query_async(conn)
        .await
        .unwrap_or(None);
    (online > 0, last_seen)
}

async fn publish_update(conn: &mut MultiplexedConnection, user_id: i64, event: serde_json::Value) {
    let _: redis::RedisResult<i64> = redis::cmd("PUBLISH")
        .arg(redis_keys::presence_events(user_id))
        .arg(event.to_string())
        .query_async(conn)
        .await;
}
