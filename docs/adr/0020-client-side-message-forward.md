# ADR 0020 — Message forwarding is a client-side re-send (no backend change)

**Status:** Accepted
**Date:** 2026-09-07

## Context

The message context menu has always shown a disabled "Forward 🔒" row. We want
it to work: pick one or more of the user's conversations (private + groups) or a
person reached by exact username / phone, and copy the message there —
WhatsApp-style.

The backend has no forward concept and no `is_forwarded` column. Adding an
endpoint + schema field is possible but the PoC only needs the behaviour, and
the existing `send_message` WS action already does everything required:

- **Text:** a forward is a new `send_message` with the same `content`.
- **Media:** `_validate_media` (ADR 0010) accepts any `media.key` that resolves
  to a `media_blob` row and HEAD-validates in storage; it then bumps
  `ref_count` and reuses the blob. The original message's presigned `media_url`
  contains that object key in its path, so the client can recover the key and
  forward media **without re-uploading a byte**. A missing `blur_hash` is
  backfilled from the blob (ADR 0014).

## Decision

Implement forwarding entirely in the PoC frontend:

- `poc/composables/useForward.js` — picker state, the exact-user search (shared
  routing with New chat: `+`/digits → `/users/by-phone`, else
  `/users/by-username`), and `confirmForward()`.
- `poc/components/ForwardModal.js` — multi-select list (round check, no
  checkboxes) of the user's chats filtered by client-side substring match, an
  exact "People" hit on top, a Send footer.
- `mediaKeyFromUrl(url)` recovers the storage key from a presigned GET:
  `pathname` minus a leading path-style bucket segment. Key shape stays
  `{h2}/{kind}/{id}{ext}`, so a leading 2-hex segment ⇒ virtual-hosted (keep),
  otherwise drop the first segment (dev MinIO path-style).
- `confirmForward()` creates a private chat (`POST /chats/private`) for each
  picked person, then sends one `send_message` frame per target chat, paced
  ~500 ms apart to stay under the WS send limiter (3/1 s). The active chat also
  gets an optimistic bubble; other chats reconcile silently via their
  `new_message` echo. Single target that isn't open ⇒ jump to it.
- **No "Forwarded" label** — that needs a backend field; deferred.
- **No outbox** — forwards use `sendRaw` directly (like the media send path);
  the outbox reconciles only the active chat's bubbles.

Also in this change: the New-chat / New-group / Forward search debounce drops
from 3 s / 2 s to **1.5 s**.

## Consequences

- Zero backend change; `ref_count` on a forwarded blob is correctly bumped, so
  a future media GC still sees the extra reference.
- A media message whose key can't be recovered (blob: URL only, e.g. an
  in-flight own send) is not forwardable — the menu entry is hidden and
  `openForwardModal` toasts.
- If the backend later grows a real forward endpoint / `is_forwarded` flag,
  `confirmForward` swaps its send loop for that call; the picker UI is unaffected.
