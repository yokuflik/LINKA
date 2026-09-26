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

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{encode, EncodingKey, Header};
    use serde::Serialize;
    use std::time::{SystemTime, UNIX_EPOCH};

    const SECRET: &[u8] = b"test-secret";

    fn now() -> usize {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as usize
    }

    fn sign(claims: &Claims, secret: &[u8]) -> String {
        encode(&Header::new(jsonwebtoken::Algorithm::HS256), claims, &EncodingKey::from_secret(secret)).unwrap()
    }

    #[test]
    fn valid_token_correct_secret_succeeds() {
        let claims = Claims { sub: "42".to_string(), exp: now() + 3600 };
        let token = sign(&claims, SECRET);
        let parsed = verify(&token, SECRET).unwrap();
        assert_eq!(parsed.sub, "42");
        assert_eq!(user_id(&parsed).unwrap(), 42);
    }

    #[test]
    fn wrong_secret_is_invalid() {
        let claims = Claims { sub: "42".to_string(), exp: now() + 3600 };
        let token = sign(&claims, SECRET);
        assert!(matches!(verify(&token, b"other-secret"), Err(AuthError::Invalid)));
    }

    #[test]
    fn expired_token_is_invalid() {
        // jsonwebtoken defaults to 60s of leeway around `exp`; go well past it.
        let claims = Claims { sub: "42".to_string(), exp: now() - 120 };
        let token = sign(&claims, SECRET);
        assert!(matches!(verify(&token, SECRET), Err(AuthError::Invalid)));
    }

    #[test]
    fn none_algorithm_is_rejected() {
        // Hand-build an `alg: none` token with no signature — a classic JWT
        // footgun; verify() must reject it even though the payload looks valid.
        #[derive(Serialize)]
        struct Header2 {
            alg: &'static str,
            typ: &'static str,
        }
        let header = base64_url(&serde_json::to_vec(&Header2 { alg: "none", typ: "JWT" }).unwrap());
        let claims = Claims { sub: "42".to_string(), exp: now() + 3600 };
        let payload = base64_url(&serde_json::to_vec(&claims).unwrap());
        let token = format!("{header}.{payload}.");
        assert!(matches!(verify(&token, SECRET), Err(AuthError::Invalid)));
    }

    #[test]
    fn rs256_signed_token_is_rejected() {
        use jsonwebtoken::{Algorithm, EncodingKey};
        // RS256 test key (2048-bit, PKCS#1 DER) — generated solely for this test.
        const RSA_DER: &[u8] = include_bytes!("../testdata/rsa_test_key.der");
        let claims = Claims { sub: "42".to_string(), exp: now() + 3600 };
        let key = EncodingKey::from_rsa_der(RSA_DER);
        let token = encode(&Header::new(Algorithm::RS256), &claims, &key).unwrap();
        assert!(matches!(verify(&token, SECRET), Err(AuthError::Invalid)));
    }

    #[test]
    fn non_numeric_sub_verifies_but_user_id_fails() {
        let claims = Claims { sub: "abc".to_string(), exp: now() + 3600 };
        let token = sign(&claims, SECRET);
        let parsed = verify(&token, SECRET).expect("HS256 signature is valid, sub content is not checked");
        assert!(matches!(user_id(&parsed), Err(AuthError::Invalid)));
    }

    #[test]
    fn missing_exp_claim_is_rejected() {
        // Build the JWT by hand since `Claims` requires `exp` to serialize.
        #[derive(Serialize)]
        struct NoExpClaims {
            sub: String,
        }
        let token = encode(
            &Header::new(jsonwebtoken::Algorithm::HS256),
            &NoExpClaims { sub: "42".to_string() },
            &EncodingKey::from_secret(SECRET),
        )
        .unwrap();
        assert!(matches!(verify(&token, SECRET), Err(AuthError::Invalid)));
    }

    #[test]
    fn extra_unknown_claims_still_verify() {
        #[derive(Serialize)]
        struct ExtraClaims {
            sub: String,
            exp: usize,
            role: String,
            iss: String,
        }
        let token = encode(
            &Header::new(jsonwebtoken::Algorithm::HS256),
            &ExtraClaims { sub: "42".to_string(), exp: now() + 3600, role: "admin".to_string(), iss: "someone".to_string() },
            &EncodingKey::from_secret(SECRET),
        )
        .unwrap();
        let parsed = verify(&token, SECRET).expect("unknown extra claims must not break verification");
        assert_eq!(parsed.sub, "42");
    }

    fn base64_url(bytes: &[u8]) -> String {
        use base64::Engine;
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
    }
}
