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
