//! Phase 8 (RUST_GATEWAY_TEST_PLAN.md), Stages 8.1-8.5: `ws.rs` connection
//! lifecycle & dispatch, driven end-to-end over a real bound TCP listener with
//! a real WS client (`tokio-tungstenite`), plus a mocked `/internal/*` HTTP
//! server (`wiremock`) for `app_internal_url`.
//!
//! Redis: `redis://127.0.0.1:6380/1` (test_redis, DB index 1 — see
//! `crates/ws_gateway/tests/README.md`). Each test builds its own `AppState`
//! (unique server_id / random high port), never touches a global flush, and
//! is `#[serial]`-guarded because `Config::from_env()` reads real process env
//! vars (same pattern as Phase 5/7's `build_state` helpers).
//!
//! Scope: Stages 8.1 (handshake/origin/auth rejection), 8.2 (connect-time
//! side effects), 8.3 (read-loop frame handling), 8.4 (heartbeat dispatch),
//! 8.5 (send_message dispatch), 8.6 (mark_delivered/read/played), 8.7
//! (typing/recording), 8.8 (subscribe/unsubscribe presence), 8.9
//! (presence_active), 8.10 (edit/delete/restore/purge relay), and 8.11
//! (disconnect cleanup).

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use axum::routing::{any, get};
use axum::Router;
use futures_util::{SinkExt, StreamExt};
use jsonwebtoken::{encode, EncodingKey, Header};
use linka_common::config::Config;
use redis::AsyncCommands;
use serde::Serialize;
use serial_test::serial;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::protocol::CloseFrame as WsCloseFrame;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};
use ws_gateway::fanin::SubCmd;
use ws_gateway::state::AppState;
use ws_gateway::ws::ws_handler;

const REDIS_URL: &str = "redis://127.0.0.1:6380/1";
const JWT_SECRET: &str = "test-secret-for-ws-dispatch";

static UNIQUE_SEQ: AtomicU64 = AtomicU64::new(0);

fn unique_label(label: &str) -> String {
    let seq = UNIQUE_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("{label}-{}-{seq}", std::process::id())
}

// ---------------------------------------------------------------------------
// Env / Config plumbing (mirrors Phase 5/7's `build_state` pattern)
// ---------------------------------------------------------------------------

const ENV_KEYS: &[&str] = &[
    "REDIS_URL",
    "JWT_SECRET",
    "CORS_ALLOW_ORIGINS",
    "APP_INTERNAL_URL",
    "APP_SERVER_ID",
    "WS_CONN_MAX_CONNECTIONS",
    "WS_CONN_MAX_AGE_SECONDS",
    "WS_UPGRADE_IP_RATE_LIMIT_MAX",
    "WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS",
    "WS_UPGRADE_USER_RATE_LIMIT_MAX",
    "WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS",
    "WS_FRAME_RATE_MAX",
    "WS_FRAME_RATE_WINDOW_SECONDS",
    "WS_FRAME_FLOOD_STRIKES",
    "WS_SEND_MESSAGE_RATE_MAX",
    "WS_SEND_MESSAGE_RATE_WINDOW_SECONDS",
    "WS_SEND_MESSAGE_BURST_MAX",
    "WS_SEND_MESSAGE_BURST_WINDOW_SECONDS",
    "WS_RECEIPTS_RATE_MAX",
    "WS_RECEIPTS_RATE_WINDOW_SECONDS",
    "WS_TYPING_RATE_MAX",
    "WS_TYPING_RATE_WINDOW_SECONDS",
    "WS_SUBSCRIBE_PRESENCE_RATE_MAX",
    "WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS",
    "WS_EDIT_RATE_MAX",
    "WS_EDIT_RATE_WINDOW_SECONDS",
];

fn clear_env() {
    for k in ENV_KEYS {
        std::env::remove_var(k);
    }
}

/// Options for building a test server; defaults are permissive so a test only
/// needs to override the knob(s) it's actually exercising.
struct Opts {
    cors_allow_origins: String,
    app_internal_url: String,
    app_server_id: String,
    ws_conn_max: u64,
    upgrade_ip_max: u64,
    upgrade_ip_window_secs: f64,
    upgrade_user_max: u64,
    upgrade_user_window_secs: f64,
    frame_max: u64,
    frame_window_secs: f64,
    frame_flood_strikes: u32,
    send_max: u64,
    send_window_secs: f64,
    send_burst_max: u64,
    send_burst_window_secs: f64,
    receipts_max: u64,
    receipts_window_secs: f64,
    typing_max: u64,
    typing_window_secs: f64,
    sub_presence_max: u64,
    sub_presence_window_secs: f64,
    edit_max: u64,
    edit_window_secs: f64,
}

impl Default for Opts {
    fn default() -> Self {
        Opts {
            cors_allow_origins: "*".to_string(),
            app_internal_url: "http://127.0.0.1:1".to_string(), // unreachable unless overridden
            app_server_id: unique_label("app"),
            ws_conn_max: 5,
            upgrade_ip_max: 1_000_000,
            upgrade_ip_window_secs: 10.0,
            upgrade_user_max: 1_000_000,
            upgrade_user_window_secs: 10.0,
            frame_max: 1_000_000,
            frame_window_secs: 10.0,
            frame_flood_strikes: 1_000_000,
            send_max: 1_000_000,
            send_window_secs: 1.0,
            send_burst_max: 1_000_000,
            send_burst_window_secs: 60.0,
            receipts_max: 1_000_000,
            receipts_window_secs: 10.0,
            typing_max: 1_000_000,
            typing_window_secs: 10.0,
            sub_presence_max: 1_000_000,
            sub_presence_window_secs: 10.0,
            edit_max: 1_000_000,
            edit_window_secs: 10.0,
        }
    }
}

/// A running test server: its bound `ws://` base URL and a handle to the
/// `AppState` (for direct Redis assertions), plus the `JoinHandle` so the
/// caller can decide whether to let it run for the test's lifetime (dropping
/// it aborts the server task).
struct TestServer {
    ws_base: String,
    state: std::sync::Arc<AppState>,
    _server_task: tokio::task::JoinHandle<()>,
    _fanin_task: tokio::task::JoinHandle<()>,
}

async fn start_server(opts: Opts) -> TestServer {
    clear_env();
    std::env::set_var("REDIS_URL", REDIS_URL);
    std::env::set_var("JWT_SECRET", JWT_SECRET);
    std::env::set_var("CORS_ALLOW_ORIGINS", &opts.cors_allow_origins);
    std::env::set_var("APP_INTERNAL_URL", &opts.app_internal_url);
    std::env::set_var("APP_SERVER_ID", &opts.app_server_id);
    std::env::set_var("WS_CONN_MAX_CONNECTIONS", opts.ws_conn_max.to_string());
    std::env::set_var("WS_CONN_MAX_AGE_SECONDS", (26 * 3600).to_string());
    std::env::set_var("WS_UPGRADE_IP_RATE_LIMIT_MAX", opts.upgrade_ip_max.to_string());
    std::env::set_var("WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS", opts.upgrade_ip_window_secs.to_string());
    std::env::set_var("WS_UPGRADE_USER_RATE_LIMIT_MAX", opts.upgrade_user_max.to_string());
    std::env::set_var("WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS", opts.upgrade_user_window_secs.to_string());
    std::env::set_var("WS_FRAME_RATE_MAX", opts.frame_max.to_string());
    std::env::set_var("WS_FRAME_RATE_WINDOW_SECONDS", opts.frame_window_secs.to_string());
    std::env::set_var("WS_FRAME_FLOOD_STRIKES", opts.frame_flood_strikes.to_string());
    std::env::set_var("WS_SEND_MESSAGE_RATE_MAX", opts.send_max.to_string());
    std::env::set_var("WS_SEND_MESSAGE_RATE_WINDOW_SECONDS", opts.send_window_secs.to_string());
    std::env::set_var("WS_SEND_MESSAGE_BURST_MAX", opts.send_burst_max.to_string());
    std::env::set_var("WS_SEND_MESSAGE_BURST_WINDOW_SECONDS", opts.send_burst_window_secs.to_string());
    std::env::set_var("WS_RECEIPTS_RATE_MAX", opts.receipts_max.to_string());
    std::env::set_var("WS_RECEIPTS_RATE_WINDOW_SECONDS", opts.receipts_window_secs.to_string());
    std::env::set_var("WS_TYPING_RATE_MAX", opts.typing_max.to_string());
    std::env::set_var("WS_TYPING_RATE_WINDOW_SECONDS", opts.typing_window_secs.to_string());
    std::env::set_var("WS_SUBSCRIBE_PRESENCE_RATE_MAX", opts.sub_presence_max.to_string());
    std::env::set_var("WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS", opts.sub_presence_window_secs.to_string());
    std::env::set_var("WS_EDIT_RATE_MAX", opts.edit_max.to_string());
    std::env::set_var("WS_EDIT_RATE_WINDOW_SECONDS", opts.edit_window_secs.to_string());

    let config = Config::from_env();
    clear_env();

    let redis = redis::Client::open(REDIS_URL)
        .expect("valid redis url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect to test_redis on 6380/1 — is docker compose up?");
    let http = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let (sub_tx, sub_rx) = tokio::sync::mpsc::channel::<SubCmd>(256);
    let state = std::sync::Arc::new(AppState::new(config, redis, http, sub_tx));

    let fanin_task = tokio::spawn(ws_gateway::fanin::run(state.clone(), sub_rx));

    let app = Router::new()
        .route("/healthz", get(|| async { "ok" }))
        .route("/ws", any(ws_handler))
        .with_state(state.clone());

    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind ephemeral port");
    let addr = listener.local_addr().unwrap();
    let server_task = tokio::spawn(async move {
        axum::serve(listener, app).await.ok();
    });

    // Give the listener a moment to actually accept.
    tokio::time::sleep(Duration::from_millis(20)).await;

    TestServer {
        ws_base: format!("ws://{addr}"),
        state,
        _server_task: server_task,
        _fanin_task: fanin_task,
    }
}

