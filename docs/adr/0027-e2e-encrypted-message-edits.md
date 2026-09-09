# ADR 0027 — End-to-end encrypted message edits

Status: Accepted
Date: 2026-09-09

## Context

ADR 0026 encrypts the **send** path only. `edit_message` (WS action, synchronous
path — no send queue) still sends and stores plaintext in `messages.content`,
overwriting the ciphertext of an encrypted message and leaking the edited text
to the server. This ADR closes that gap for **text** messages.

## Decision

### Wire protocol

- The WS `edit_message` action gains an optional `enc` field (same shape as
  `send_message`: `{v, alg, iv, eph_pub, wraps}`). When present, `content` is
  base64 ciphertext, re-encrypted client-side for the chat's **current**
  participant set (a fresh message key + ephemeral keypair, exactly like a new
  send).
- The `message_edited` fan-out event carries `is_encrypted` + `enc_header`
  (previously neither), so every client can decrypt the new content.

### Server behaviour (pass-through, no crypto)

- `crud.edit_message_content` also writes `enc_header` and `is_encrypted` in its
  single `UPDATE ... RETURNING`.
- `edit_delete.edit_message(session, user_id, chat_id, message_id, new_content,
  enc_header=None)`:
  - `enc_header` given ⇒ store ciphertext, `is_encrypted=True`.
  - `enc_header` absent **and the existing row is `is_encrypted`** ⇒ reject with
    `EncryptionRequiredError` (→ WS `error` frame). This blocks a stale or buggy
    client from silently downgrading an encrypted message to plaintext.
  - `enc_header` absent and the existing row is plaintext ⇒ unchanged (plaintext
    edit, `is_encrypted` stays `False`, `enc_header` stays `NULL`).
- No schema change — `messages.is_encrypted` / `messages.enc_header` already
  exist (ADR 0026). No migration.
- `build_last_message_preview` already returns the `🔒 Encrypted message`
  constant for `is_encrypted` rows; the edit path reuses
  `updateChatPreviewIfLast` client-side and the server preview column is only
  touched by the send path, so nothing to change there.

### Client (PoC)

- `useMessageSend.sendMessage` edit branch: `await ctx.encryptFor(chatId,
  newContent)`; on success send `content=ciphertext` + `enc`, keep the local
  bubble on the plaintext (as the send path already does). `encryptFor`
  returning `null` for a chat that *was* encrypted is surfaced as a friendly
  error rather than a plaintext downgrade.
- `useWsRouter` `message_edited`: when `msg.is_encrypted`, decrypt `msg.content`
  via `msg.enc_header` before writing `m.content` and before
  `updateChatPreviewIfLast`.

## Out of scope

Media-caption edits, edit history, and everything already deferred by ADR 0026
(media E2E, double ratchet, multi-device, safety-number UI).
