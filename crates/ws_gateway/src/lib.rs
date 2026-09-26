//! Library face of the `ws_gateway` binary — exists solely so integration
//! tests under `tests/` (compiled as separate binaries linking this crate)
//! can reach internal modules like `presence`/`state`/`routing` directly.
//! `main.rs` is the actual process entrypoint and re-exports nothing extra
//! beyond what it already used as private `mod` declarations.

pub mod bootstrap;
pub mod fanin;
pub mod handlers;
pub mod message_ops;
pub mod presence;
pub mod receipts;
pub mod routing;
pub mod send_path;
pub mod state;
pub mod ws;