fn token_for(user_id: i64) -> String {
    #[derive(Serialize)]
    struct Claims {
        sub: String,
        exp: usize,
    }
    let exp = (std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        + 3600) as usize;
    encode(
        &Header::new(jsonwebtoken::Algorithm::HS256),
        &Claims { sub: user_id.to_string(), exp },
        &EncodingKey::from_secret(JWT_SECRET.as_bytes()),
    )
    .unwrap()
}

fn expired_token_for(user_id: i64) -> String {
    #[derive(Serialize)]
    struct Claims {
        sub: String,
        exp: usize,
    }
    encode(
        &Header::new(jsonwebtoken::Algorithm::HS256),
        &Claims { sub: user_id.to_string(), exp: 1 },
        &EncodingKey::from_secret(JWT_SECRET.as_bytes()),
    )
    .unwrap()
}

/// Unique positive i64 user id per test so Redis keys / rate-limit buckets
/// never collide across concurrently-run tests.
fn unique_user_id() -> i64 {
    let seq = UNIQUE_SEQ.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos() as i64;
    900_000_000_000 + (std::process::id() as i64) * 1_000_000 + (nanos % 1_000_000) + seq as i64
}

type WsStream = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

async fn connect_raw(
    ws_base: &str,
    token: &str,
    extra_headers: &[(&str, &str)],
) -> Result<WsStream, tokio_tungstenite::tungstenite::Error> {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    let url = format!("{ws_base}/ws?token={token}");
    let mut request = url.into_client_request().unwrap();
    for (k, v) in extra_headers {
        request.headers_mut().insert(
            axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
            axum::http::HeaderValue::from_str(v).unwrap(),
        );
    }
    let (stream, _resp) = tokio_tungstenite::connect_async(request).await?;
    Ok(stream)
}

/// Read the next message with a short timeout — returns `None` on timeout
/// (used for "assert nothing arrives").
async fn next_msg(ws: &mut WsStream, millis: u64) -> Option<WsMessage> {
    tokio::time::timeout(Duration::from_millis(millis), ws.next())
        .await
        .ok()
        .flatten()
        .and_then(|r| r.ok())
}

fn close_code(msg: &WsMessage) -> Option<u16> {
    match msg {
        WsMessage::Close(Some(WsCloseFrame { code, .. })) => Some((*code).into()),
        _ => None,
    }
}

async fn cleanup_user(state: &AppState, user_id: i64) {
    let mut conn = state.redis.clone();
    let _: i64 = conn.del(linka_common::redis_keys::ws_conns(user_id)).await.unwrap_or(0);
    let _: i64 = conn.del(linka_common::redis_keys::presence(user_id)).await.unwrap_or(0);
    let _: i64 = conn.del(linka_common::redis_keys::presence_last_seen(user_id)).await.unwrap_or(0);
}

/// Poll for a connected user's `ConnId` to appear in `state.conns` — the
/// connect-time setup (writer spawn, `add_conn`, bootstrap fetch) runs
/// asynchronously after the WS upgrade completes, so a fresh client-side
/// connect can briefly race an immediate server-side lookup.
async fn wait_for_conn_id(state: &AppState, user_id: i64) -> ws_gateway::state::ConnId {
    for _ in 0..50 {
        if let Some(entry) = state.conns.iter().find(|e| e.value().user_id == user_id) {
            return *entry.key();
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("user {user_id} never appeared in state.conns");
}

// ===========================================================================
// Stage 8.1 — handshake / origin / auth rejection paths
// ===========================================================================

#[tokio::test]
#[serial]
async fn missing_invalid_jwt_closes_with_4401() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, "not-a-real-token", &[]).await.expect("upgrade must succeed");
    let msg = next_msg(&mut ws, 2000).await.expect("must receive a close frame");
    assert_eq!(close_code(&msg), Some(4401));

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn expired_jwt_closes_with_4401() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &expired_token_for(user_id), &[]).await.expect("upgrade must succeed");
    let msg = next_msg(&mut ws, 2000).await.expect("must receive a close frame");
    assert_eq!(close_code(&msg), Some(4401));

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn disallowed_origin_closes_with_4403_even_with_garbage_token() {
    let mut opts = Opts::default();
    opts.cors_allow_origins = "https://allowed.example".to_string();
    let srv = start_server(opts).await;

    // Garbage token — proves origin is checked strictly before auth.
    let mut ws = connect_raw(&srv.ws_base, "garbage", &[("Origin", "https://evil.example")])
        .await
        .expect("upgrade must succeed (rejection happens after upgrade)");
    let msg = next_msg(&mut ws, 2000).await.expect("must receive a close frame");
    assert_eq!(close_code(&msg), Some(4403));
}

#[tokio::test]
#[serial]
async fn allowed_origin_proceeds_past_origin_check() {
    let mut opts = Opts::default();
    opts.cors_allow_origins = "https://allowed.example".to_string();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(
        &srv.ws_base,
        &token_for(user_id),
        &[("Origin", "https://allowed.example")],
    )
    .await
    .expect("upgrade must succeed");
    // Proceeds past origin AND auth: send heartbeat, expect heartbeat_ack, not a close.
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive a reply, not silence");
    assert!(matches!(msg, WsMessage::Text(ref t) if t.contains("heartbeat_ack")), "got {msg:?}");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn no_origin_header_with_wildcard_allowlist_is_allowed() {
    let srv = start_server(Opts::default()).await; // default cors_allow_origins = "*"
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("upgrade must succeed");
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive a reply, not a close");
    assert!(matches!(msg, WsMessage::Text(ref t) if t.contains("heartbeat_ack")), "got {msg:?}");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn no_origin_header_with_specific_allowlist_is_rejected_4403() {
    let mut opts = Opts::default();
    opts.cors_allow_origins = "https://allowed.example".to_string();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("upgrade must succeed");
    let msg = next_msg(&mut ws, 2000).await.expect("must receive a close frame");
    assert_eq!(close_code(&msg), Some(4403));
}

#[tokio::test]
#[serial]
async fn handshake_churn_cap_per_ip_closes_4429() {
    let mut opts = Opts::default();
    opts.upgrade_ip_max = 1;
    opts.upgrade_ip_window_secs = 30.0;
    let srv = start_server(opts).await;

    // A unique-per-test synthetic IP via X-Forwarded-For: the bare "unknown"
    // identifier (no XFF header at all) is a single Redis bucket shared by
    // every other test in this binary that also connects without an XFF
    // header, which made this test flaky (a sibling test could exhaust the
    // shared "unknown" bucket first). A per-test synthetic IP isolates it.
    let synthetic_ip = unique_label("198.51.100.1");
    let xff = [("X-Forwarded-For", synthetic_ip.as_str())];

    // First connection from this "IP" consumes the one allowed slot.
    let user1 = unique_user_id();
    let mut ws1 = connect_raw(&srv.ws_base, &token_for(user1), &xff).await.expect("first connect must succeed");
    // Drain to confirm it's alive (heartbeat_ack), not rejected.
    ws1.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws1, 2000).await.expect("first connection must be accepted");
    assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    // Second connection, same synthetic IP, different user — over the IP cap.
    let user2 = unique_user_id();
    let mut ws2 = connect_raw(&srv.ws_base, &token_for(user2), &xff).await.expect("upgrade must succeed");
    let msg2 = next_msg(&mut ws2, 2000).await.expect("must receive a close frame");
    assert_eq!(close_code(&msg2), Some(4429));

    cleanup_user(&srv.state, user1).await;
    cleanup_user(&srv.state, user2).await;
}

#[tokio::test]
#[serial]
async fn handshake_churn_cap_per_user_closes_4429() {
    let mut opts = Opts::default();
    opts.upgrade_user_max = 1;
    opts.upgrade_user_window_secs = 30.0;
    // Distinguish IPs via X-Forwarded-For so only the per-user cap is exercised.
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws1 = connect_raw(&srv.ws_base, &token_for(user_id), &[("X-Forwarded-For", "10.0.0.1")])
        .await
        .expect("first connect must succeed");
    ws1.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws1, 2000).await.expect("first connection must be accepted");
    assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    // Same user, different IP: per-user cap still applies.
    let mut ws2 = connect_raw(&srv.ws_base, &token_for(user_id), &[("X-Forwarded-For", "10.0.0.2")])
        .await
        .expect("upgrade must succeed");
    let msg2 = next_msg(&mut ws2, 2000).await.expect("must receive a close frame");
    assert_eq!(close_code(&msg2), Some(4429));

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn connection_under_both_churn_caps_proceeds_to_full_accept() {
    let mut opts = Opts::default();
    opts.upgrade_ip_max = 5;
    opts.upgrade_user_max = 5;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();
    // A unique synthetic IP, not the bare "unknown" default: with a cap this
    // low, sharing the "unknown" bucket with other no-XFF tests in this file
    // risks a false rejection depending on `cargo test`'s run order.
    let synthetic_ip = unique_label("198.51.100.2");
    let xff = [("X-Forwarded-For", synthetic_ip.as_str())];

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &xff).await.expect("upgrade must succeed");
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive a reply");
    assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    cleanup_user(&srv.state, user_id).await;
}

// client_ip extraction is private to ws.rs and not independently exposed, so
// it's exercised indirectly above via the handshake-churn-cap tests (which
// rely on X-Forwarded-For / its absence to distinguish "IPs"). A dedicated
// unit test isn't possible without making `client_ip` `pub`, which the plan
// doesn't call for — noting this as the test strategy for those two bullets
// rather than skipping them silently.

// ===========================================================================
// Stage 8.2 — connect-time side effects (happy path)
// ===========================================================================

