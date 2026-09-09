# ADR 0026 — Client-side end-to-end encryption (AES-256-GCM + ECDH)

Status: Accepted
Date: 2026-09-09

## Context

Linka currently stores message plaintext in `messages.content`. We want the
server (FastAPI + Postgres + Redis) to only ever hold, route and store **opaque
ciphertext** — never the plaintext, never any private key. All cryptography runs
in the Vue 3 PoC using the native Web Crypto API.

Scope of this ADR: the **text** message path only. Media E2E, a Signal-style
double ratchet, multi-device key sync and the safety-number UI are explicitly
deferred (see "Out of scope").

## Decision

### Keys

- **Identity keypair (long-term, one per account).** ECDH on P-256, generated
  in the browser: `crypto.subtle.generateKey({name:"ECDH", namedCurve:"P-256"},
  false, ["deriveKey","deriveBits"])`. The private key is created
  **non-extractable** and stored as a live `CryptoKey` in `IndexedDB` (DB name
  scoped by `user_id`); it is never serialised and never leaves the device.
- The **public key** is exported as JWK and uploaded to the server via
  `PUT /users/me/public-key`. The server treats it as an opaque blob, only
  sanity-checking that it is a public EC/P-256 JWK with **no `d` component**
  (a private-key upload is rejected).
- New device / cleared storage ⇒ the user generates a fresh keypair and loses
  the ability to read history encrypted to the old key. Accepted (WhatsApp-like).

### Per-message encryption (hybrid, sender-side ephemeral — pairwise wrapping)

1. Sender draws a random 256-bit **message key `MK`** + 96-bit IV and
   `AES-256-GCM`-encrypts the UTF-8 plaintext → `ciphertext` (base64).
2. Sender generates a **fresh ephemeral ECDH keypair for this message**.
3. For **every chat participant** (including the sender itself, so its own
   "sent" bubble and its other sessions can read the message):
   `ECDH(ephemeral_priv, recipient_pub)` → `HKDF-SHA256` → 256-bit KEK →
   `AES-256-GCM` wrap of `MK` (own 96-bit IV).
4. The client sends, over the existing WS `send_message` action:
   - `ciphertext` (base64) — goes into `messages.content` unchanged.
   - `enc` — the **encryption header**, stored verbatim in the new
     `messages.enc_header` JSONB column and echoed on `new_message` /
     `GET /chats/{id}/messages`:

     ```json
     {
       "v": 1,
       "alg": "A256GCM",
       "iv": "<b64 12 bytes>",
       "eph_pub": { "kty": "EC", "crv": "P-256", "x": "...", "y": "..." },
       "wraps": {
         "<user_id>": { "ek": "<b64 wrapped MK>", "iv": "<b64 12 bytes>" }
       }
     }
     ```

For a 1:1 chat `wraps` has 2 entries. For a group of N it has N entries and the
sender does N wrap operations — acceptable at PoC scale.

### Server changes (pure pass-through, no crypto)

- `messages`: two new columns, added with `ADD COLUMN IF NOT EXISTS` (no
  migrations, per CLAUDE.md):
  - `is_encrypted BOOLEAN NOT NULL DEFAULT FALSE`
  - `enc_header JSONB` (nullable)
- `content` (existing nullable `TEXT`) carries the base64 ciphertext when
  `is_encrypted`.
- `build_last_message_preview` returns a constant `"🔒 Encrypted message"` when
  the message is encrypted — **the server never derives or stores a plaintext
  preview**. The chat-list line for an encrypted chat is that constant; a
  future client change may decrypt and show a real preview locally.
- Offline **push** for an encrypted message uses a generic body
  (`"New message"`), never `content` (which is ciphertext).
- The send path threads `enc_header` unchanged:
  `ws_router._handle_send_message` → `send_queue.enqueue_outgoing_message`
  (JSON string on the stream) → `SendWorker.process_entry` (parsed back) →
  `message_service.process_outgoing` → `crud.create_message` → row.
  `fan_out_message` copies `content` + `enc_header` onto the `new_message`
  event verbatim. No Python code ever inspects the plaintext.
- **Public-key distribution** lives in `modules/users`:
  - `user_public_keys` table: `(user_id PK, public_key JSONB, algo, fingerprint,
    created_at, updated_at)`. One current key per user; the shape allows going
    multi-row (per-device) later without a migration.
  - `PUT /users/me/public-key` — upsert the caller's key.
  - `GET /users/{user_id}/public-key` — a single peer's key (New-chat flow).
  - `GET /chats/{chat_id}/key-bundle` — every participant's key in one call
    (the group send path); requester must be a participant.
  - `fingerprint` = SHA-256 hex of the canonical JWK — computed server-side and
    returned, but **no UI is built for it in this cycle**.

### Client (PoC) — Phase 3, summarised here

- `poc/composables/useE2E.js`: key lifecycle (generate-on-first-login, publish,
  IndexedDB persistence), `encryptFor(participants, plaintext)` and
  `decrypt(message)`. Peer public keys cached in memory, seeded from the
  key-bundle endpoint and refreshed on `profile_updated` / membership change.
- Send flow encrypts before `sendMessage`; `useWsRouter` decrypts on
  `new_message` and history load before rendering.

## What the server still sees (accepted metadata leakage)

`sender_id`, `chat_id`, participants, Snowflake-ordered timestamps, ciphertext
length, `type`, `reply_to_message_id`, receipts, presence, typing. Not the
message text.

## Trust model / limitations (accepted for the PoC)

- **TOFU.** The server distributes public keys and could substitute one (MITM).
  Mitigation (fingerprint / safety number) is stored server-side but not
  surfaced. Deferred.
- **No forward secrecy on the recipient side.** A per-message ephemeral sender
  key gives partial (sender-side) FS only; no double ratchet.
- **Single device per account.** Multi-device needs per-device keys; deferred.
- History predating a client's key, or sent by a pre-feature client, renders as
  today (plaintext / unencrypted). `is_encrypted=FALSE` default keeps the old
  and new paths coexisting with zero backfill.

## Future scale path — Sender Keys

Pairwise wrapping is `O(N)` wrap ops and `O(N)` header bytes per group message.
The scale path is a Signal-style **Sender Keys** ratchet: each member publishes
a per-chat sending key once (pairwise-wrapped), then broadcasts a single
ciphertext per message with no per-recipient envelope. Not built now; recorded
so the header format (`v` field) can be versioned toward it.

## Out of scope

Media/file E2E, double ratchet, multi-device key sync, safety-number UI,
key-transparency / gossip, encrypted push payloads. Vector search, embeddings
and rate limiting are unrelated and untouched.
