//! Producer side of the async send path: an `XADD` onto the sharded
//! `message_send_stream`, matching `realtime/fanout/send_queue.enqueue_outgoing_message`.
//! The Python `SendWorker` does everything downstream (idempotency, media HEAD,
//! persist, fan-out) unchanged.

use linka_common::events::{SendMessageFrame, SendStreamEntry};
use linka_common::redis_keys;

use crate::state::AppState;

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
