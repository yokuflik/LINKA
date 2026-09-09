# ADR 0014 — Lazy media loading with a client-computed blur placeholder

Status: Accepted
Date: 2026-09-06

## Context

Opening a chat with image/video attachments currently fetches every object's
bytes eagerly (`<img src>` / `<video preload="metadata">` bound to a presigned
GET the moment the bubble mounts). At scale that is wasted bandwidth — most
media in a scrolled-past history is never looked at.

We want each media bubble to render an instant, tiny, blurred preview from data
carried in the message row, and fetch the real object only on an explicit tap.

Constraints:
- **The app server never touches file bytes** (storage design principle,
  `.claude_docs/storage_and_media.md`) — so it cannot generate thumbnails.
- No DB migrations (`init_db.py` `ADD COLUMN IF NOT EXISTS` pattern).
- No build step / no CDN in the PoC (CSP `script-src 'self'`).

## Decision

### Placeholder format: ThumbHash

Use **ThumbHash** (https://github.com/evanw/thumbhash, MIT) as the placeholder
string, vendored at `poc/vendor/thumbhash.js`:

- ~20–32 bytes → base64 ≈ 28–44 chars, stored as a nullable `TEXT` column.
- Encodes aspect ratio + average color + a very low-frequency image → we can
  size the reserved bubble box from the hash and drop the current
  portrait/landscape guess + `probeMediaOrientation` round-trip for
  hash-bearing messages.
- Pure-JS encoder and decoder; decoder yields an RGBA bitmap painted to a tiny
  canvas / `data:` URL.

Chosen over BlurHash: smaller, carries aspect ratio and alpha, and the decode
is cheaper.

### Computed by the sender's browser, at send time

- **Image**: draw into a ≤100 px canvas → `rgbaToThumbHash` → base64.
- **Video**: detached `<video>`, seek to `currentTime = 0`, `seeked` → draw the
  frame → same as image.
- **File / audio**: no hash (files have no visual; voice notes already render a
  waveform and only fetch bytes on play).
- Any failure (cross-origin, codec, no canvas) just omits the field — the
  bubble degrades to today's eager-load behavior.

### Storage: message column + blob backfill

- `Message.media_blur_hash TEXT` (nullable).
- `MediaBlob.blur_hash TEXT` (nullable). On a deduped re-send / forward
  (`already_uploaded` / known `sha256`) the client may not recompute the hash;
  `send.py` backfills `media_blur_hash` from `media_blob.blur_hash`, and
  `crud_media_blob.confirm_and_ref` stores the hash on the blob the first time
  it is seen. A viral forwarded image keeps its preview for free.

### Transport

- WS `send_message` `media` object gains optional `blur_hash`.
- `_validate_media` validates it as untrusted input: `len ≤ 64`, charset
  `^[A-Za-z0-9+/=]*$`; attaches to `MediaAttachment`.
- Echoed everywhere the other `media_*` fields already are: `new_message`
  event, `MessageOut` / history, `message_restored`, `routers/schemas.py`.
- The presigned `media_url` keeps being attached at read time — it is only a
  URL, no bytes. The frontend just does not put it in `<img src>` until the
  bubble is tapped. A mint-on-demand `GET /messages/{id}/media-url` endpoint is
  **deferred** (not needed for the bandwidth goal).

### Frontend

- `poc/composables/useMediaPlaceholder.js`: `thumbHashToDataUrl(hash)` +
  `thumbHashToAspect(hash)`, memoized.
- Image / video bubbles show the blurred box immediately; the real
  `<img>` / `<video>` is gated on a per-message `_mediaOpened` flag set by a
  tap handler. `probeMediaOrientation` / `carryOverImageOrientation` are
  skipped when a hash is present (kept as the fallback for legacy rows).

## Consequences

- Additive, nullable columns; old clients and pre-feature rows keep working
  (they load eagerly, no hash).
- UX cost: one extra tap to view media. Accepted for now; an
  "auto-download over Wi-Fi" setting is a natural follow-up.
- No server-side image processing, no storage-GC change, no blur for PDFs.
- Work plan: `MEDIA_BLUR_PLACEHOLDER_PLAN.md`.
