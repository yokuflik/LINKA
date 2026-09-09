//! WebSocket wire shapes + Redis stream payloads.
//!
//! Inbound client frames are tagged by `type`; the Python dispatcher
//! (`realtime/ws_router.py`) uses the same tag. Stream entries are flat
//! string->string maps because Redis stream fields are strings (Python's
//! `_clean` turns `None` into `""`, read back as `None`).

use serde::{Deserialize, Serialize};

/// A frame received from a connected client. Unknown/rare actions fall through
/// to `Other` so the dispatcher can reject them without a parse failure.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientFrame {
    SendMessage(SendMessageFrame),
    MarkDelivered(MarkFrame),
    MarkRead(MarkFrame),
    MarkPlayed(MarkFrame),
    Typing(TypingFrame),
    Recording(TypingFrame),
    SubscribePresence(PresenceTargetFrame),
    UnsubscribePresence(PresenceTargetFrame),
    PresenceActive {
        active: bool,
    },
    Heartbeat,
    #[serde(other)]
    Other,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SendMessageFrame {
    #[serde(deserialize_with = "de_id")]
    pub chat_id: i64,
    pub client_message_id: String,
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default = "one")]
    pub r#type: i32,
    #[serde(default, deserialize_with = "de_opt_id")]
    pub reply_to_message_id: Option<i64>,
    #[serde(default)]
    pub media_key: Option<String>,
    #[serde(default)]
    pub media_name: Option<String>,
    #[serde(default)]
    pub media_duration_seconds: Option<i64>,
    #[serde(default)]
    pub media_blur_hash: Option<String>,
    /// E2E header (ADR 0026): opaque JSON object, carried verbatim.
    #[serde(default)]
    pub enc: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MarkFrame {
    #[serde(deserialize_with = "de_id")]
    pub chat_id: i64,
    /// The client sends `message_id`; the stream field is `up_to_message_id`
    /// (watermark semantics). Accept the old name as an alias too.
    #[serde(deserialize_with = "de_id", alias = "up_to_message_id")]
    pub message_id: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TypingFrame {
    #[serde(deserialize_with = "de_id")]
    pub chat_id: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PresenceTargetFrame {
    #[serde(deserialize_with = "de_id")]
    pub user_id: i64,
}

/// XADD payload for `message_send_stream` — field-for-field
/// `send_queue.enqueue_outgoing_message` (all values stringified, `None` -> "").
#[derive(Debug, Clone, Serialize)]
pub struct SendStreamEntry {
    pub chat_id: String,
    pub sender_id: String,
    pub client_message_id: String,
    pub content: String,
    pub r#type: String,
    pub reply_to_message_id: String,
    pub media_key: String,
    pub media_name: String,
    pub media_duration_seconds: String,
    pub media_blur_hash: String,
    /// `json.dumps(enc)` or "".
    pub enc_header: String,
}

impl SendStreamEntry {
    pub fn from_frame(f: &SendMessageFrame, sender_id: i64) -> Self {
        let clean_opt = |o: &Option<String>| o.clone().unwrap_or_default();
        Self {
            chat_id: f.chat_id.to_string(),
            sender_id: sender_id.to_string(),
            client_message_id: f.client_message_id.clone(),
            content: clean_opt(&f.content),
            r#type: f.r#type.to_string(),
            reply_to_message_id: f
                .reply_to_message_id
                .map(|v| v.to_string())
                .unwrap_or_default(),
            media_key: clean_opt(&f.media_key),
            media_name: clean_opt(&f.media_name),
            media_duration_seconds: f
                .media_duration_seconds
                .map(|v| v.to_string())
                .unwrap_or_default(),
            media_blur_hash: clean_opt(&f.media_blur_hash),
            enc_header: match &f.enc {
                Some(v) => serde_json::to_string(v).unwrap_or_default(),
                None => String::new(),
            },
        }
    }

    /// Flatten to the (field, value) pairs `XADD` wants.
    pub fn pairs(&self) -> Vec<(&'static str, String)> {
        vec![
            ("chat_id", self.chat_id.clone()),
            ("sender_id", self.sender_id.clone()),
            ("client_message_id", self.client_message_id.clone()),
            ("content", self.content.clone()),
            ("type", self.r#type.clone()),
            ("reply_to_message_id", self.reply_to_message_id.clone()),
            ("media_key", self.media_key.clone()),
            ("media_name", self.media_name.clone()),
            (
                "media_duration_seconds",
                self.media_duration_seconds.clone(),
            ),
            ("media_blur_hash", self.media_blur_hash.clone()),
            ("enc_header", self.enc_header.clone()),
        ]
    }
}

/// XADD payload for `receipt_log_stream` — `enqueue_receipt_event`.
#[derive(Debug, Clone)]
pub struct ReceiptStreamEntry {
    pub chat_id: i64,
    pub user_id: i64,
    pub kind: i32,
    pub up_to_message_id: i64,
    /// ISO-8601, UTC (`datetime.now(timezone.utc).isoformat()`).
    pub occurred_at: String,
}

impl ReceiptStreamEntry {
    pub fn pairs(&self) -> Vec<(&'static str, String)> {
        vec![
            ("chat_id", self.chat_id.to_string()),
            ("user_id", self.user_id.to_string()),
            ("kind", self.kind.to_string()),
            ("up_to_message_id", self.up_to_message_id.to_string()),
            ("occurred_at", self.occurred_at.clone()),
        ]
    }
}

/// Receipt kind ints, matching the Python enum.
pub mod receipt_kind {
    pub const DELIVERED: i32 = 2;
    pub const READ: i32 = 3;
    pub const PLAYED: i32 = 4;
}

/// An event coming back from Python over `instance_inbox` / a user channel.
/// The gateway routes it by `chat_id` (when present) and otherwise forwards it
/// verbatim to the target connection, so it stays an opaque `Value`.
pub fn event_chat_id(event: &serde_json::Value) -> Option<i64> {
    match event.get("chat_id")? {
        serde_json::Value::String(s) => s.parse().ok(),
        serde_json::Value::Number(n) => n.as_i64(),
        _ => None,
    }
}

fn one() -> i32 {
    1
}

fn de_id<'de, D>(d: D) -> Result<i64, D::Error>
where
    D: serde::Deserializer<'de>,
{
    // Clients may send ids as strings (IdStr) or numbers.
    match serde_json::Value::deserialize(d)? {
        serde_json::Value::String(s) => s.parse().map_err(serde::de::Error::custom),
        serde_json::Value::Number(n) => n
            .as_i64()
            .ok_or_else(|| serde::de::Error::custom("id not an integer")),
        _ => Err(serde::de::Error::custom("id must be string or number")),
    }
}

fn de_opt_id<'de, D>(d: D) -> Result<Option<i64>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    match Option::<serde_json::Value>::deserialize(d)? {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(serde_json::Value::String(s)) if s.is_empty() => Ok(None),
        Some(serde_json::Value::String(s)) => s.parse().map(Some).map_err(serde::de::Error::custom),
        Some(serde_json::Value::Number(n)) => Ok(n.as_i64()),
        _ => Err(serde::de::Error::custom("id must be string or number")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_send_message_frame() {
        let raw =
            r#"{"type":"send_message","chat_id":"123","client_message_id":"c1","content":"hi"}"#;
        let f: ClientFrame = serde_json::from_str(raw).unwrap();
        match f {
            ClientFrame::SendMessage(m) => {
                assert_eq!(m.chat_id, 123);
                assert_eq!(m.r#type, 1);
                let e = SendStreamEntry::from_frame(&m, 999);
                assert_eq!(e.sender_id, "999");
                assert_eq!(e.reply_to_message_id, "");
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn mark_frame_takes_client_message_id_field() {
        let f: ClientFrame =
            serde_json::from_str(r#"{"type":"mark_read","chat_id":"5","message_id":"42"}"#).unwrap();
        match f {
            ClientFrame::MarkRead(m) => {
                assert_eq!(m.chat_id, 5);
                assert_eq!(m.message_id, 42);
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn unknown_type_is_other() {
        let f: ClientFrame = serde_json::from_str(r#"{"type":"totally_new"}"#).unwrap();
        assert!(matches!(f, ClientFrame::Other));
    }
}
