//! Producer side of the async receipt path (ADR 0037): an `XADD
//! receipt_log_stream` matching `modules/receipts/receipt_log.enqueue_receipt_event`.
//! The Python `receipt_log` worker does everything downstream — coarse
//! watermark, detailed-log row, ADR 0003 privacy gate, live receipt event.

use linka_common::events::ReceiptStreamEntry;
use linka_common::redis_keys::RECEIPT_STREAM_KEY;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

use crate::state::AppState;

pub async fn enqueue(
    state: &AppState,
    chat_id: i64,
    user_id: i64,
    kind: i32,
    message_id: i64,
) -> redis::RedisResult<String> {
    let entry = ReceiptStreamEntry {
        chat_id,
        user_id,
        kind,
        up_to_message_id: message_id,
        occurred_at: OffsetDateTime::now_utc()
            .format(&Rfc3339)
            .unwrap_or_default(),
    };

    let mut cmd = redis::cmd("XADD");
    cmd.arg(RECEIPT_STREAM_KEY)
        .arg("MAXLEN")
        .arg("~")
        .arg(state.config.receipt_stream_maxlen)
        .arg("*");
    for (field, value) in entry.pairs() {
        cmd.arg(field).arg(value);
    }

    let mut conn = state.redis.clone();
    cmd.query_async(&mut conn).await
}