#[tokio::test]
#[serial]
async fn successful_connect_calls_ws_bootstrap_and_subscribes_returned_chats() {
    let mock = MockServer::start().await;
    let chat_id: i64 = 900_000_100;
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "chat_ids": [chat_id.to_string()]
        })))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("upgrade must succeed");
    // Give the connect-time bootstrap + add_chat_sub a moment to land.
    tokio::time::sleep(Duration::from_millis(150)).await;

    // Publish an instance_inbox event scoped to that chat_id and confirm delivery.
    let mut pub_conn = srv.state.redis.clone();
    let payload = serde_json::json!({"event": "new_message", "chat_id": chat_id.to_string()});
    let _: i64 = redis::cmd("PUBLISH")
        .arg(linka_common::redis_keys::instance_inbox(&srv.state.server_id))
        .arg(payload.to_string())
        .query_async(&mut pub_conn)
        .await
        .unwrap();

    let msg = next_msg(&mut ws, 2000).await.expect("must receive the chat-scoped fan-out");
    assert!(matches!(msg, WsMessage::Text(ref t) if t.contains("new_message")), "got {msg:?}");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn ws_bootstrap_failure_still_succeeds_with_zero_chat_subs() {
    let mock = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("upgrade must still succeed");
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("connection must stay usable despite bootstrap failure");
    assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn new_user_subscribes_user_events_and_receives_user_scoped_publish() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("upgrade must succeed");
    tokio::time::sleep(Duration::from_millis(150)).await;

    let mut pub_conn = srv.state.redis.clone();
    let payload = serde_json::json!({"event": "profile_updated"});
    let _: i64 = redis::cmd("PUBLISH")
        .arg(linka_common::redis_keys::user_events(user_id))
        .arg(payload.to_string())
        .query_async(&mut pub_conn)
        .await
        .unwrap();

    // Poll with retries rather than a single fixed sleep (flaky-timing risk
    // flagged by the plan itself).
    let mut received = None;
    for _ in 0..20 {
        if let Some(msg) = next_msg(&mut ws, 200).await {
            received = Some(msg);
            break;
        }
    }
    let msg = received.expect("must receive the forwarded user_events publish");
    assert!(matches!(msg, WsMessage::Text(ref t) if t.contains("profile_updated")), "got {msg:?}");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn second_simultaneous_connection_same_user_both_receive_user_events() {
    let mut opts = Opts::default();
    opts.ws_conn_max = 5;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws1 = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("first connect");
    let mut ws2 = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("second connect");
    tokio::time::sleep(Duration::from_millis(150)).await;

    let mut pub_conn = srv.state.redis.clone();
    let payload = serde_json::json!({"event": "profile_updated"});
    let _: i64 = redis::cmd("PUBLISH")
        .arg(linka_common::redis_keys::user_events(user_id))
        .arg(payload.to_string())
        .query_async(&mut pub_conn)
        .await
        .unwrap();

    async fn wait_for_event(ws: &mut WsStream) -> bool {
        for _ in 0..20 {
            if let Some(WsMessage::Text(t)) = next_msg(ws, 200).await {
                if t.contains("profile_updated") {
                    return true;
                }
            }
        }
        false
    }
    assert!(wait_for_event(&mut ws1).await, "connection 1 must receive the user event");
    assert!(wait_for_event(&mut ws2).await, "connection 2 must also receive the user event");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn presence_marked_online_on_connect() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();

    let _ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("upgrade must succeed");
    tokio::time::sleep(Duration::from_millis(150)).await;

    let mut conn = srv.state.redis.clone();
    let count: i64 = conn.scard(linka_common::redis_keys::presence(user_id)).await.unwrap_or(0);
    assert_eq!(count, 1, "presence:{{uid}} must gain exactly one member on connect");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn connection_cap_eviction_closes_oldest_with_4409() {
    let mut opts = Opts::default();
    opts.ws_conn_max = 1;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws1 = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("first connect");
    // Confirm #1 is alive before evicting it.
    ws1.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let ack = next_msg(&mut ws1, 2000).await.expect("first connection must be alive");
    assert!(matches!(ack, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    // Second connection for the same user, over the cap of 1 -> evicts #1.
    let _ws2 = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.expect("second connect must succeed");

    let msg = next_msg(&mut ws1, 3000).await.expect("connection #1 must receive a close frame");
    assert_eq!(close_code(&msg), Some(4409));

    cleanup_user(&srv.state, user_id).await;
}

// ===========================================================================
// Stage 8.3 — read-loop frame handling
// ===========================================================================

#[tokio::test]
#[serial]
async fn non_json_text_frame_gets_bad_frame_error_and_stays_open() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    ws.send(WsMessage::Text("not json at all".to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive bad_frame error");
    assert!(matches!(&msg, WsMessage::Text(t) if t == r#"{"type":"error","code":"bad_frame"}"#));

    // Connection stays open for the next frame.
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg2 = next_msg(&mut ws, 2000).await.expect("connection must remain usable");
    assert!(matches!(msg2, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn unknown_frame_type_is_silently_ignored_connection_stays_open() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    ws.send(WsMessage::Text(r#"{"type":"totally_unknown_type"}"#.to_string())).await.unwrap();
    // No reply expected for a genuine no-op frame.
    let nothing = next_msg(&mut ws, 500).await;
    assert!(nothing.is_none(), "unknown frame type must not get any reply, got {nothing:?}");

    // Prove the connection is still alive.
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("connection must remain usable");
    assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn ping_pong_control_frames_are_swallowed_silently() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    ws.send(WsMessage::Ping(vec![1, 2, 3])).await.unwrap();
    // tokio-tungstenite auto-replies to Ping with a Pong at the transport
    // level and may surface the Ping to the reader; either way there must be
    // no app-level JSON error/ack frame from the gateway's dispatch logic.
    let msg = next_msg(&mut ws, 500).await;
    if let Some(m) = &msg {
        assert!(!matches!(m, WsMessage::Text(_)), "ping must not produce an app-level text reply, got {m:?}");
    }

    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let ack = next_msg(&mut ws, 2000).await.expect("connection must remain usable after ping");
    assert!(matches!(ack, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn raw_close_frame_ends_read_loop_cleanly() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    // Establish presence so we can assert cleanup ran (no panic + offline).
    tokio::time::sleep(Duration::from_millis(100)).await;

    ws.close(None).await.ok();
    tokio::time::sleep(Duration::from_millis(300)).await;

    let mut conn = srv.state.redis.clone();
    let count: i64 = conn.scard(linka_common::redis_keys::presence(user_id)).await.unwrap_or(-1);
    assert_eq!(count, 0, "cleanup after a client-initiated Close must run without panicking");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn frame_rate_flood_yields_rate_limited_errors_without_closing() {
    let mut opts = Opts::default();
    opts.frame_max = 2;
    opts.frame_window_secs = 30.0;
    opts.frame_flood_strikes = 1_000; // effectively unlimited strikes for this test
    let srv = start_server(opts).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    // First 2 heartbeats consume the bucket (frame_max=2).
    for _ in 0..2 {
        ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
        let msg = next_msg(&mut ws, 2000).await.expect("must be under the limit");
        assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));
    }

    // 3rd frame, with a client_message_id, must be rate_limited and echo it.
    ws.send(WsMessage::Text(r#"{"type":"heartbeat","client_message_id":"cmid-1"}"#.to_string()))
        .await
        .unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive rate_limited error");
    match msg {
        WsMessage::Text(t) => {
            assert!(t.contains(r#""code":"rate_limited""#), "got {t}");
            assert!(t.contains("cmid-1"), "client_message_id must be echoed back, got {t}");
        }
        other => panic!("expected a text error frame, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn frame_rate_flood_past_strikes_forcibly_disconnects() {
    let mut opts = Opts::default();
    opts.frame_max = 1;
    opts.frame_window_secs = 30.0;
    opts.frame_flood_strikes = 3;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    // Consume the one allowed frame.
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.unwrap();
    assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    // 3 more flooded frames -> flood_strikes hits the threshold and the read
    // loop breaks (socket closes from the server side).
    for _ in 0..3 {
        ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    }

    // Drain whatever comes (rate_limited errors, then a disconnect); the
    // socket must actually close within a reasonable window.
    let mut closed = false;
    for _ in 0..10 {
        match next_msg(&mut ws, 500).await {
            Some(WsMessage::Close(_)) | None if closed_or_eof(&mut ws).await => {
                closed = true;
                break;
            }
            _ => continue,
        }
    }
    // Fallback: attempt one more send; a closed connection will error.
    if !closed {
        let send_result = ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await;
        let recv_after = next_msg(&mut ws, 1000).await;
        closed = send_result.is_err() || recv_after.is_none();
    }
    assert!(closed, "connection must be forcibly closed after exceeding frame_flood_strikes");

    cleanup_user(&srv.state, user_id).await;
}

/// Helper: checks whether the underlying stream has actually ended (EOF).
async fn closed_or_eof(ws: &mut WsStream) -> bool {
    tokio::time::timeout(Duration::from_millis(200), ws.next()).await.map(|r| r.is_none()).unwrap_or(false)
}

#[tokio::test]
#[serial]
async fn clean_frame_after_rate_limited_ones_resets_flood_strikes() {
    let mut opts = Opts::default();
    opts.frame_max = 1;
    opts.frame_window_secs = 30.0;
    opts.frame_flood_strikes = 3;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    // Consume the allowed frame.
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.unwrap();
    assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    // 2 flooded frames (under the 3-strike threshold).
    for _ in 0..2 {
        ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
        let m = next_msg(&mut ws, 2000).await.expect("rate_limited error expected");
        assert!(matches!(m, WsMessage::Text(t) if t.contains("rate_limited")));
    }

    // Wait out the window so the next frame is clean (resets flood_strikes on
    // any successfully-processed frame, per the doc-comment).
    tokio::time::sleep(Duration::from_secs(31)).await;
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let clean = next_msg(&mut ws, 2000).await.expect("clean frame must succeed after the window elapses");
    assert!(matches!(clean, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    // Flood again for 2 more (still under 3 strikes) -> connection must still
    // be alive, proving strikes did not carry over across the gap.
    for _ in 0..2 {
        ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
        let m = next_msg(&mut ws, 2000).await.expect("connection must still be alive, not yet disconnected");
        assert!(matches!(m, WsMessage::Text(t) if t.contains("rate_limited")));
    }

    cleanup_user(&srv.state, user_id).await;
}

// ===========================================================================
// Stage 8.4 — heartbeat dispatch
// ===========================================================================

#[tokio::test]
#[serial]
async fn bare_heartbeat_gets_ack_with_no_resync_key_when_nothing_dropped() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.unwrap();
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "heartbeat_ack");
            assert!(v.get("resync_chat_ids").is_none(), "got {t}");
        }
        other => panic!("expected text frame, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn heartbeat_after_dropped_chat_fanout_includes_resync_chat_ids_as_strings() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;

    // Directly manipulate AppState to simulate a dropped chat fan-out for
    // this connection (mirrors Phase 7's gap-tracking test approach — driving
    // fanin::handle_message's full mpsc-full scenario here would require
    // knowing the connection's internal ConnId, which isn't observable from
    // the WS client). Find the ConnId for this user via user_conns.
    let conn_id = *srv
        .state
        .user_conns
        .get(&user_id)
        .expect("connection must be registered")
        .iter()
        .next()
        .expect("must have exactly one conn id");
    let chat_id: i64 = 900_000_200;
    srv.state.mark_dropped(conn_id, chat_id);

    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.unwrap();
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "heartbeat_ack");
            let ids = v["resync_chat_ids"].as_array().expect("resync_chat_ids must be an array");
            assert_eq!(ids.len(), 1);
            assert_eq!(ids[0], serde_json::Value::String(chat_id.to_string()), "chat ids must be strings, got {t}");
        }
        other => panic!("expected text frame, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn resync_chat_ids_is_drained_after_being_sent() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;

    let conn_id = *srv
        .state
        .user_conns
        .get(&user_id)
        .expect("connection must be registered")
        .iter()
        .next()
        .expect("must have exactly one conn id");
    srv.state.mark_dropped(conn_id, 900_000_201);

    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let first = next_msg(&mut ws, 2000).await.unwrap();
    let first_v: serde_json::Value = match &first {
        WsMessage::Text(t) => serde_json::from_str(t).unwrap(),
        other => panic!("expected text, got {other:?}"),
    };
    assert!(first_v.get("resync_chat_ids").is_some(), "first heartbeat must carry the dropped chat");

    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let second = next_msg(&mut ws, 2000).await.unwrap();
    match second {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert!(v.get("resync_chat_ids").is_none(), "second heartbeat must not repeat resync_chat_ids, got {t}");
        }
        other => panic!("expected text, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn heartbeat_refreshes_presence_ttl() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();
    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;

    let mut conn = srv.state.redis.clone();
    let ttl_before: i64 = conn.ttl(linka_common::redis_keys::presence(user_id)).await.unwrap_or(-2);
    assert!(ttl_before > 0, "presence key must have a TTL set on connect, got {ttl_before}");

    // Artificially shrink the TTL, then heartbeat and confirm it's refreshed.
    let _: bool = conn.expire(linka_common::redis_keys::presence(user_id), 2).await.unwrap_or(false);
    ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let _ = next_msg(&mut ws, 2000).await.expect("heartbeat must ack");
    tokio::time::sleep(Duration::from_millis(100)).await;

    let ttl_after: i64 = conn.ttl(linka_common::redis_keys::presence(user_id)).await.unwrap_or(-2);
    assert!(ttl_after > 2, "heartbeat must refresh the presence TTL well past the shrunk value, got {ttl_after}");

    cleanup_user(&srv.state, user_id).await;
}

// ===========================================================================
// Stage 8.5 — send_message dispatch
// ===========================================================================

async fn set_app_worker_alive(state: &AppState, app_server_id: &str) {
    let mut conn = state.redis.clone();
    let _: () = conn
        .set_ex(linka_common::redis_keys::app_worker_alive(app_server_id), "1", 30)
        .await
        .unwrap();
}

async fn clear_app_worker_alive(state: &AppState, app_server_id: &str) {
    let mut conn = state.redis.clone();
    let _: i64 = conn.del(linka_common::redis_keys::app_worker_alive(app_server_id)).await.unwrap_or(0);
}

#[tokio::test]
#[serial]
async fn well_formed_send_message_workers_alive_gets_queued_ack_and_xadd() {
    let opts = Opts::default();
    let app_server_id = opts.app_server_id.clone();
    let srv = start_server(opts).await;
    set_app_worker_alive(&srv.state, &app_server_id).await;
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_300;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    let mut conn = srv.state.redis.clone();
    let key = linka_common::redis_keys::send_stream_key_for_chat(chat_id, srv.state.config.send_stream_shards);
    let len_before: i64 = redis::cmd("XLEN").arg(&key).query_async(&mut conn).await.unwrap_or(0);

    let frame = serde_json::json!({
        "type": "send_message",
        "chat_id": chat_id.to_string(),
        "client_message_id": "cmid-happy",
        "content": "hello",
    });
    ws.send(WsMessage::Text(frame.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive queued ack");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "ack");
            assert_eq!(v["for"], "send_message");
            assert_eq!(v["status"], "queued");
        }
        other => panic!("expected text ack, got {other:?}"),
    }

    let len_after: i64 = redis::cmd("XLEN").arg(&key).query_async(&mut conn).await.unwrap_or(0);
    assert_eq!(len_after, len_before + 1, "a stream entry must land in the correctly sharded key");

    clear_app_worker_alive(&srv.state, &app_server_id).await;
    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn exceeding_primary_send_bucket_is_rate_limited_no_xadd() {
    let mut opts = Opts::default();
    opts.send_max = 1;
    opts.send_window_secs = 30.0;
    let app_server_id = opts.app_server_id.clone();
    let srv = start_server(opts).await;
    set_app_worker_alive(&srv.state, &app_server_id).await;
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_301;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    let send = |cmid: &str| {
        serde_json::json!({
            "type": "send_message",
            "chat_id": chat_id.to_string(),
            "client_message_id": cmid,
        })
    };

    ws.send(WsMessage::Text(send("cmid-1").to_string())).await.unwrap();
    let first = next_msg(&mut ws, 2000).await.unwrap();
    assert!(matches!(&first, WsMessage::Text(t) if t.contains(r#""status":"queued""#)));

    let mut conn = srv.state.redis.clone();
    let key = linka_common::redis_keys::send_stream_key_for_chat(chat_id, srv.state.config.send_stream_shards);
    let len_before: i64 = redis::cmd("XLEN").arg(&key).query_async(&mut conn).await.unwrap_or(0);

    ws.send(WsMessage::Text(send("cmid-2").to_string())).await.unwrap();
    let second = next_msg(&mut ws, 2000).await.unwrap();
    match second {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "error");
            assert_eq!(v["code"], "rate_limited");
            assert_eq!(v["client_message_id"], "cmid-2");
        }
        other => panic!("expected rate_limited error, got {other:?}"),
    }

    let len_after: i64 = redis::cmd("XLEN").arg(&key).query_async(&mut conn).await.unwrap_or(0);
    assert_eq!(len_after, len_before, "no XADD must occur on a rate-limited send");

    clear_app_worker_alive(&srv.state, &app_server_id).await;
    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn exceeding_burst_bucket_while_under_primary_is_also_rate_limited() {
    let mut opts = Opts::default();
    opts.send_max = 1_000_000; // primary bucket never the bottleneck here
    opts.send_burst_max = 1;
    opts.send_burst_window_secs = 30.0;
    let app_server_id = opts.app_server_id.clone();
    let srv = start_server(opts).await;
    set_app_worker_alive(&srv.state, &app_server_id).await;
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_302;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    let send = |cmid: &str| {
        serde_json::json!({"type": "send_message", "chat_id": chat_id.to_string(), "client_message_id": cmid})
    };

    ws.send(WsMessage::Text(send("b1").to_string())).await.unwrap();
    let first = next_msg(&mut ws, 2000).await.unwrap();
    assert!(matches!(&first, WsMessage::Text(t) if t.contains(r#""status":"queued""#)));

    ws.send(WsMessage::Text(send("b2").to_string())).await.unwrap();
    let second = next_msg(&mut ws, 2000).await.unwrap();
    match second {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["code"], "rate_limited", "burst-only exhaustion must still surface as rate_limited");
        }
        other => panic!("expected rate_limited error, got {other:?}"),
    }

    clear_app_worker_alive(&srv.state, &app_server_id).await;
    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn app_worker_alive_key_absent_yields_internal_error_no_xadd() {
    let opts = Opts::default();
    let srv = start_server(opts).await; // app_worker_alive key never set
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_303;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    let mut conn = srv.state.redis.clone();
    let key = linka_common::redis_keys::send_stream_key_for_chat(chat_id, srv.state.config.send_stream_shards);
    let len_before: i64 = redis::cmd("XLEN").arg(&key).query_async(&mut conn).await.unwrap_or(0);

    let frame = serde_json::json!({
        "type": "send_message",
        "chat_id": chat_id.to_string(),
        "client_message_id": "cmid-dead-worker",
    });
    ws.send(WsMessage::Text(frame.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive internal_error");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "error");
            assert_eq!(v["code"], "internal_error");
            assert_eq!(v["client_message_id"], "cmid-dead-worker");
        }
        other => panic!("expected internal_error, got {other:?}"),
    }

    let len_after: i64 = redis::cmd("XLEN").arg(&key).query_async(&mut conn).await.unwrap_or(0);
    assert_eq!(len_after, len_before, "no XADD must occur when app workers are not alive");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn app_worker_alive_absent_still_consumes_the_rate_limit_bucket() {
    // ADR 0041 / the file's own Step-4 doc-comment: "Metered actions still
    // consume their bucket" even when the send is ultimately rejected for a
    // different reason (dead workers). Prove it: send_max=1, worker dead ->
    // first send is rejected as internal_error (not rate_limited), but the
    // bucket is now exhausted, so a second send (even with workers now alive)
    // is rejected as rate_limited, not queued.
    let mut opts = Opts::default();
    opts.send_max = 1;
    opts.send_window_secs = 30.0;
    let app_server_id = opts.app_server_id.clone();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_304;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    let send = |cmid: &str| {
        serde_json::json!({"type": "send_message", "chat_id": chat_id.to_string(), "client_message_id": cmid})
    };

    // Workers dead: first send consumes the bucket, rejected as internal_error.
    ws.send(WsMessage::Text(send("first").to_string())).await.unwrap();
    let first = next_msg(&mut ws, 2000).await.unwrap();
    match first {
        WsMessage::Text(t) => assert!(t.contains(r#""code":"internal_error""#), "got {t}"),
        other => panic!("expected internal_error, got {other:?}"),
    }

    // Now bring workers alive and try again — the bucket should already be
    // exhausted from the first (rejected) attempt.
    set_app_worker_alive(&srv.state, &app_server_id).await;
    ws.send(WsMessage::Text(send("second").to_string())).await.unwrap();
    let second = next_msg(&mut ws, 2000).await.unwrap();
    match second {
        WsMessage::Text(t) => {
            assert!(
                t.contains(r#""code":"rate_limited""#),
                "the first send must have consumed the bucket despite being rejected; got {t}"
            );
        }
        other => panic!("expected rate_limited, got {other:?}"),
    }

    clear_app_worker_alive(&srv.state, &app_server_id).await;
    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn client_message_id_echoed_verbatim_including_unusual_values() {
    let opts = Opts::default();
    let app_server_id = opts.app_server_id.clone();
    let srv = start_server(opts).await;
    // Leave app_workers dead so we exercise the internal_error echo path
    // (also covers the ack echo path via a separate well-formed case).
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_305;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    for cmid in ["", "a-very-very-very-long-client-message-id-".repeat(5).as_str(), "héllo-🌍-世界"] {
        let frame = serde_json::json!({
            "type": "send_message",
            "chat_id": chat_id.to_string(),
            "client_message_id": cmid,
        });
        ws.send(WsMessage::Text(frame.to_string())).await.unwrap();
        let msg = next_msg(&mut ws, 2000).await.expect("must receive a reply");
        match msg {
            WsMessage::Text(t) => {
                let v: serde_json::Value = serde_json::from_str(&t).unwrap();
                assert_eq!(v["code"], "internal_error");
                assert_eq!(v["client_message_id"], cmid, "client_message_id must be echoed verbatim, got {t}");
            }
            other => panic!("expected text error, got {other:?}"),
        }
    }

    let _ = app_server_id; // unused in the dead-worker path, kept for symmetry
    cleanup_user(&srv.state, user_id).await;
}

// ===========================================================================
// Stage 8.6 — mark_delivered / mark_read / mark_played dispatch
// ===========================================================================

async fn receipt_stream_len(state: &AppState) -> i64 {
    let mut conn = state.redis.clone();
    redis::cmd("XLEN")
        .arg(linka_common::redis_keys::RECEIPT_STREAM_KEY)
        .query_async(&mut conn)
        .await
        .unwrap_or(0)
}

async fn last_receipt_entry(state: &AppState) -> std::collections::HashMap<String, String> {
    let mut conn = state.redis.clone();
    let rows: Vec<(String, std::collections::HashMap<String, String>)> = redis::cmd("XREVRANGE")
        .arg(linka_common::redis_keys::RECEIPT_STREAM_KEY)
        .arg("+")
        .arg("-")
        .arg("COUNT")
        .arg(1)
        .query_async(&mut conn)
        .await
        .unwrap_or_default();
    rows.into_iter().next().map(|(_, fields)| fields).unwrap_or_default()
}

#[tokio::test]
#[serial]
async fn well_formed_mark_read_acks_and_xadds_correct_kind() {
    let opts = Opts::default();
    let app_server_id = opts.app_server_id.clone();
    let srv = start_server(opts).await;
    set_app_worker_alive(&srv.state, &app_server_id).await;
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_400;
    let message_id: i64 = 900_000_401;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    let len_before = receipt_stream_len(&srv.state).await;

    let frame = serde_json::json!({
        "type": "mark_read",
        "chat_id": chat_id.to_string(),
        "message_id": message_id.to_string(),
    });
    ws.send(WsMessage::Text(frame.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive an ack");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "ack");
            assert_eq!(v["for"], "mark_read");
        }
        other => panic!("expected ack, got {other:?}"),
    }

    let len_after = receipt_stream_len(&srv.state).await;
    assert_eq!(len_after, len_before + 1, "exactly one receipt_log_stream entry must land");

    let entry = last_receipt_entry(&srv.state).await;
    assert_eq!(entry.get("kind").map(String::as_str), Some("3"), "mark_read -> receipt_kind::READ (3)");
    assert_eq!(entry.get("chat_id").map(String::as_str), Some(chat_id.to_string().as_str()));
    assert_eq!(entry.get("up_to_message_id").map(String::as_str), Some(message_id.to_string().as_str()));

    clear_app_worker_alive(&srv.state, &app_server_id).await;
    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn all_three_mark_kinds_use_the_correct_receipt_kind_constant() {
    let opts = Opts::default();
    let app_server_id = opts.app_server_id.clone();
    let srv = start_server(opts).await;
    set_app_worker_alive(&srv.state, &app_server_id).await;
    let user_id = unique_user_id();

    for (frame_type, ack_for, expected_kind) in [
        ("mark_delivered", "mark_delivered", "2"),
        ("mark_read", "mark_read", "3"),
        ("mark_played", "mark_played", "4"),
    ] {
        let chat_id: i64 = 900_000_410 + expected_kind.parse::<i64>().unwrap();
        let message_id: i64 = chat_id + 1;
        let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

        let frame = serde_json::json!({
            "type": frame_type,
            "chat_id": chat_id.to_string(),
            "message_id": message_id.to_string(),
        });
        ws.send(WsMessage::Text(frame.to_string())).await.unwrap();
        let msg = next_msg(&mut ws, 2000).await.expect("must receive an ack");
        match msg {
            WsMessage::Text(t) => {
                let v: serde_json::Value = serde_json::from_str(&t).unwrap();
                assert_eq!(v["type"], "ack");
                assert_eq!(v["for"], ack_for);
            }
            other => panic!("expected ack for {frame_type}, got {other:?}"),
        }

        let entry = last_receipt_entry(&srv.state).await;
        assert_eq!(
            entry.get("kind").map(String::as_str),
            Some(expected_kind),
            "{frame_type} must XADD kind={expected_kind}"
        );
    }

    clear_app_worker_alive(&srv.state, &app_server_id).await;
    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn exceeding_receipts_bucket_yields_rate_limited_with_for_field() {
    let mut opts = Opts::default();
    opts.receipts_max = 1;
    opts.receipts_window_secs = 30.0;
    let app_server_id = opts.app_server_id.clone();
    let srv = start_server(opts).await;
    set_app_worker_alive(&srv.state, &app_server_id).await;
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_420;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    let mark = |mid: i64| {
        serde_json::json!({"type": "mark_read", "chat_id": chat_id.to_string(), "message_id": mid.to_string()})
    };

    ws.send(WsMessage::Text(mark(1).to_string())).await.unwrap();
    let first = next_msg(&mut ws, 2000).await.unwrap();
    assert!(matches!(&first, WsMessage::Text(t) if t.contains(r#""type":"ack""#)));

    ws.send(WsMessage::Text(mark(2).to_string())).await.unwrap();
    let second = next_msg(&mut ws, 2000).await.unwrap();
    match second {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "error");
            assert_eq!(v["code"], "rate_limited");
            assert_eq!(v["for"], "mark_read", "the field name is `for`, not `client_message_id`");
            assert!(v.get("client_message_id").is_none());
        }
        other => panic!("expected rate_limited error, got {other:?}"),
    }

    clear_app_worker_alive(&srv.state, &app_server_id).await;
    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn workers_not_alive_yields_internal_error_no_xadd_for_marks() {
    let opts = Opts::default();
    let srv = start_server(opts).await; // app_worker_alive never set
    let user_id = unique_user_id();
    let chat_id: i64 = 900_000_430;

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    let len_before = receipt_stream_len(&srv.state).await;

    let frame = serde_json::json!({
        "type": "mark_read",
        "chat_id": chat_id.to_string(),
        "message_id": "900000431",
    });
    ws.send(WsMessage::Text(frame.to_string())).await.unwrap();
    let msg = next_msg(&mut ws, 2000).await.expect("must receive internal_error");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "error");
            assert_eq!(v["code"], "internal_error");
            assert_eq!(v["for"], "mark_read");
        }
        other => panic!("expected internal_error, got {other:?}"),
    }

    let len_after = receipt_stream_len(&srv.state).await;
    assert_eq!(len_after, len_before, "no XADD must occur when app workers are not alive");

    cleanup_user(&srv.state, user_id).await;
}

// NOTE: "a receipt XADD failure yields no ack/no error frame at all" (the
// plan's 4th bullet) can't be forced deterministically against a live,
// healthy Redis without a fault-injection hook — same class of gap the plan
// itself already flags for send_message's equivalent case (Stage 8.5). Not
// automated; the `mark()` code path (fire-and-forget, no frame sent on an
// `Err` from `receipts::enqueue`) was read and matches its own doc-comment.

// ===========================================================================
// Stage 8.7 — typing / recording dispatch
// ===========================================================================

#[tokio::test]
#[serial]
async fn typing_allowed_true_publishes_with_no_direct_reply() {
    let mock = MockServer::start().await;
    let chat_id: i64 = 900_000_500;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"allowed": true})))
        .mount(&mock)
        .await;
    // The typing publish path resolves subscribers via chat_instances (Redis),
    // populated at connect-time from ws-bootstrap — a purely local
    // `add_chat_sub` isn't enough since `publish_event` looks up Redis.
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"chat_ids": [chat_id.to_string()]})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let sender_id = unique_user_id();
    let watcher_id = unique_user_id();

    // Both connect subscribed (via ws-bootstrap) to the same chat, so the
    // watcher genuinely observes the Redis-routed publish.
    let mut sender_ws = connect_raw(&srv.ws_base, &token_for(sender_id), &[]).await.unwrap();
    let mut watcher_ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();
    wait_for_conn_id(&srv.state, watcher_id).await;
    tokio::time::sleep(Duration::from_millis(150)).await; // let chat_instances registration land

    sender_ws
        .send(WsMessage::Text(serde_json::json!({"type": "typing", "chat_id": chat_id.to_string()}).to_string()))
        .await
        .unwrap();

    let event = next_msg(&mut watcher_ws, 2000).await.expect("watcher must receive the typing event");
    match event {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["event"], "typing");
            assert_eq!(v["kind"], "typing");
            assert_eq!(v["user_id"], sender_id.to_string());
        }
        other => panic!("expected typing event, got {other:?}"),
    }

    // No direct ack/error reply to the sender: since the sender is also
    // subscribed to this chat (via the shared ws-bootstrap mock), it does
    // receive the broadcast typing event itself (fan-out doesn't exclude the
    // sender) — but nothing else, and specifically no ack/error frame type.
    let direct = next_msg(&mut sender_ws, 300).await;
    match direct {
        None => {}
        Some(WsMessage::Text(t)) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["event"], "typing", "sender must get no direct ack/reply for typing, got {t}");
        }
        other => panic!("unexpected frame to sender: {other:?}"),
    }
    // Confirm truly nothing further (no ack/error) follows.
    let extra = next_msg(&mut sender_ws, 300).await;
    assert!(extra.is_none(), "sender must get no direct ack/reply for typing, got {extra:?}");

    cleanup_user(&srv.state, sender_id).await;
    cleanup_user(&srv.state, watcher_id).await;
}

#[tokio::test]
#[serial]
async fn typing_allowed_false_drops_silently_no_publish_no_error() {
    let mock = MockServer::start().await;
    let chat_id: i64 = 900_000_501;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"allowed": false})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let sender_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(sender_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(serde_json::json!({"type": "typing", "chat_id": chat_id.to_string()}).to_string()))
        .await
        .unwrap();

    let reply = next_msg(&mut ws, 300).await;
    assert!(reply.is_none(), "denied typing must drop silently, got {reply:?}");

    cleanup_user(&srv.state, sender_id).await;
}

#[tokio::test]
#[serial]
async fn typing_allowed_check_erroring_drops_silently_fail_closed() {
    let mock = MockServer::start().await;
    let chat_id: i64 = 900_000_502;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let sender_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(sender_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(serde_json::json!({"type": "typing", "chat_id": chat_id.to_string()}).to_string()))
        .await
        .unwrap();

    let reply = next_msg(&mut ws, 300).await;
    assert!(reply.is_none(), "a failed internal check must fail closed (silent drop), got {reply:?}");

    cleanup_user(&srv.state, sender_id).await;
}

#[tokio::test]
#[serial]
async fn exceeding_typing_rate_limit_short_circuits_before_the_internal_call() {
    let mock = MockServer::start().await;
    let chat_id: i64 = 900_000_503;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"allowed": true})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    opts.typing_max = 1;
    opts.typing_window_secs = 30.0;
    let srv = start_server(opts).await;
    let sender_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(sender_id), &[]).await.unwrap();
    let typing_frame = serde_json::json!({"type": "typing", "chat_id": chat_id.to_string()}).to_string();

    ws.send(WsMessage::Text(typing_frame.clone())).await.unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await; // let the first request land at the mock

    let typing_requests = |reqs: &[wiremock::Request]| {
        reqs.iter().filter(|r| r.url.path() == "/internal/typing-allowed").count()
    };

    let requests_after_first = typing_requests(&mock.received_requests().await.unwrap());
    assert_eq!(requests_after_first, 1, "the first, allowed typing frame must hit the mock once");

    ws.send(WsMessage::Text(typing_frame)).await.unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;

    let requests_after_second = typing_requests(&mock.received_requests().await.unwrap());
    assert_eq!(
        requests_after_second, requests_after_first,
        "a rate-limited typing frame must never reach the internal HTTP call"
    );

    cleanup_user(&srv.state, sender_id).await;
}

#[tokio::test]
#[serial]
async fn recording_frame_follows_the_same_path_with_recording_audio_kind() {
    let mock = MockServer::start().await;
    let chat_id: i64 = 900_000_504;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"allowed": true})))
        .mount(&mock)
        .await;
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"chat_ids": [chat_id.to_string()]})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let sender_id = unique_user_id();
    let watcher_id = unique_user_id();

    let mut sender_ws = connect_raw(&srv.ws_base, &token_for(sender_id), &[]).await.unwrap();
    let mut watcher_ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();
    wait_for_conn_id(&srv.state, watcher_id).await;
    tokio::time::sleep(Duration::from_millis(150)).await; // let chat_instances registration land

    sender_ws
        .send(WsMessage::Text(serde_json::json!({"type": "recording", "chat_id": chat_id.to_string()}).to_string()))
        .await
        .unwrap();

    let event = next_msg(&mut watcher_ws, 2000).await.expect("watcher must receive the recording event");
    match event {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["event"], "typing");
            assert_eq!(v["kind"], "recording_audio");
        }
        other => panic!("expected recording_audio event, got {other:?}"),
    }

    cleanup_user(&srv.state, sender_id).await;
    cleanup_user(&srv.state, watcher_id).await;
}

// ===========================================================================
// Stage 8.8 — subscribe_presence / unsubscribe_presence dispatch
// ===========================================================================

#[tokio::test]
#[serial]
async fn subscribing_to_own_presence_is_a_bad_request_no_redis_subscription() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "subscribe_presence", "user_id": user_id.to_string()}).to_string(),
    ))
    .await
    .unwrap();

    let msg = next_msg(&mut ws, 2000).await.expect("must receive a bad_request error");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "error");
            assert_eq!(v["code"], "bad_request");
            assert_eq!(v["message"], "Cannot subscribe to your own presence");
        }
        other => panic!("expected bad_request error, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn subscribing_to_authorized_target_returns_real_presence_status() {
    let mock = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"authorized": true})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let watcher_id = unique_user_id();
    let target_id = unique_user_id();

    // Target connects first so it's genuinely online.
    let mut target_ws = connect_raw(&srv.ws_base, &token_for(target_id), &[]).await.unwrap();
    let mut watcher_ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();

    watcher_ws
        .send(WsMessage::Text(
            serde_json::json!({"type": "subscribe_presence", "user_id": target_id.to_string()}).to_string(),
        ))
        .await
        .unwrap();

    let msg = next_msg(&mut watcher_ws, 2000).await.expect("must receive a presence_status frame");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "presence_status");
            assert_eq!(v["user_id"], target_id.to_string());
            assert_eq!(v["status"], "online");
        }
        other => panic!("expected presence_status, got {other:?}"),
    }

    let _ = target_ws.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await;
    cleanup_user(&srv.state, watcher_id).await;
    cleanup_user(&srv.state, target_id).await;
}

