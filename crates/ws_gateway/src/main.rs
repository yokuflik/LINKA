//! Linka WebSocket gateway (ADR 0033).
//!
//! Step 5: connection lifecycle + the routing layer + Redis fan-in. On connect
//! the gateway resolves the user's chats via `GET /internal/ws-bootstrap`
//! (ADR 0036), registers in `chat_instances`, and a single pub/sub task turns
//! `instance_inbox:{server_id}` / `user_events:{uid}` messages into client
//! frames. Presence + typing land in Step 6.

mod bootstrap;
mod fanin;
mod handlers;
mod presence;
mod receipts;
mod routing;
mod send_path;
mod state;
mod ws;

use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use axum::{
    routing::{any, get},
    Router,
};
use linka_common::config::Config;
use state::AppState;
use tokio::sync::mpsc;
use tracing_subscriber::{prelude::*, EnvFilter};

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> anyhow::Result<()> {
    // `ws_gateway healthcheck` — used by the container HEALTHCHECK, since the
    // distroless/static runtime image has no shell / curl. Exits 0 iff the
    // local `/healthz` returns 200.
    if std::env::args().nth(1).as_deref() == Some("healthcheck") {
        std::process::exit(health_probe());
    }

    tracing_subscriber::registry()
        .with(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .with(tracing_subscriber::fmt::layer().compact())
        .init();

    let config = Config::from_env();
    if config.jwt_secret.is_empty() {
        tracing::warn!("JWT_SECRET is empty - every token will be rejected");
    }

    let redis = redis::Client::open(config.redis_url.clone())
        .context("invalid REDIS_URL")?
        .get_multiplexed_async_connection()
        .await
        .context("cannot reach Redis")?;

    let http = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .context("cannot build HTTP client")?;

    let (sub_tx, sub_rx) = mpsc::channel(256);
    let state = Arc::new(AppState::new(config.clone(), redis, http, sub_tx));
    tracing::info!(server_id = %state.server_id, "ws_gateway starting");

    // Single pub/sub task (fan-in) + routing heartbeat.
    tokio::spawn(fanin::run(state.clone(), sub_rx));
    tokio::spawn(routing_heartbeat(state.clone()));

    let app = Router::new()
        .route("/healthz", get(|| async { "ok" }))
        .route("/ws", any(ws::ws_handler))
        .with_state(state.clone());

    let listener = tokio::net::TcpListener::bind(&config.bind_addr)
        .await
        .with_context(|| format!("cannot bind {}", config.bind_addr))?;
    tracing::info!(addr = %config.bind_addr, "listening");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .context("server error")?;

    // Drop every chat registration so fan-out stops targeting this dead process
    // immediately rather than waiting for the TTL.
    let mut conn = state.redis.clone();
    routing::unregister(&mut conn, &state.server_id).await;
    tracing::info!("ws_gateway stopped");
    Ok(())
}

/// Re-`EXPIRE` every `chat_instances` membership before the TTL lapses.
async fn routing_heartbeat(state: Arc<AppState>) {
    let period = Duration::from_secs(state.config.routing_heartbeat_secs.max(1));
    let mut ticker = tokio::time::interval(period);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        ticker.tick().await;
        let mut conn = state.redis.clone();
        routing::heartbeat(&mut conn, &state.server_id, state.config.chat_instance_ttl_secs).await;
    }
}

/// Blocking, dependency-free HTTP/1.0 GET to the local `/healthz`. Returns a
/// process exit code (0 = healthy).
fn health_probe() -> i32 {
    use std::io::{Read, Write};

    let bind = std::env::var("WS_GATEWAY_BIND").unwrap_or_else(|_| "0.0.0.0:8081".to_string());
    let port = bind.rsplit(':').next().unwrap_or("8081");
    let addr = format!("127.0.0.1:{port}");

    let Ok(mut stream) = std::net::TcpStream::connect(&addr) else {
        return 1;
    };
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(3)));
    if stream
        .write_all(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return 1;
    }
    let mut buf = String::new();
    if stream.read_to_string(&mut buf).is_err() {
        return 1;
    }
    if buf.starts_with("HTTP/1.0 200") || buf.starts_with("HTTP/1.1 200") {
        0
    } else {
        1
    }
}

async fn shutdown_signal() {
    let ctrl_c = tokio::signal::ctrl_c();
    #[cfg(unix)]
    let term = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {}
        _ = term => {}
    }
    tracing::info!("shutdown signal received");
}
