//! JWT verification. Must match the Python side (`modules/auth`, PyJWT HS256).
//!
//! Tokens are HS256-signed with the shared `JWT_SECRET`. The gateway only
//! needs to authenticate the connection: pull `sub` (the user id) and enforce
//! `exp`. Any richer authorization is a Redis/DB lookup, not a claim.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claims {
    /// User id, stringified Snowflake. Python signs it as a string.
    pub sub: String,
    /// Expiry, seconds since epoch.
    pub exp: usize,
}

#[derive(Debug, thiserror::Error)]
pub enum AuthError {
    #[error("token invalid or expired")]
    Invalid,
}

/// Verify an HS256 token against `secret` and return its claims.
///
/// `exp` is validated by the library; a missing/foreign signature, a bad
/// algorithm, or an elapsed `exp` all collapse to `AuthError::Invalid` so the
/// caller closes the socket with a single close code (4401).
pub fn verify(token: &str, secret: &[u8]) -> Result<Claims, AuthError> {
    use jsonwebtoken::{decode, Algorithm, DecodingKey, Validation};

    let mut validation = Validation::new(Algorithm::HS256);
    validation.validate_exp = true;
    // Python does not set `aud`/`iss`; do not require them.
    validation.required_spec_claims.clear();
    validation.required_spec_claims.insert("exp".to_string());

    decode::<Claims>(token, &DecodingKey::from_secret(secret), &validation)
        .map(|data| data.claims)
        .map_err(|_| AuthError::Invalid)
}

/// Parse `sub` into the numeric user id the rest of the system uses.
pub fn user_id(claims: &Claims) -> Result<i64, AuthError> {
    claims.sub.parse().map_err(|_| AuthError::Invalid)
}