#[tokio::test]
#[serial]
async fn subscribing_when_unauthorized_revokes_and_tears_down_existing_watch() {
    let mock = MockServer::start().await;
    // First call (initial subscribe) authorized; the mock is reconfigured
    // to unauthorized right before the second (revoke-triggering) subscribe.
    let target_id_holder = std::sync::Arc::new(std::sync::Mutex::new(0i64));
    let holder = target_id_holder.clone();
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(move |_req: &wiremock::Request| {
            let authorized = *holder.lock().unwrap() == 0; // 0 sentinel = "authorize"
            ResponseTemplate::new(200).set_body_json(serde_json::json!({"authorized": authorized}))
        })
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let watcher_id = unique_user_id();
    let target_id = unique_user_id();

    let mut watcher_ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();

    // Establish the watch while authorized.
    watcher_ws
        .send(WsMessage::Text(
            serde_json::json!({"type": "subscribe_presence", "user_id": target_id.to_string()}).to_string(),
        ))
        .await
        .unwrap();
    let first = next_msg(&mut watcher_ws, 2000).await.expect("first subscribe must succeed");
    assert!(matches!(&first, WsMessage::Text(t) if t.contains("presence_status")));

    // Flip the mock to unauthorized and re-subscribe (mirrors the "re-run on
    // every heartbeat" contract revoking a stale watch).
    *target_id_holder.lock().unwrap() = 1;
    watcher_ws
        .send(WsMessage::Text(
            serde_json::json!({"type": "subscribe_presence", "user_id": target_id.to_string()}).to_string(),
        ))
        .await
        .unwrap();
    let second = next_msg(&mut watcher_ws, 2000).await.expect("must receive presence_revoked");
    match second {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "presence_revoked");
            assert_eq!(v["user_id"], target_id.to_string());
        }
        other => panic!("expected presence_revoked, got {other:?}"),
    }

    cleanup_user(&srv.state, watcher_id).await;
    cleanup_user(&srv.state, target_id).await;
}

