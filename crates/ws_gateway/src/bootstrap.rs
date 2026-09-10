//! Connect-time call to the Python app's `GET /internal/ws-bootstrap`
//! (ADR 0036): resolves the chat ids a freshly-connected user belongs to so
//! the gateway can populate `chat_subs` and register in `chat_instances`.
//!
//! Best-effort: any failure yields an empty list — the socket still opens, the
//! client just won't get live chat events until it reconnects (same failure
//! mode as a dropped routing registration).
//!
//! Also hosts the other internal helpers the gateway needs (ADR 0036):
//! `presence-authorized` and `typing-allowed`.

use serde::Deserialize;

#[derive(Deserialize)]
struct BootstrapResponse {
    chat_ids: Vec<String>,
}

#[derive(Deserialize)]
struct AuthorizedResponse {
    authorized: bool,
}

#[derive(Deserialize)]
struct AllowedResponse {
    allowed: bool,
}

async fn get_bool<T, F>(
    client: &reqwest::Client,
    url: String,
    query: &[(&str, String)],
    pick: F,
) -> Option<bool>
where
    T: serde::de::DeserializeOwned,
    F: FnOnce(T) -> bool,
{
    match client.get(&url).query(query).send().await {
        Ok(r) => match r.error_for_status() {
            Ok(r) => r.json::<T>().await.ok().map(pick),
            Err(e) => {
                tracing::warn!(error = %e, "internal call returned an error status");
                None
            }
        },
        Err(e) => {
            tracing::warn!(error = %e, "internal call failed");
            None
        }
    }
}

/// `GET /internal/presence-authorized`. `None` on any failure (caller decides
/// the fail-safe direction).
pub async fn presence_authorized(
    client: &reqwest::Client,
    base_url: &str,
    watcher_id: i64,
    target_user_id: i64,
) -> Option<bool> {
    let url = format!("{}/internal/presence-authorized", base_url.trim_end_matches('/'));
    get_bool::<AuthorizedResponse, _>(
        client,
        url,
        &[
            ("watcher_id", watcher_id.to_string()),
            ("target_user_id", target_user_id.to_string()),
        ],
        |r| r.authorized,
    )
    .await
}

/// Outcome of a `/internal/message/*` POST.
pub enum PostOutcome {
    /// 2xx — the JSON body (the ack payload the gateway relays).
    Ok(serde_json::Value),
    /// 4xx — a client-visible error: (`code`, human message).
    ClientError(String, String),
    /// 5xx / network / parse — surface as a generic internal error.
    Failed,
}

/// POST a JSON body to `path` on the app and classify the response.
pub async fn post_json(
    client: &reqwest::Client,
    base_url: &str,
    path: &str,
    body: &serde_json::Value,
) -> PostOutcome {
    let url = format!("{}{}", base_url.trim_end_matches('/'), path);
    let resp = match client.post(&url).json(body).send().await {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(error = %e, path, "internal POST failed");
            return PostOutcome::Failed;
        }
    };
    let status = resp.status();
    if status.is_success() {
        return match resp.json::<serde_json::Value>().await {
            Ok(v) => PostOutcome::Ok(v),
            Err(_) => PostOutcome::Ok(serde_json::json!({})),
        };
    }
    if status.is_client_error() {
        let detail = resp
            .json::<serde_json::Value>()
            .await
            .ok()
            .and_then(|v| v.get("detail").and_then(|d| d.as_str()).map(str::to_string))
            .unwrap_or_default();
        let code = if status.as_u16() == 403 { "forbidden" } else { "bad_request" };
        return PostOutcome::ClientError(code.to_string(), detail);
    }
    tracing::warn!(status = %status, path, "internal POST returned a server error");
    PostOutcome::Failed
}

/// `GET /internal/typing-allowed` — full server-side gate for a typing event.
pub async fn typing_allowed(
    client: &reqwest::Client,
    base_url: &str,
    chat_id: i64,
    sender_id: i64,
) -> Option<bool> {
    let url = format!("{}/internal/typing-allowed", base_url.trim_end_matches('/'));
    get_bool::<AllowedResponse, _>(
        client,
        url,
        &[
            ("chat_id", chat_id.to_string()),
            ("sender_id", sender_id.to_string()),
        ],
        |r| r.allowed,
    )
    .await
}

/// Fetch the user's chat ids. Never errors — logs and returns `[]` on failure.
pub async fn fetch_chat_ids(client: &reqwest::Client, base_url: &str, token: &str) -> Vec<i64> {
    let url = format!("{}/internal/ws-bootstrap", base_url.trim_end_matches('/'));
    let result = async {
        let resp = client
            .get(&url)
            .query(&[("token", token)])
            .send()
            .await?
            .error_for_status()?;
        let body: BootstrapResponse = resp.json().await?;
        Ok::<_, reqwest::Error>(body)
    }
    .await;

    match result {
        Ok(body) => body
            .chat_ids
            .iter()
            .filter_map(|s| s.parse::<i64>().ok())
            .collect(),
        Err(e) => {
            tracing::warn!(error = %e, "ws-bootstrap failed; connecting with no chat subscriptions");
            Vec::new()
        }
    }
}
