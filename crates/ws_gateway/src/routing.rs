//! Chat fan-out routing table — the Rust twin of `realtime/fanout/routing.py`.
//!
//! Each process registers, in Redis, the chats it currently serves (has at
//! least one local WS member of). `realtime_service.publish_event` on the
//! Python side does `SMEMBERS chat_instances:{chat_id}` → `PUBLISH
//! instance_inbox:{server_id}`; a Rust process that has SADDed itself into
//! `chat_instances` is indistinguishable from a Python one.
//!
//! All operations are best-effort: a dropped registration self-heals on the
//! next `heartbeat`.

use linka_common::redis_keys;
use redis::aio::MultiplexedConnection;

/// This process gained its first local member of `chat_id` (0→1 edge).
pub async fn add_chat(conn: &mut MultiplexedConnection, server_id: &str, chat_id: i64, ttl_secs: u64) {
    let res: redis::RedisResult<()> = redis::pipe()
        .cmd("SADD")
        .arg(redis_keys::chat_instances(chat_id))
        .arg(server_id)
        .ignore()
        .cmd("EXPIRE")
        .arg(redis_keys::chat_instances(chat_id))
        .arg(ttl_secs)
        .ignore()
        .cmd("SADD")
        .arg(redis_keys::instance_chats(server_id))
        .arg(chat_id)
        .ignore()
        .query_async(conn)
        .await;
    if let Err(e) = res {
        tracing::warn!(chat_id, error = %e, "routing add_chat failed");
    }
}

/// This process lost its last local member of `chat_id` (1→0 edge).
pub async fn remove_chat(conn: &mut MultiplexedConnection, server_id: &str, chat_id: i64) {
    let res: redis::RedisResult<()> = redis::pipe()
        .cmd("SREM")
        .arg(redis_keys::chat_instances(chat_id))
        .arg(server_id)
        .ignore()
        .cmd("SREM")
        .arg(redis_keys::instance_chats(server_id))
        .arg(chat_id)
        .ignore()
        .query_async(conn)
        .await;
    if let Err(e) = res {
        tracing::warn!(chat_id, error = %e, "routing remove_chat failed");
    }
}

/// Re-assert every registration and refresh TTLs. Runs every
/// `ROUTING_HEARTBEAT_INTERVAL_SECONDS`, which must stay well below the TTL.
pub async fn heartbeat(conn: &mut MultiplexedConnection, server_id: &str, ttl_secs: u64) {
    let chat_ids: Vec<i64> = match redis::cmd("SMEMBERS")
        .arg(redis_keys::instance_chats(server_id))
        .query_async(conn)
        .await
    {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(error = %e, "routing heartbeat SMEMBERS failed");
            return;
        }
    };

    let mut pipe = redis::pipe();
    for chat_id in &chat_ids {
        pipe.cmd("SADD")
            .arg(redis_keys::chat_instances(*chat_id))
            .arg(server_id)
            .ignore()
            .cmd("EXPIRE")
            .arg(redis_keys::chat_instances(*chat_id))
            .arg(ttl_secs)
            .ignore();
    }
    // Keep the reverse-map key alive even when this process serves zero chats,
    // so shutdown can still clean up.
    pipe.cmd("EXPIRE")
        .arg(redis_keys::instance_chats(server_id))
        .arg(ttl_secs)
        .ignore();
    let _: redis::RedisResult<()> = pipe.query_async(conn).await;
}

/// Graceful shutdown: drop this process from every chat it served.
pub async fn unregister(conn: &mut MultiplexedConnection, server_id: &str) {
    let chat_ids: Vec<i64> = redis::cmd("SMEMBERS")
        .arg(redis_keys::instance_chats(server_id))
        .query_async(conn)
        .await
        .unwrap_or_default();

    let mut pipe = redis::pipe();
    for chat_id in &chat_ids {
        pipe.cmd("SREM")
            .arg(redis_keys::chat_instances(*chat_id))
            .arg(server_id)
            .ignore();
    }
    pipe.cmd("DEL")
        .arg(redis_keys::instance_chats(server_id))
        .ignore();
    let _: redis::RedisResult<()> = pipe.query_async(conn).await;
    tracing::info!(chats = chat_ids.len(), "routing unregistered");
}