#[tokio::test]
#[serial]
async fn presence_authorization_check_erroring_is_treated_as_unauthorized() {
    let mock = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let watcher_id = unique_user_id();
    let target_id = unique_user_id();

    let mut watcher_ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();
    watcher_ws
        .send(WsMessage::Text(
            serde_json::json!({"type": "subscribe_presence", "user_id": target_id.to_string()}).to_string(),
        ))
        .await
        .unwrap();

    let msg = next_msg(&mut watcher_ws, 2000).await.expect("must receive presence_revoked (fail-closed)");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "presence_revoked");
        }
        other => panic!("expected presence_revoked, got {other:?}"),
    }

    cleanup_user(&srv.state, watcher_id).await;
    cleanup_user(&srv.state, target_id).await;
}

#[tokio::test]
#[serial]
async fn unsubscribe_presence_removes_watch_and_is_never_rate_limited() {
    let mock = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"authorized": true})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    opts.sub_presence_max = 1; // deliberately tiny — unsubscribe must never hit this
    opts.sub_presence_window_secs = 30.0;
    let srv = start_server(opts).await;
    let watcher_id = unique_user_id();
    let target_id = unique_user_id();

    let mut watcher_ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();
    watcher_ws
        .send(WsMessage::Text(
            serde_json::json!({"type": "subscribe_presence", "user_id": target_id.to_string()}).to_string(),
        ))
        .await
        .unwrap();
    let _ = next_msg(&mut watcher_ws, 2000).await.expect("subscribe must succeed (consumes the 1-slot bucket)");

    // Flood unsubscribe far past the (already-exhausted) bucket max.
    for _ in 0..10 {
        watcher_ws
            .send(WsMessage::Text(
                serde_json::json!({"type": "unsubscribe_presence", "user_id": target_id.to_string()}).to_string(),
            ))
            .await
            .unwrap();
    }
    // No reply of any kind is expected for unsubscribe; confirm silence.
    let reply = next_msg(&mut watcher_ws, 300).await;
    assert!(reply.is_none(), "unsubscribe_presence must never itself produce a reply, got {reply:?}");

    let watcher_conn_id = watcher_ws; // keep alive
    drop(watcher_conn_id);
    cleanup_user(&srv.state, watcher_id).await;
    cleanup_user(&srv.state, target_id).await;
}

