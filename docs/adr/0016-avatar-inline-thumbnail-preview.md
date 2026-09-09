# ADR 0016 — Avatar placeholder is a real inline thumbnail, not a blur

Status: Accepted
Date: 2026-09-06
Supersedes: ADR 0015 (avatar half only — message-media blur, ADR 0014, is unchanged)

## Context

ADR 0015 gave every avatar a ThumbHash blur placeholder: the uploader's browser
encoded a ~25-byte ThumbHash, the app stored it, and `<Avatar>` rendered the
decoded blur *only* — the real image was never loaded except in a full-screen
lightbox on tap.

In practice the blur is unrecognisable at avatar size (36–40 px). A chat list of
blurred blobs is worse than useless — you cannot tell contacts apart. The user
asked for a small but *real* version of the picture that still reads correctly.

## Decision

Replace the ThumbHash blur with a genuine downscaled thumbnail carried inline as
a `data:` URI.

### Format

- The uploader's browser downscales the picked image to a **64 px** longest edge,
  re-encodes it as **JPEG q0.7**, and takes the resulting `data:image/jpeg;base64,…`
  string (typically 1–3 KB).
- Stored verbatim in a nullable `TEXT` column. No server-side image processing
  (storage design principle unchanged — the app never touches real image bytes,
  and a 64 px client-made thumbnail is not "real bytes" in that sense: it is a
  cosmetic, client-authored, self-describing string, same category as the old
  ThumbHash).

### Storage — renamed column, no back-compat

- `User.profile_pic_blur_hash` → **`User.profile_pic_preview`** (`TEXT`, nullable).
- `Chat.profile_pic_blur_hash` → **`Chat.profile_pic_preview`** (`TEXT`, nullable).
- This is **mock data** — `init_db.py` drops and recreates. The old column is
  simply removed from the `ADD COLUMN IF NOT EXISTS` list; no migration, no
  dual-read. Existing dev rows are discarded; the user recreates test users.

### Transport

- `AvatarCommitIn` / `CreateGroupChatIn` field renamed `blur_hash` → `preview`
  (`avatar_blur_hash` → `avatar_preview` for group create).
- `avatar_service._clean_preview` validates untrusted input:
  `len ≤ MAX_AVATAR_PREVIEW_LENGTH` (config, default 8192) and the string must
  start with `data:image/`. A bad value is dropped, not fatal (cosmetic).
- Echoed on `UserOut` / `ChatOut` as `profile_pic_preview` and on the transient
  `profile_updated` / `chat_updated` WS events.
- `profile_pic_url` (presigned/public URL, no bytes) keeps being attached for the
  tap-to-enlarge lightbox.

### Frontend

- `<Avatar>` renders, in priority order: (1) `preview` set → `<img :src="preview">`
  **shown directly** — a real, if small, picture; (2) no preview but `url` set →
  legacy eager `<img loading="lazy">` (seed / pre-feature rows); (3) coloured
  initial.
- The chat-list / header / bubble avatars now show a recognisable picture with
  **zero** network fetches for the thumbnail (it rode in on the JSON).
- Tapping an avatar still opens the lightbox, which downloads the full-res
  `profile_pic_url` once. The lightbox uses the `preview` as its instant backdrop
  instead of the blur.
- `useMediaPlaceholder.encodeImageBlurHash` → `encodeAvatarPreview(file)`
  (downscale to 64 px → JPEG data URL). ThumbHash vendor lib is still loaded for
  message media (ADR 0014) but is no longer used for avatars.

## Consequences

- Avatars are recognisable at a glance again.
- Payload cost: chat-list JSON grows ~1–3 KB per distinct avatar (vs ~45 B for a
  ThumbHash). Acceptable for a contact list; still far cheaper than fetching N
  avatar objects.
- One renamed column, mock data wiped — no migration path by design.
- Message-media blur (ADR 0014) is untouched.
