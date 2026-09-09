# ADR 0015 — Lazy avatar loading with a client-computed blur placeholder

Status: Superseded by ADR 0016
Date: 2026-09-06

## Context

Every avatar shown in the UI (chat-list rows, headers, message-bubble sender
icons, members list) currently binds `<img src>` to the resolved public avatar
URL the moment it mounts. Opening the app with a long chat list fetches dozens
of small images that are mostly glanced at, never studied.

This is the same problem ADR 0014 solved for message media. We apply the same
solution to avatars.

Constraints:
- **The app server never touches image bytes** (storage design principle) — it
  cannot generate thumbnails.
- No DB migrations (`init_db.py` `ADD COLUMN IF NOT EXISTS` pattern).
- No build step / no CDN (CSP `script-src 'self'`).
- Avatars are **not** content-addressed / deduped (ADR 0010 explicitly excludes
  them), so unlike message media there is no blob row to backfill a hash onto.

## Decision

### Placeholder format: ThumbHash

Reuse the ThumbHash vendor lib from ADR 0014 (`poc/vendor/thumbhash.js`,
`window.ThumbHash`). ~25 bytes → base64 ≈ 33 chars, stored as a nullable `TEXT`
column. Decoder yields an RGBA bitmap painted to a tiny `data:` URL.

### Computed by the uploader's browser, at avatar-set time

The client already has the decoded image it just picked. Before committing the
avatar it draws it into a ≤100 px canvas → `rgbaToThumbHash` → base64 and sends
that string in the commit body. Any failure just omits the field — the avatar
degrades to today's eager-load behaviour.

### Storage: one column per avatar owner

- `User.profile_pic_blur_hash TEXT` (nullable).
- `Chat.profile_pic_blur_hash TEXT` (nullable, group avatars).
- No `media_blob`-style backfill: avatars are not deduped.
- Cleared to `NULL` whenever the avatar is cleared.

### Transport

- `AvatarCommitIn` (user + group) and `GroupCreateIn` gain optional `blur_hash`.
- `avatar_service` validates it as untrusted input: `len ≤ 64`, charset
  `^[A-Za-z0-9+/=]*$`. A bad value is dropped, not fatal (cosmetic).
- Echoed on `UserOut` / `ChatOut` as `profile_pic_blur_hash` (plain passthrough,
  no URL resolution) and on the transient `profile_updated` WS event.
- The resolved `profile_pic_url` keeps being attached — it is only a URL, no
  bytes. The frontend just does not put it in `<img src>` until the avatar is
  tapped.

### Frontend

- `Avatar.js` renders, in priority order: (1) the decoded blur when a hash is
  present — real URL never loaded; (2) today's eager `<img loading="lazy">` for
  legacy hash-less rows; (3) the coloured-initial circle.
- Tapping **any** avatar opens a full-screen lightbox that shows the blur
  immediately and downloads the real full-res image once, fading it in. Close
  on backdrop / Esc.
- `thumbHashToDataUrl` is reused from `useMediaPlaceholder.js` (already generic).

## Consequences

- Additive, nullable columns; old clients and pre-feature rows keep working
  (they load eagerly, no hash).
- UX cost: full-res avatar needs one tap. Accepted; an "always load avatars"
  setting is a natural follow-up.
- No server-side image processing, no storage-GC change.
- Work plan: `AVATAR_BLUR_PLACEHOLDER_PLAN.md`.