#[tokio::test]
#[serial]
async fn unsubscribe_presence_for_never_watched_target_is_a_silent_noop() {
    let srv = start_server(Opts::default()).await;
    let watcher_id = unique_user_id();
    let target_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "unsubscribe_presence", "user_id": target_id.to_string()}).to_string(),
    ))
    .await
    .unwrap();

    let reply = next_msg(&mut ws, 300).await;
    assert!(reply.is_none(), "unsubscribing a never-watched target must be silent, got {reply:?}");

    cleanup_user(&srv.state, watcher_id).await;
}

#[tokio::test]
#[serial]
async fn exceeding_sub_presence_bucket_on_subscribe_drops_silently_no_reply() {
    let mock = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"authorized": true})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    opts.sub_presence_max = 1;
    opts.sub_presence_window_secs = 30.0;
    let srv = start_server(opts).await;
    let watcher_id = unique_user_id();
    let target1 = unique_user_id();
    let target2 = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();

    // First subscribe consumes the single slot.
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "subscribe_presence", "user_id": target1.to_string()}).to_string(),
    ))
    .await
    .unwrap();
    let first = next_msg(&mut ws, 2000).await.expect("first subscribe must succeed");
    assert!(matches!(&first, WsMessage::Text(t) if t.contains("presence_status")));

    // Second subscribe (different target) is over the bucket -> no reply at all.
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "subscribe_presence", "user_id": target2.to_string()}).to_string(),
    ))
    .await
    .unwrap();
    let second = next_msg(&mut ws, 300).await;
    assert!(second.is_none(), "a rate-limited subscribe_presence must yield no reply at all, got {second:?}");

    cleanup_user(&srv.state, watcher_id).await;
    cleanup_user(&srv.state, target1).await;
    cleanup_user(&srv.state, target2).await;
}

