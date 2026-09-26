//! Phase 9 — `bootstrap.rs` HTTP client helpers, mock-server based.
//!
//! Pure request/response mapping, no Redis needed. Each test spins up its own
//! `wiremock::MockServer` (ephemeral local port), so these run fully isolated
//! and in parallel with no shared state.

use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use ws_gateway::bootstrap::{fetch_chat_ids, post_json, presence_authorized, typing_allowed, PostOutcome};

fn client() -> reqwest::Client {
    reqwest::Client::new()
}

// ---------------------------------------------------------------------------
// fetch_chat_ids
// ---------------------------------------------------------------------------

#[tokio::test]
async fn fetch_chat_ids_200_returns_parsed_i64s() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "chat_ids": ["1", "2", "3"]
        })))
        .mount(&server)
        .await;

    let ids = fetch_chat_ids(&client(), &server.uri(), "tok").await;
    assert_eq!(ids, vec![1, 2, 3]);
}

#[tokio::test]
async fn fetch_chat_ids_drops_non_numeric_entries_but_keeps_the_rest() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "chat_ids": ["1", "not-a-number", "3"]
        })))
        .mount(&server)
        .await;

    let ids = fetch_chat_ids(&client(), &server.uri(), "tok").await;
    assert_eq!(ids, vec![1, 3]);
}

#[tokio::test]
async fn fetch_chat_ids_500_returns_empty_no_panic() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&server)
        .await;

    let ids = fetch_chat_ids(&client(), &server.uri(), "tok").await;
    assert_eq!(ids, Vec::<i64>::new());
}

#[tokio::test]
async fn fetch_chat_ids_connection_refused_returns_empty() {
    // Nothing listening on this port — reqwest fails at the transport layer.
    let ids = fetch_chat_ids(&client(), "http://127.0.0.1:1", "tok").await;
    assert_eq!(ids, Vec::<i64>::new());
}

#[tokio::test]
async fn fetch_chat_ids_malformed_json_returns_empty() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/ws-bootstrap"))
        .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
        .mount(&server)
        .await;

    let ids = fetch_chat_ids(&client(), &server.uri(), "tok").await;
    assert_eq!(ids, Vec::<i64>::new());
}

// ---------------------------------------------------------------------------
// presence_authorized
// ---------------------------------------------------------------------------

#[tokio::test]
async fn presence_authorized_true() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"authorized": true})))
        .mount(&server)
        .await;

    let result = presence_authorized(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, Some(true));
}

#[tokio::test]
async fn presence_authorized_false() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"authorized": false})))
        .mount(&server)
        .await;

    let result = presence_authorized(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, Some(false));
}

#[tokio::test]
async fn presence_authorized_4xx_is_none() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(404))
        .mount(&server)
        .await;

    let result = presence_authorized(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, None);
}

#[tokio::test]
async fn presence_authorized_5xx_is_none() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&server)
        .await;

    let result = presence_authorized(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, None);
}

#[tokio::test]
async fn presence_authorized_network_failure_is_none() {
    let result = presence_authorized(&client(), "http://127.0.0.1:1", 1, 2).await;
    assert_eq!(result, None);
}

#[tokio::test]
async fn presence_authorized_malformed_json_is_none() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/presence-authorized"))
        .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
        .mount(&server)
        .await;

    let result = presence_authorized(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, None);
}

// ---------------------------------------------------------------------------
// typing_allowed
// ---------------------------------------------------------------------------

#[tokio::test]
async fn typing_allowed_true() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"allowed": true})))
        .mount(&server)
        .await;

    let result = typing_allowed(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, Some(true));
}

#[tokio::test]
async fn typing_allowed_false() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"allowed": false})))
        .mount(&server)
        .await;

    let result = typing_allowed(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, Some(false));
}

#[tokio::test]
async fn typing_allowed_4xx_is_none() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(400))
        .mount(&server)
        .await;

    let result = typing_allowed(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, None);
}

#[tokio::test]
async fn typing_allowed_5xx_is_none() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(503))
        .mount(&server)
        .await;

    let result = typing_allowed(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, None);
}

