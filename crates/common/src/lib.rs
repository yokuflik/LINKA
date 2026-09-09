//! Shared contract between the Python backend and the Rust `ws_gateway`.
//!
//! Everything that must stay byte-identical across the two languages lives
//! here: JWT claims, Redis key/stream/channel names, and the WebSocket wire
//! event shapes. See ADR 0033 and `RUST_WS_GATEWAY_PLAN.md`.

pub mod auth;
pub mod config;
pub mod events;
pub mod ratelimit;
pub mod redis_keys;
