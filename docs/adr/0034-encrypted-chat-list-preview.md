# ADR 0034 — Decryptable last-message preview for the chat list

**Status:** Accepted
**Date:** 2026-09-09
**Extends:** ADR 0026 (client-side E2E for text), ADR 0027 (encrypted edits)

## Context

`GET /chats` returns `ChatOut.last_message_preview`, a denormalised string kept
on the `chats` row by `crud_message` (`build_last_message_preview`). For an
encrypted last message that string is the fixed literal
`"🔒 Encrypted message"` (`ENCRYPTED_MESSAGE_PREVIEW`) — the server never has
the plaintext.

Live messages already render a real decrypted preview: `useWsRouter` calls
`decryptMessage({content, enc_header})` on the `new_message` event and bumps the
sidebar line. But on a fresh load / reconnect the sidebar shows the raw
placeholder for every encrypted chat until a new message arrives, which looks
broken.

The client can only decrypt if it receives the ciphertext **and** the
`enc_header` (whose `wraps` map carries this user's wrapped message key). Neither
is in `ChatOut` today.

## Options considered

1. **Per-chat message read in `get_chat_list`.** Load the last `Message` row for
   each chat to pull `content` + `enc_header`. Rejected: the home screen already
   does one `COUNT` per chat; adding a second per-chat query into the partitioned
   `messages` table on the hottest read path is the wrong direction at
   tens-of-billions scale.
2. **Denormalise the encrypted payload onto the `chats` row** (chosen). Mirror
   what `last_message_preview` already does: write the encrypted last message's
   `{ct, header}` into a new nullable `chats.last_message_enc` JSONB column in the
   same single-row recency bump. No extra read, one extra column on a small
   table.
3. **Frontend-only: decrypt from the local message cache.** Works only for chats
   already visited on this device this schema-version; a fresh device / cleared
   storage still shows the placeholder. Kept as a *fallback* layer, not the fix.

## Decision

Add `chats.last_message_enc JSONB NULL`. Shape:

```json
{ "ct": "<base64 AES-256-GCM ciphertext>", "header": { ...enc_header... } }
```

This is the *same* blob every recipient of that message already received over the
socket (the `wraps` map has one wrapped key per participant), so putting it on
the shared chat row leaks nothing new.

`crud_message` keeps it in lockstep with `last_message_preview`, on the same
`UPDATE chats WHERE id = :chat_id` already issued:

| Path | `last_message_preview` | `last_message_enc` |
|---|---|---|
| send, encrypted (`enc_header` present) | `"🔒 Encrypted message"` | `{ct, header}` |
| send, plaintext | preview text | `NULL` |
| edit of the last message, encrypted | `"🔒 Encrypted message"` | `{ct, header}` |
| edit of the last message, plaintext | preview text | `NULL` |
| soft-delete / purge of the last message | tombstone string | `NULL` |
| undelete of the last message | rebuilt from content | `{ct, header}` if the row is encrypted, else `NULL` |
| system message | untouched | untouched |

`ChatOut.last_message_enc: Optional[dict]` exposes it verbatim (opaque to the
server, no validation beyond "is a dict").

### Client

`useChatList.loadChats` post-processes each chat: when `last_message_enc` is
present, `decryptMessage({is_encrypted: true, content: enc.ct, enc_header:
enc.header})` and replace `last_message_preview` with `previewText(text, type)`.
On `null` / decrypt failure the server's placeholder string stands. The
message-cache fallback (option 3) runs first for chats we've already got locally.

## Consequences

- One nullable JSONB column on `chats`. Per ADR 0005 the DB has no migration
  framework — `scripts/init_db.py` gets an idempotent
  `ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_message_enc JSONB`.
- Every `crud_message` write path that already touches `last_message_preview`
  now also sets `last_message_enc` (mostly to `NULL`). No new queries.
- Slightly larger `chats` rows for encrypted chats (~1–4 KB of JSONB for a
  1:1; more for a large group's `wraps` map). Acceptable — one row per chat.
- The value is never read server-side; it is pure client passthrough.