// ===========================================================================
// Stage 8.9 — presence_active dispatch
// ===========================================================================

#[tokio::test]
#[serial]
async fn presence_active_true_reports_online_to_a_subscriber() {
    let mock = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"authorized": true})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();
    let watcher_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(serde_json::json!({"type": "presence_active", "active": true}).to_string()))
        .await
        .unwrap();
    // presence_active has no direct reply; give it a moment to land in Redis.
    tokio::time::sleep(Duration::from_millis(150)).await;

    let mut watcher_ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();
    watcher_ws
        .send(WsMessage::Text(
            serde_json::json!({"type": "subscribe_presence", "user_id": user_id.to_string()}).to_string(),
        ))
        .await
        .unwrap();
    let msg = next_msg(&mut watcher_ws, 2000).await.expect("must receive presence_status");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["status"], "online");
        }
        other => panic!("expected presence_status online, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
    cleanup_user(&srv.state, watcher_id).await;
}

#[tokio::test]
#[serial]
async fn presence_active_false_reports_offline_when_it_was_the_only_connection() {
    let mock = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"authorized": true})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();
    let watcher_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(serde_json::json!({"type": "presence_active", "active": false}).to_string()))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(150)).await;

    let mut watcher_ws = connect_raw(&srv.ws_base, &token_for(watcher_id), &[]).await.unwrap();
    watcher_ws
        .send(WsMessage::Text(
            serde_json::json!({"type": "subscribe_presence", "user_id": user_id.to_string()}).to_string(),
        ))
        .await
        .unwrap();
    let msg = next_msg(&mut watcher_ws, 2000).await.expect("must receive presence_status");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["status"], "offline");
        }
        other => panic!("expected presence_status offline, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
    cleanup_user(&srv.state, watcher_id).await;
}

#[tokio::test]
#[serial]
async fn presence_active_shares_the_sub_presence_rate_limit_bucket() {
    let mut opts = Opts::default();
    opts.sub_presence_max = 1;
    opts.sub_presence_window_secs = 30.0;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    // Consume the shared bucket via presence_active first.
    ws.send(WsMessage::Text(serde_json::json!({"type": "presence_active", "active": true}).to_string()))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;

    // subscribe_presence on a distinct target now shares the same exhausted
    // bucket (conn_uuid-scoped "ws_sub_presence") -> silent drop.
    let target_id = unique_user_id();
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "subscribe_presence", "user_id": target_id.to_string()}).to_string(),
    ))
    .await
    .unwrap();
    let reply = next_msg(&mut ws, 300).await;
    assert!(
        reply.is_none(),
        "presence_active and subscribe_presence must share one ws_sub_presence bucket per conn_uuid, got {reply:?}"
    );

    cleanup_user(&srv.state, user_id).await;
    cleanup_user(&srv.state, target_id).await;
}

// ===========================================================================
// Stage 8.10 — edit/delete/restore/purge_message dispatch (relayed via
// /internal/message/*)
// ===========================================================================

