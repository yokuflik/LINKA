# ADR 0009 — Real phone verification via Firebase Phone Auth (client-side SMS, server-side token exchange)

Status: Accepted
Date: 2026-09-02

## Context

Auth today is an open OTP stub: `auth_service.verify_otp_and_login` has its
`stored_code != code` check commented out, so once `/auth/otp/request` has been
called, **any** 6-digit code logs the number in (ADR 0007 §Consequences, and
`.claude_docs/backend_services_and_api.md` §Auth). There is no SMS provider
wired up — codes only ever print to the server console.

We want real phone-number verification for arbitrary numbers, without:
- running our own SMS provider / paying per-message from the app,
- adding a heavy dependency or a service-account secret file to the 1 GB demo box,
- losing the ability to log in offline / without a Firebase project during local PoC work.

Firebase Phone Auth performs the SMS send **and** the code check entirely on the
client (JS SDK + invisible reCAPTCHA). The server's only job is to verify the
Firebase-issued ID token and trade it for our own access/refresh pair.

## Decision

1. **New endpoint `POST /auth/firebase/verify` `{ id_token }` → `LoginOut`.**
   `auth_service.verify_firebase_and_login(session, id_token)` verifies the token,
   extracts the verified `phone_number` claim, then reuses the exact find-or-create
   + `_create_access_token` / `_issue_refresh_token` tail of `verify_otp_and_login`.
   Our JWT remains the sole source of truth after login; the client calls
   `firebase.auth().signOut()` immediately.

2. **Token verification is manual against Google's JWKS — no `firebase-admin`.**
   `_verify_firebase_id_token` fetches and in-memory-caches Google's x509 certs
   from `https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com`
   (cache honors `Cache-Control: max-age`, min 1 h; one forced refetch on an
   unknown `kid` for key rotation), then
   `jwt.decode(id_token, key, algorithms=["RS256"], audience=FIREBASE_PROJECT_ID,
   issuer="https://securetoken.google.com/<FIREBASE_PROJECT_ID>")`, plus asserts
   non-empty `sub` and `auth_time <= now`. Any failure → `InvalidOTPError` (already
   mapped to HTTP 401 in `main.py`). New deps: **`cryptography`** only (for PyJWT
   RS256); `httpx` for the cert fetch is already present.
   Rationale: no ~15 MB `google-*` tree on the 1 GB box, no service-account JSON
   to mount or leak. The web `apiKey` is a public identifier, not a secret.

3. **The open OTP stub is closed.** `verify_otp_and_login` now enforces
   `stored_code != code` (previously commented out — see the long-standing
   warning in `.claude_docs/backend_services_and_api.md` §Auth). The console
   `[STUB] Would SMS OTP …` print stays as the "SMS provider" for non-Firebase
   numbers, but the code it prints must now actually be entered.

4. **Dev whitelist bypasses verification for exact phone strings `1`–`5`**
   (`DEV_AUTH_WHITELIST` env, default `"1,2,3,4,5"`), enforced **server-side** in
   both `request_otp` (no code stored, returns immediately) and
   `verify_otp_and_login` (skips straight to find-or-create). These are not real
   numbers and are only reachable by typing a single digit 1–5 into the phone
   field. This is the only login path that skips verification.

5. **The register/login `intent` pre-check is dropped for the Firebase path.**
   Firebase sends the SMS before our server is involved, so we cannot reject
   "register an already-registered number" / "log in with an unknown number"
   beforehand. `verify_firebase_and_login` is find-or-create, like today's first
   login. The `intent` gate still applies to the `1`–`5` stub path.

6. **Frontend: phone input gets a worldwide country-code picker + a lightweight
   validator.** New `poc/composables/usePhoneInput.js` + committed
   `poc/data/country-codes.js` (~250 ISO-3166 rows: `iso2`, `name`, `dial`,
   `flag`). Validation is a deliberately loose E.164 check
   (`^\+[1-9]\d{7,14}$` + a per-country national-length table for common
   countries) — **not** `libphonenumber-js` (bundle size, no build step in the
   PoC). It only needs to stop typos, not be authoritative. A raw input of
   `1`–`5` is treated as whitelisted and bypasses the validator.

7. **No user-facing channel choice.** The client picks stub vs Firebase purely
   from whether the raw input is in the 1–5 whitelist.

8. **Deploy:** `FIREBASE_PROJECT_ID` + `DEV_AUTH_WHITELIST` added to
   `deploy/env.production.example` and the server `.env`. Nothing to mount.
   `index.html` CSP/connectivity must allow `https://www.gstatic.com`,
   `https://*.firebaseapp.com`, `https://identitytoolkit.googleapis.com`,
   `https://securetoken.googleapis.com`, and `https://www.google.com`
   (reCAPTCHA). Firebase Authorized domains: `localhost` + the demo host.

## Consequences

- Real numbers now require a working Firebase project, network egress to Google,
  and HTTPS (reCAPTCHA) — the live demo has all three; `localhost` is reCAPTCHA-exempt.
- Firebase Spark free tier caps phone-auth SMS (~10/day as of 2026); heavier use
  needs Blaze. Test numbers (console-configured fixed codes) sidestep this for QA.
- The `1`–`5` whitelist is a real (small) auth bypass and must never be promoted
  to a user-facing environment. Set `DEV_AUTH_WHITELIST=` (empty) to remove it.
- Closing the OTP stub means the three `test_auth_service.py` tests that asserted
  the *wrong* code is rejected now pass (they were failing against the open stub).
- Manual JWKS verification means we own the token-validation logic (issuer,
  audience, expiry, key rotation) that `firebase-admin` would otherwise handle;
  covered by unit tests with a forged-token / wrong-aud / expired matrix.
- `phone_number` from the Firebase claim is trusted as-is for find-or-create;
  Firebase normalizes to E.164, matching what the picker produces.
- No DB schema change, no migration, no new model.