#[tokio::test]
async fn typing_allowed_network_failure_is_none() {
    let result = typing_allowed(&client(), "http://127.0.0.1:1", 1, 2).await;
    assert_eq!(result, None);
}

#[tokio::test]
async fn typing_allowed_malformed_json_is_none() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/internal/typing-allowed"))
        .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
        .mount(&server)
        .await;

    let result = typing_allowed(&client(), &server.uri(), 1, 2).await;
    assert_eq!(result, None);
}

// ---------------------------------------------------------------------------
// post_json
// ---------------------------------------------------------------------------

#[tokio::test]
async fn post_json_2xx_valid_body_is_ok() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"ok": true, "id": "5"})))
        .mount(&server)
        .await;

    let outcome = post_json(&client(), &server.uri(), "/internal/message/edit", &serde_json::json!({})).await;
    match outcome {
        PostOutcome::Ok(body) => assert_eq!(body, serde_json::json!({"ok": true, "id": "5"})),
        _ => panic!("expected Ok"),
    }
}

#[tokio::test]
async fn post_json_2xx_empty_body_falls_back_to_empty_object() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;

    let outcome = post_json(&client(), &server.uri(), "/internal/message/edit", &serde_json::json!({})).await;
    match outcome {
        PostOutcome::Ok(body) => assert_eq!(body, serde_json::json!({})),
        _ => panic!("expected Ok with empty object fallback"),
    }
}

#[tokio::test]
async fn post_json_2xx_non_json_body_falls_back_to_empty_object() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
        .mount(&server)
        .await;

    let outcome = post_json(&client(), &server.uri(), "/internal/message/edit", &serde_json::json!({})).await;
    match outcome {
        PostOutcome::Ok(body) => assert_eq!(body, serde_json::json!({})),
        _ => panic!("expected Ok with empty object fallback"),
    }
}

#[tokio::test]
async fn post_json_403_is_forbidden() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(403).set_body_json(serde_json::json!({"detail": "not your message"})))
        .mount(&server)
        .await;

    let outcome = post_json(&client(), &server.uri(), "/internal/message/edit", &serde_json::json!({})).await;
    match outcome {
        PostOutcome::ClientError(code, detail) => {
            assert_eq!(code, "forbidden");
            assert_eq!(detail, "not your message");
        }
        _ => panic!("expected ClientError(forbidden, ..)"),
    }
}

#[tokio::test]
async fn post_json_other_4xx_is_bad_request() {
    for status in [400u16, 404, 422] {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/internal/message/edit"))
            .respond_with(ResponseTemplate::new(status).set_body_json(serde_json::json!({"detail": "nope"})))
            .mount(&server)
            .await;

        let outcome = post_json(&client(), &server.uri(), "/internal/message/edit", &serde_json::json!({})).await;
        match outcome {
            PostOutcome::ClientError(code, detail) => {
                assert_eq!(code, "bad_request", "status {status}");
                assert_eq!(detail, "nope", "status {status}");
            }
            _ => panic!("expected ClientError(bad_request, ..) for status {status}"),
        }
    }
}

#[tokio::test]
async fn post_json_4xx_no_detail_field_defaults_to_empty_string() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(400).set_body_json(serde_json::json!({})))
        .mount(&server)
        .await;

    let outcome = post_json(&client(), &server.uri(), "/internal/message/edit", &serde_json::json!({})).await;
    match outcome {
        PostOutcome::ClientError(code, detail) => {
            assert_eq!(code, "bad_request");
            assert_eq!(detail, "");
        }
        _ => panic!("expected ClientError(bad_request, \"\")"),
    }
}

#[tokio::test]
async fn post_json_5xx_is_failed() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/message/edit"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&server)
        .await;

    let outcome = post_json(&client(), &server.uri(), "/internal/message/edit", &serde_json::json!({})).await;
    assert!(matches!(outcome, PostOutcome::Failed));
}

#[tokio::test]
async fn post_json_network_failure_is_failed() {
    let outcome = post_json(&client(), "http://127.0.0.1:1", "/internal/message/edit", &serde_json::json!({})).await;
    assert!(matches!(outcome, PostOutcome::Failed));
}
