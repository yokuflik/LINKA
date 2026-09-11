//! Producer side of the async send path: an `XADD` onto the sharded
//! `message_send_stream`, matching `realtime/fanout/send_queue.enqueue_outgoing_message`.
//! The Python `SendWorker` does everything downstream (idempotency, media HEAD,
//! persist, fan-out) unchanged.

use linka_common::events::{SendMessageFrame, SendStreamEntry};
use linka_common::redis_keys;

use crate::state::AppState;

/// ADR 0041: `EXISTS app_worker_alive:{app_server_id}` before enqueueing —
/// missing/expired means nothing is draining `message_send_stream` /
/// `receipt_log_stream` right now, so a `queued` ack here would be a lie.
/// Fails closed: a Redis error is treated the same as "not alive" (the
/// enqueue would fail the same way anyway).
pub async fn app_workers_alive(state: &AppState) -> bool {
    let key = redis_keys::app_worker_alive(&state.config.app_server_id);
    let mut conn = state.redis.clone();
    redis::cmd("EXISTS")
        .arg(&key)
        .query_async::<i64>(&mut conn)
        .await
        .map(|n| n > 0)
        .unwrap_or(false)
}

pub async fn enqueue(
    state: &AppState,
    frame: &SendMessageFrame,
    sender_id: i64,
) -> redis::RedisResult<String> {
    let entry = SendStreamEntry::from_frame(frame, sender_id);
    let key = redis_keys::send_stream_key_for_chat(frame.chat_id, state.config.send_stream_shards);

    // Plain XADD, exactly like the Python producer: it creates the stream if
    // absent; the consumer group is (re)created by the worker's BUSYGROUP-safe
    // `ensure_group`.
    let mut cmd = redis::cmd("XADD");
    cmd.arg(&key)
        .arg("MAXLEN")
        .arg("~")
        .arg(state.config.send_stream_maxlen)
        .arg("*");
    for (field, value) in entry.pairs() {
        cmd.arg(field).arg(value);
    }

    let mut conn = state.redis.clone();
    cmd.query_async(&mut conn).await
}
