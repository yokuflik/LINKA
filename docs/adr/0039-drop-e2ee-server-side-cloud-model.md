# ADR 0039 — Drop client-side E2EE; adopt a server-side cloud model

Status: Accepted
Date: 2026-09-10

Supersedes: ADR 0026, ADR 0027, ADR 0034 (all now **Superseded**).

## Context

ADR 0026/0027/0034 put text messages under client-side end-to-end encryption:
the browser held the keys, the server stored only opaque ciphertext in
`messages.content`, and the chat-list preview was a denormalised ciphertext blob
the client decrypted on load.

The product direction now requires **server-side full-text and vector search**
over message history (and future server-side features: moderation, smart
replies, cross-device history without a key-transfer dance). E2EE makes all of
that impossible without shipping the entire history to every client and
indexing on-device — a non-starter at the target scale.

We are therefore moving to a **Telegram-style cloud model**: transport security
only (TLS / WSS), plaintext at rest, server has full read access to message
content.

## Decision

1. **Message content is plaintext.** The WS `send_message` / `edit_message`
   frames and the REST `MessageOut` carry `content` as UTF-8 plain text. No
   `enc` / `enc_header` fields anywhere.
2. **Schema cleanup** (`scripts/init_db.py`, no migrations per CLAUDE.md — uses
   `DROP ... IF EXISTS`):
   - `ALTER TABLE messages DROP COLUMN IF EXISTS is_encrypted`
   - `ALTER TABLE messages DROP COLUMN IF EXISTS enc_header`
   - `ALTER TABLE chats DROP COLUMN IF EXISTS last_message_enc`
   - `DROP TABLE IF EXISTS user_public_keys`
   A deployed DB needs `python3 -m scripts.init_db` run once by hand.
3. **Endpoints removed:** `PUT /users/me/public-key`,
   `GET /users/{id}/public-key`, `GET /chats/{id}/key-bundle`. Their service /
   CRUD / schema code is deleted (`modules/chats/key_bundle.py`,
   `user_service` public-key block, `crud_user.*_public_key`, `PublicKeyIn/Out`,
   `UserPublicKey` model).
4. **`EncryptionRequiredError` removed** — a plaintext edit is always allowed.
5. **`build_last_message_preview`** drops its `is_encrypted` arg and the
   `"🔒 Encrypted message"` constant; the preview is always derived from the
   plaintext / media label.
6. **Send path**: `send_queue.enqueue_outgoing_message` / `SendWorker` /
   `process_outgoing` / `crud.create_message` / `edit_message_content` drop the
   `enc_header` parameter. The Rust `ws_gateway` (`crates/common/src/events.rs`,
   `message_ops.rs`) drops the `enc` frame fields and the `enc_header` stream
   field.
7. **Frontend**: `poc/composables/useE2E.js` deleted; all
   encrypt/decrypt/key-exchange logic removed from `useMessageSend`,
   `useForward`, `useOutbox`, `useWsRouter`, `useChatOpen`, `useChatList`,
   `useAuth`, `index.html`. Message cache `SCHEMA` bumped 3→4 (one-time
   refetch drops any cached ciphertext rows).

## Data migration

PoC / dev only. Existing `is_encrypted=TRUE` rows hold ciphertext in `content`
and cannot be recovered server-side. The dev DB is **wiped and reseeded**
(`scripts/init_db.py --drop` + `scripts/seed_mock_data.py`). No production data.

## Consequences

- The server can now index `messages.content` directly (FTS / embeddings —
  future ADR).
- Security posture: message content is protected in transit (TLS/WSS, ADR 0012)
  and by DB/host access controls only. This is a deliberate downgrade from E2EE,
  matching the "cloud chat" threat model. No "secret chat" option is planned in
  this cycle.
- Metadata exposure is unchanged (it was already server-visible under ADR 0026).