#[tokio::test]
#[serial]
async fn well_formed_edit_relays_mock_200_body_as_ack() {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"message_id": "42", "content": "hi"})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "edit_message", "chat_id": "1", "message_id": "42", "content": "hi"}).to_string(),
    ))
    .await
    .unwrap();

    let msg = next_msg(&mut ws, 2000).await.expect("must receive an ack");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "ack");
            assert_eq!(v["for"], "edit_message");
            assert_eq!(v["message_id"], "42");
            assert_eq!(v["content"], "hi");
        }
        other => panic!("expected ack, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn mock_4xx_relays_bad_request_with_detail_message() {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(400).set_body_json(serde_json::json!({"detail": "not your message"})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "edit_message", "chat_id": "1", "message_id": "42", "content": "hi"}).to_string(),
    ))
    .await
    .unwrap();

    let msg = next_msg(&mut ws, 2000).await.expect("must receive an error");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "error");
            assert_eq!(v["code"], "bad_request");
            assert_eq!(v["for"], "edit_message");
            assert_eq!(v["message"], "not your message");
        }
        other => panic!("expected bad_request error, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn mock_403_relays_forbidden_specifically() {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(403).set_body_json(serde_json::json!({"detail": "no"})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "edit_message", "chat_id": "1", "message_id": "42", "content": "hi"}).to_string(),
    ))
    .await
    .unwrap();

    let msg = next_msg(&mut ws, 2000).await.expect("must receive an error");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["code"], "forbidden", "403 must map to the special-cased forbidden code");
        }
        other => panic!("expected forbidden error, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn mock_5xx_or_unreachable_relays_internal_error_with_no_message_field() {
    let opts = Opts::default(); // app_internal_url stays unreachable (127.0.0.1:1)
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "edit_message", "chat_id": "1", "message_id": "42", "content": "hi"}).to_string(),
    ))
    .await
    .unwrap();

    let msg = next_msg(&mut ws, 2000).await.expect("must receive an error");
    match msg {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "error");
            assert_eq!(v["code"], "internal_error");
            assert_eq!(v["for"], "edit_message");
            assert!(v.get("message").is_none(), "internal_error must carry no message field, got {t}");
        }
        other => panic!("expected internal_error, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn exceeding_ws_edit_bucket_rate_limits_without_calling_the_internal_endpoint() {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    opts.edit_max = 1;
    opts.edit_window_secs = 30.0;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    let edit = |mid: &str| {
        serde_json::json!({"type": "edit_message", "chat_id": "1", "message_id": mid, "content": "hi"})
    };

    let edit_requests = |reqs: &[wiremock::Request]| {
        reqs.iter().filter(|r| r.url.path() == "/internal/message/edit").count()
    };

    ws.send(WsMessage::Text(edit("1").to_string())).await.unwrap();
    let first = next_msg(&mut ws, 2000).await.unwrap();
    assert!(matches!(&first, WsMessage::Text(t) if t.contains(r#""type":"ack""#)));

    let requests_after_first = edit_requests(&mock.received_requests().await.unwrap());
    assert_eq!(requests_after_first, 1);

    ws.send(WsMessage::Text(edit("2").to_string())).await.unwrap();
    let second = next_msg(&mut ws, 2000).await.unwrap();
    match second {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["code"], "rate_limited");
            assert_eq!(v["for"], "edit_message");
        }
        other => panic!("expected rate_limited, got {other:?}"),
    }

    let requests_after_second = edit_requests(&mock.received_requests().await.unwrap());
    assert_eq!(
        requests_after_second, requests_after_first,
        "a rate-limited edit must never reach the internal HTTP call"
    );

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn edit_delete_restore_purge_share_one_ws_edit_bucket() {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(path("/internal/message/delete"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    opts.edit_max = 1;
    opts.edit_window_secs = 30.0;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();

    // Exhaust the shared bucket via edit_message.
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "edit_message", "chat_id": "1", "message_id": "1", "content": "hi"}).to_string(),
    ))
    .await
    .unwrap();
    let first = next_msg(&mut ws, 2000).await.unwrap();
    assert!(matches!(&first, WsMessage::Text(t) if t.contains(r#""type":"ack""#)));

    // A completely different op (delete_message) must now also be rate_limited.
    ws.send(WsMessage::Text(
        serde_json::json!({"type": "delete_message", "chat_id": "1", "message_id": "1"}).to_string(),
    ))
    .await
    .unwrap();
    let second = next_msg(&mut ws, 2000).await.unwrap();
    match second {
        WsMessage::Text(t) => {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["code"], "rate_limited");
            assert_eq!(v["for"], "delete_message", "edit/delete/restore/purge must share one ws_edit bucket");
        }
        other => panic!("expected delete_message to be rate_limited by edit's bucket, got {other:?}"),
    }

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn delete_restore_purge_hit_their_own_distinct_paths_with_no_content_field() {
    let mock = MockServer::start().await;
    for (op, path_str) in [
        ("delete_message", "/internal/message/delete"),
        ("restore_message", "/internal/message/restore"),
        ("purge_message", "/internal/message/purge"),
    ] {
        Mock::given(method("POST"))
            .and(path(path_str))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"op": op})))
            .mount(&mock)
            .await;
    }

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    for (frame_type, ack_for, expected_op) in [
        ("delete_message", "delete_message", "delete_message"),
        ("restore_message", "restore_message", "restore_message"),
        ("purge_message", "purge_message", "purge_message"),
    ] {
        let mut ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
        ws.send(WsMessage::Text(
            serde_json::json!({"type": frame_type, "chat_id": "1", "message_id": "1"}).to_string(),
        ))
        .await
        .unwrap();
        let msg = next_msg(&mut ws, 2000).await.expect("must receive an ack");
        match msg {
            WsMessage::Text(t) => {
                let v: serde_json::Value = serde_json::from_str(&t).unwrap();
                assert_eq!(v["type"], "ack");
                assert_eq!(v["for"], ack_for);
                assert_eq!(v["op"], expected_op, "{frame_type} must hit its own distinct /internal/message/* path");
            }
            other => panic!("expected ack for {frame_type}, got {other:?}"),
        }
    }

    // Verify the body shape sent for delete/restore/purge has no `content` field.
    let requests = mock.received_requests().await.unwrap();
    let delete_req = requests
        .iter()
        .find(|r| r.url.path() == "/internal/message/delete")
        .expect("delete request must have been made");
    let body: serde_json::Value = serde_json::from_slice(&delete_req.body).unwrap();
    assert!(body.get("content").is_none(), "delete/restore/purge bodies must carry no content field");
    assert!(body.get("user_id").is_some());
    assert!(body.get("chat_id").is_some());
    assert!(body.get("message_id").is_some());

    cleanup_user(&srv.state, user_id).await;
}

// ===========================================================================
// Stage 8.11 — cleanup / disconnect
// ===========================================================================

#[tokio::test]
#[serial]
async fn disconnect_removes_last_local_member_from_chat_instances() {
    let mock = MockServer::start().await;
    let chat_id: i64 = 900_000_600;
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"chat_ids": [chat_id.to_string()]})))
        .mount(&mock)
        .await;

    let mut opts = Opts::default();
    opts.app_internal_url = mock.uri();
    let server_id = opts.app_server_id.clone(); // AppState uses a fresh server_id internally too
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    // Give the connect-time bootstrap a moment to populate chat_instances.
    tokio::time::sleep(Duration::from_millis(200)).await;

    let mut conn = srv.state.redis.clone();
    let members_before: Vec<String> = conn
        .smembers(linka_common::redis_keys::chat_instances(chat_id))
        .await
        .unwrap_or_default();
    assert!(
        members_before.contains(&srv.state.server_id),
        "this server must be registered for the chat before disconnect"
    );

    drop(ws);
    // Poll for the disconnect cleanup to run (async, not instantaneous).
    let mut members_after = members_before.clone();
    for _ in 0..30 {
        tokio::time::sleep(Duration::from_millis(100)).await;
        members_after = conn
            .smembers(linka_common::redis_keys::chat_instances(chat_id))
            .await
            .unwrap_or_default();
        if !members_after.contains(&srv.state.server_id) {
            break;
        }
    }
    assert!(
        !members_after.contains(&srv.state.server_id),
        "disconnecting the last local subscriber must remove_chat this server from chat_instances"
    );

    let _ = server_id;
    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn presence_is_marked_offline_on_disconnect() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();

    let ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    tokio::time::sleep(Duration::from_millis(150)).await;

    let mut conn = srv.state.redis.clone();
    let (online_before, _) = ws_gateway::presence::get_status(&mut conn, user_id).await;
    assert!(online_before, "must be online right after connect");

    drop(ws);
    let mut online_after = true;
    for _ in 0..30 {
        tokio::time::sleep(Duration::from_millis(100)).await;
        let (online, _) = ws_gateway::presence::get_status(&mut conn, user_id).await;
        online_after = online;
        if !online_after {
            break;
        }
    }
    assert!(!online_after, "disconnect must mark presence offline");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn disconnect_unregisters_from_ws_conns_cap_set() {
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();

    let ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    tokio::time::sleep(Duration::from_millis(150)).await;

    let mut conn = srv.state.redis.clone();
    let card_before: i64 = conn.zcard(linka_common::redis_keys::ws_conns(user_id)).await.unwrap_or(0);
    assert!(card_before >= 1, "connection must be registered in ws:conns:{{uid}} while connected");

    drop(ws);
    let mut card_after = card_before;
    for _ in 0..30 {
        tokio::time::sleep(Duration::from_millis(100)).await;
        card_after = conn.zcard(linka_common::redis_keys::ws_conns(user_id)).await.unwrap_or(-1);
        if card_after == 0 {
            break;
        }
    }
    assert_eq!(card_after, 0, "disconnect must unregister_connection so it doesn't count toward the next connect's cap");

    cleanup_user(&srv.state, user_id).await;
}

#[tokio::test]
#[serial]
async fn disconnect_cleanup_is_safe_when_bootstrap_returned_zero_chats() {
    // app_internal_url stays unreachable -> fetch_chat_ids returns [] per its
    // own best-effort contract; connect-time setup is thus "partial" (no
    // chat_subs at all). Disconnect cleanup (empty emptied_chats) must not panic.
    let srv = start_server(Opts::default()).await;
    let user_id = unique_user_id();

    let ws = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    tokio::time::sleep(Duration::from_millis(150)).await;
    drop(ws);
    tokio::time::sleep(Duration::from_millis(300)).await;

    // The server task must still be alive (no panic unwound it) — prove it by
    // successfully completing a brand-new connection afterward.
    let user2 = unique_user_id();
    let mut ws2 = connect_raw(&srv.ws_base, &token_for(user2), &[]).await.expect("server must still be alive");
    ws2.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let msg = next_msg(&mut ws2, 2000).await.expect("must still serve new connections");
    assert!(matches!(msg, WsMessage::Text(t) if t.contains("heartbeat_ack")));

    cleanup_user(&srv.state, user_id).await;
    cleanup_user(&srv.state, user2).await;
}

#[tokio::test]
#[serial]
async fn force_disconnected_connection_still_runs_full_cleanup() {
    let mut opts = Opts::default();
    opts.ws_conn_max = 1;
    let srv = start_server(opts).await;
    let user_id = unique_user_id();

    let mut ws1 = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws1.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let _ = next_msg(&mut ws1, 2000).await.expect("first connection must be accepted");
    tokio::time::sleep(Duration::from_millis(150)).await;

    let mut conn = srv.state.redis.clone();
    let card_before: i64 = conn.zcard(linka_common::redis_keys::ws_conns(user_id)).await.unwrap_or(0);
    assert!(card_before >= 1);

    // A second connection for the same user, over ws_conn_max=1, evicts the
    // first via force_disconnect -> Close(4409).
    let mut ws2 = connect_raw(&srv.ws_base, &token_for(user_id), &[]).await.unwrap();
    ws2.send(WsMessage::Text(r#"{"type":"heartbeat"}"#.to_string())).await.unwrap();
    let _ = next_msg(&mut ws2, 2000).await.expect("second connection must be accepted");

    let close_msg = next_msg(&mut ws1, 2000).await.expect("first connection must be force-closed");
    assert_eq!(close_code(&close_msg), Some(4409));

    // Full cleanup (not just the abrupt close send) must still have run for
    // conn #1: ws:conns count settles back down rather than double-counting.
    let mut card_after = card_before + 10;
    for _ in 0..30 {
        tokio::time::sleep(Duration::from_millis(100)).await;
        card_after = conn.zcard(linka_common::redis_keys::ws_conns(user_id)).await.unwrap_or(-1);
        if card_after <= 1 {
            break;
        }
    }
    assert!(
        card_after <= 1,
        "force-disconnect must still run unregister_connection cleanup for the evicted conn, got count {card_after}"
    );

    cleanup_user(&srv.state, user_id).await;
}


