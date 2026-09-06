# Frontend (PoC)

Read this before editing anything under `poc/`.

There is **no real client app** — only a single-file HTML/Vue PoC (`poc/`) for manual testing.

## Structure
- Single-file entry `poc/index.html` (~294 lines after refactor).
- **`useChats.js` is a facade** (~15 lines) over five sub-domain modules,
  loaded/merged in this order before it: `useChatStore.js` (all shared chat
  reactive state + pure helpers: `sortChats`, mute/tick helpers, status/role
  formatters, consts), `useChatMembers.js` (`/chats/{id}/members` resolvers,
  name/label + system-message helpers, `activeChat*` label/avatar computeds),
  `useChatList.js` (`GET /chats`, unread seeding, pin/mute mutations),
  `useChatOpen.js` (`selectChat` cache+network paths, scroll/pin-to-bottom,
  keyset paging, draft chats, focus/reconnect refresh, write-through cache
  watch), `useChatMenu.js` (sidebar chat-row right-click menu). Public API
  (everything on `ctx`) is unchanged.
- Template markup split into `poc/components/*.js` (`Vue.defineComponent` objects, no build step).
- `setup()` logic split into `poc/composables/*.js` (`useX(ctx)` factories merged onto one shared `ctx`).
- Plan/progress in `poc/composables/REFACTOR_PLAN.md`.

## Running
- Open `poc/index.html` directly (CORS wide open, dev-only). OTP codes print to server console — no real SMS/FCM.
- `uvicorn --reload` drops every WebSocket on each `.py` save; the PoC auto-reconnects (~3s). Not a bug.

## Hard rules (also in root CLAUDE.md)
- **NEVER display the raw `user_id`** in the UI (chat lists, message bubbles, headers). Show the peer's server `display_name`, falling back to `phone_number`. There is **no client-side contact book** (the old `MOCK_CONTACT_NAMES` map is gone). `user_id` is strictly for backend logic / API calls.
- **No autonomous visual testing** — no browser tools, Puppeteer, screenshots, or local servers to verify the UI. The user tests manually and reports back.
- **After editing `poc/index.html`, syntax-check its inline `<script>`** — one error (e.g. a duplicate `const`) silently kills the whole PoC:
  ```
  python3 -c "import re; open('/tmp/i.js','w').write(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>', open('poc/index.html').read(), re.S)[0])"
  node --check /tmp/i.js
  ```
  `poc/components/*.js` and `poc/composables/*.js` can be `node --check`ed directly.
- **Never chain multiple `$emit(...)` calls with `;` in an inline template expression** — always use a real method.
- Never `parseInt()`/`Number()` a Snowflake id — they cross the wire as JSON strings.

## Live-update event handling (`poc/composables/useWsRouter.js`)
- `chat_updated` → patch `chats.value[…].chat` in place (no `loadChats`).
- `profile_updated` → merge into `userById` + the matching `groupChatMembers` row.
- `role_changed` → client filters to `actor_id`/`target_id` only and builds the human-readable line itself.
- `read_receipt`/`delivery_receipt` → `refreshMessageStatuses(chatId)` (`useChatMeta`) re-pulls `GET /chats/{id}/messages?limit=50` and copies `status` onto loaded bubbles. **Coalesced + generation-guarded**: a burst of receipt events (other side reading N messages in a row) is debounced to one trailing fetch after 250ms, and each fetch carries a generation counter so a slow older response can't clobber the final state (this was why the last 1-2 ticks in a 16-message burst didn't update to ✓✓).
- **Buffered live messages for a not-open chat** (`useWsRouter` `pendingChatMessages` map, `takeBufferedMessages`): a `new_message` for a chat that isn't `activeChatId` used to be dropped from the live list (only counted for the unread badge). It's now also buffered (cap 200/chat). `useChats.selectChat` calls `mergeBuffered()` on **both** the cache-hit and network paths — dedupe by `id`, re-sort by id (Snowflake ids are fixed-width, lexical = chronological) — and the network path also folds back messages pushed live *during* the `GET` (which the history assignment would otherwise wipe) plus uses the merged-newest id for the delivered/read catch-up. Fixes: opening a chat mid-burst on the receiver losing the messages that arrived before the switch, even after refresh.
- `message_restored` → copies `msg.media_blur_hash` back onto the row, then `carryOverImageOrientation(oldUrl, newUrl)` (delete stashes the pre-delete URL on `m._deletedMediaUrl`; restore copies its already-probed orientation onto the fresh presigned URL so the first paint isn't the 'landscape' default), then re-runs `probeMediaOrientation(url, kind, m.media_blur_hash)` on the new URL to confirm (no-op for hash-bearing rows — see ADR 0014 below).
- Clients dedupe messages by `message_id`; filter out their own echoed events.

## Auth / phone verification (ADR 0009)
- `composables/usePhoneInput.js` — worldwide country-code picker + a **loose** E.164 validator (regex `^\+[1-9]\d{7,14}$` + per-country national-length hints in `poc/data/country-codes.js` `COUNTRY_NSN_LEN`; deliberately **not** libphonenumber-js). Exposes `phoneCountries`, `phoneCountry`, `phoneRawInput`, `phoneE164`, `phoneIsValid`, `phoneIsWhitelisted`, `resolvedPhone`, `resetPhoneInput`. Merged into `ctx` **before** `useAuth`.
- **Dev whitelist:** raw input of exactly `1`–`5` → `phoneIsWhitelisted`, bypasses the validator, and `useAuth` routes it through `POST /auth/otp/request` + `/auth/otp/verify`. The backend short-circuits these numbers (no code stored, verify skips straight to login) — any code entered works. Every other number now needs the real SMS code (the open OTP stub is closed, ADR 0009).
- **Real numbers** → `useAuth.requestOtp` calls `firebase.auth().signInWithPhoneNumber(e164, invisibleRecaptcha)` (SMS client-side); `verifyOtp` does `confirmationResult.confirm(code)` → `getIdToken()` → `POST /auth/firebase/verify {id_token}` → `finishLogin(body)` (shared token-store + register-profile + avatar-upload + `connectWebSocket` tail), then `firebase.auth().signOut()`.
- Firebase SDK loaded from `gstatic.com` in `index.html` (compat build) + `firebase-config.js` (public config, not a secret) + `firebase-init.js` (sets `window.firebaseAuth`, or `null` when offline — the `1`–`5` path still works). `#recaptcha-container` lives in `AuthScreen.js`.
- `AuthScreen.js` no longer has a `phoneNumber` prop; it takes the `phone*` props + `update:phoneRawInput`/`update:phoneCountry` and shows a `<select>` country picker + prefixed number input. No channel radio — stub vs Firebase is decided purely by the `1`–`5` whitelist check.
- `useAuth` still exposes a `phoneNumber` ref (synced from `resolvedPhone` at request time) for lifecycle code that destructures it.

## Settings (privacy)
- `composables/useSettings.js` — `GET`/`PATCH /users/me/settings`, `userSettings` ref, `ONLINE_VISIBILITY_OPTIONS`, `loadSettings`/`saveSettings`/`resetSettings`, **plus the ⚙ Settings-modal state**: `showSettingsModal`, `settingsForm` (`{privacy_online, privacy_read_receipts}`), `settingsBusy`, `settingsError`, `openSettingsModal`, `submitSettings` (PATCHes a partial `{privacy:{...}}` with only changed keys).
- **Two separate top-bar entry points** (`AppHeader.js`): the name/avatar button `@edit-profile` → `openProfileModal` (**ProfileEditModal**: name/about/avatar only, **no settings**); the ⚙ gear button (outline SVG, WhatsApp-style, matches the mic icon) `@open-settings` → `openSettingsModal` (**SettingsModal**: privacy only).
- **SettingsModal** (`components/SettingsModal.js`): "who can see when I'm online" `<select>` bound to `settingsForm.privacy_online` (note: also governs last-seen) + "Read receipts" checkbox bound to `settingsForm.privacy_read_receipts`.
- `ProfileEditModal` no longer takes `onlineVisibilityOptions`; `useProfileEdit.profileForm` is `{display_name, about_text}` only; `saveProfile` no longer touches settings.
- One presence-privacy control only (`privacy.online`); it governs both the online indicator and last-seen. No separate `last_seen` privacy setting. `privacy.read_receipts` wired to the ADR-0003 backend setting.
- **Presence re-check:** `useWebsocket`'s heartbeat calls `usePresence.refreshPresenceSubscription()` — re-sends `subscribe_presence` for the open private chat so the server re-runs the `privacy.online` gate. A `presence_revoked` push (`useWsRouter` → `usePresence.onPresenceRevoked`) clears the cached status when the other user hides it or the shared chat is gone.
- **Last seen:** `useWsRouter` stores `{status, last_seen_at}` per user from `presence_status`/`presence_update`. `usePresence.presenceLabelFor` shows `'online'` when connected, else `'last seen <relative>'` (via `formatLastSeen`) when a stamp exists, else nothing — so last-seen is only ever displayed for an offline user. The label refreshes ~every 30s because the heartbeat re-subscribe triggers a fresh `presence_status` with an updated `last_seen_at`. Rendered by `ChatHeader` (`activeChatPresenceLabel`), typing indicator still takes priority.

## Peer name resolution
- `display_name || phone_number`, everywhere. No contact book, no `contactDisplayName` helper.
- `useChats.resolvePrivateChatTitle` stores `other.user.display_name || other.user.phone_number` in `privateChatTitles[chatId]`; `chatDisplayName` / `chatAvatarName` read it straight. `force:true` re-pull (chat open / tab focus) picks up the peer's renamed `display_name` since there's no server push for a profile edit.
- `senderLabel` / `userLabelById` / `memberDisplayName` / draft-chat computeds all do `user.display_name || user.phone_number` directly.

## Lazy message cache (`composables/useMessageCache.js`)
- Front-end only, no backend changes. Per-chat `localStorage` snapshot of the newest page, key `linka_msgcache_<userId>_<chatId>`, `{v:SCHEMA, messages:[oldest-first]}`, capped `MAX_CACHED=60`.
- `useChats.selectChat`: on a cache hit it renders `ctx.loadChatMessages(chatId)` and **returns before any `/chats/{id}/messages` fetch** — still runs `probeLoadedImageOrientations`, `scrollMessagesToBottom`, and the delivered/read receipt catch-up on the cached newest id. `hasMoreMessages` = `cached.length >= MESSAGE_PAGE_SIZE`.
- Freshness: network loads call `ctx.saveChatMessages`, and a `watch(messages, …, {deep:true})` in `useChats` write-through-persists the open chat on every change (live `new_message`/edit/delete, optimistic send). "Load older" pages are fetched from the API and not separately persisted (the watch just keeps the newest 60).
- `useAuth.logout` calls `ctx.clearAllMessageCache()` (wipes every `linka_msgcache_` key). API: `loadChatMessages`/`saveChatMessages`/`clearChatMessages`/`clearAllMessageCache`.

## Pinned chats
- `useChats.togglePinChat(chatId)` — optimistic flip of `item.pinned` + `sortChats()`, then `PUT`/`DELETE /chats/{id}/pin`, rolls back on failure.
- **Multi-device:** the server echoes `chat_pin_changed {chat_id, pinned}` on the personal channel; `useWsRouter` applies the flag + `sortChats()` in place (guarded on an actual change, so the acting tab's echo is a no-op).
- `sortChats()` mirrors the server order (pinned first, then `last_message_at` desc, then id desc); called in `loadChats`, after every toggle, and by `useChatMeta.bumpChatPreview` (which previously re-sorted by activity only, dropping pinned chats down on each new message).
- `ChatSidebar` row is a `<div>` with `@contextmenu.prevent` → `@chat-contextmenu {chatId, event}`. A small 📌 shows next to the timestamp when `item.pinned`.
- `ChatContextMenu.js` — right-click menu on a chat row: "Pin to top"/"Unpin" + mute. Same fixed-overlay pattern as `MessageContextMenu`. State in `useChats`: `chatMenuChatId`/`chatMenuItem`/`chatMenuPosition`, `openChatContextMenu`/`closeChatContextMenu`/`togglePinFromMenu`.

## Muted chats
- Backend: `Participant.muted_until` + `PUT`/`DELETE /chats/{id}/mute` (ADR 0004). Server only suppresses offline push; the client does the visible part.
- `ChatContextMenu` mute: when not muted, "Mute" expands to presets (8 hours / 1 day / 1 week / Forever) + "Custom…" with a `datetime-local` picker (any absolute expiry — the server takes any `muted_until`). When muted, one "Unmute" entry with the expiry as a hint.
- `useChats`: `MUTE_PRESETS`, `muteChat(chatId, presetKey)` / `muteChatUntil(chatId, iso)` / `unmuteChat(chatId)` (all optimistic on `item.muted_until`, roll back on failure), `isChatMuted(item)` (future-expiry check), `mutedUntilLabel(item)` ('' for a year-9999 "forever"). Menu handlers: `mutePresetFromMenu` / `muteUntilFromMenu` / `unmuteFromMenu`. "Forever" = ISO `9999-12-31T23:59:59Z`.
- **Multi-device:** server echoes `chat_mute_changed {chat_id, muted_until}` on the personal channel; `useWsRouter` sets `item.muted_until` in place (acting tab's echo is a no-op).
- `ChatSidebar` shows a 🔇 icon next to the timestamp when `isChatMuted(item)`, and greys the unread badge (still shown, just `bg-slate-400`). Takes an `isChatMuted` fn prop.

## Draft private chats
- Opening a private chat from the sidebar no longer POSTs `/chats/private`. `useNewChat.createPrivateChat` resolves the phone → user and calls `useChats.openDraftChat(user)`, which sets `draftChat = {otherUserId, phone, user}`, nulls `activeChatId`, and subscribes to presence **by user id** (the `subscribe_presence` gate is the target's `privacy.online`, not a shared chat — so `everyone` resolves with no chat row; `contacts` won't).
- `activePaneVisible` (`activeChatId || draftChat`) drives the main pane; the `activeChat*` label/avatar/presence computeds all fall back to `draftChat` values. Header shows name + online / last-seen.
- First text send: `useMessageSend.sendMessage` awaits `useNewChat.commitDraftChat()` (POST `/chats/private` → `loadChats` → `selectChat`) then sends normally. Media/voice are blocked in draft mode ("Send a message first…").
- Leaving without sending: `selectChat` (any real chat) and `logout` call `discardDraftChat` / clear `draftChat` + `unsubscribeFromPresence`. Nothing is created server-side.
- If a real chat with that user already exists, `createPrivateChat` just opens it (no draft).

## Image / video bubbles (`components/MessageList.js`) + blur placeholder (ADR 0014)
- **Lazy media loading:** the sender's browser computes a **ThumbHash** (`poc/vendor/thumbhash.js`, MIT, `window.ThumbHash` global) of the image / video first frame at send time (`useMediaUpload.computeImageBlurHash` / `computeVideoBlurHash` → ≤100px canvas → `rgbaToThumbHash` → base64), carried on WS `send_message` `media.blur_hash` → nullable `Message.media_blur_hash` (+ `MediaBlob.blur_hash`, backfilled on dedup). Any failure just omits the field.
- **`composables/useMediaPlaceholder.js`** (merged after `useMediaUpload`): `thumbHashToDataUrl(hash)` (blurred PNG `data:` URL) + `thumbHashToAspect(hash)` (w/h ratio), each memoized in a `Map`. Both return `null` on a decode failure.
- **Rendering:** for a row **with** a hash, the bubble box is sized from `thumbHashToAspect` (`mediaBoxStyle`), shows the blurred `data:` URL immediately, and **withholds the real `<img>`/`<video>` src until downloaded** — `openedMedia` reactive set + `isMediaOpened(m)` (true when no hash → legacy eager, or once `openMedia(m)` fires). Both image and video overlay a **download button** on the blur: `⭳ <formatBytes(media_size)>` (falls back to "Download" when size is unknown), spinner while fetching. `downloadMedia(m)` `fetch`es the presigned `media_url`, turns the response into a `blob:` object URL stored in `downloadedBlobUrl[key]`, then `openMedia(m)`. The opened `<img>`/`<video>` bind `mediaSrc(m)` (the blob URL if downloaded, else `media_url`) so the browser never sees S3's `Content-Disposition: attachment` and never triggers a "save as" / auto-download. Video has no autoplay and `controlslist="nodownload noremoteplayback"`. Post-open, the old spinner/`imageLoaded[media_url]` fade-in still applies to the image.
- **Legacy rows (no hash):** unchanged — fixed two-shape reserved box (portrait `w-48 aspect-[3/4]` / landscape `w-64 aspect-[4/3]`), eager load, `probeMediaOrientation` still runs. `probeMediaOrientation(url, kind, blurHash)` early-returns when `blurHash` is set (probing would fetch the full object).
- File / audio bubbles: no hash, unchanged. No blur for PDFs.
- `useMessageCache.SCHEMA` bumped 1 → 2 so stale snapshots refetch once (the field rides along in the stored message object). `useWsRouter` `new_message` / `message_restored` carry `media_blur_hash`.
- Follow-up (deferred): an "auto-download over Wi-Fi" setting; default is tap-to-load for everyone.
- **Sender never re-downloads their own media, and sees it instantly.** `useMediaUpload.sendMediaMessage` / voice send push the optimistic bubble **first — before any slow work** (image downscale, ThumbHash, sha256, upload-ticket, PUT). `media_url` is a local `URL.createObjectURL(file/blob)`, `pending:true` (🕓), marked `_localMediaUrl`. The slow work then runs in the `try`; on success `sendRaw` fires and the bubble reconciles via the normal `new_message` echo → sent/delivered/read ticks (`statusTickSymbol`). On any failure the bubble flips to ⚠️ (`send_failed`). Oversize non-image = hard stop up front; an oversize photo is downscaled inside the `try` (bubble already visible), and still-too-big throws → ⚠️.
- `useWsRouter` `new_message` reconcile keeps the blob URL for display and stashes the presigned GET on `media_url_remote`. `MessageList.isMediaOpened` treats `_localMediaUrl` rows as already-opened (no "tap to view" over your own photo; video autoplay suppressed). `useMessageCache.saveChatMessages` swaps `media_url_remote` back in and drops the local-only markers when persisting (blob: URLs die on reload), so a later session loads the real URL normally. `useOutbox.retryFailedMessage` ignores non-text rows (media retry = re-pick the file).

## Message context menu (`components/MessageContextMenu.js`)
- Desktop: `@contextmenu.prevent` on the bubble → `message-contextmenu {message, event}` → `useMessageMenu.openMessageContextMenu` (reads `event.clientX/clientY`).
- Touch: `MessageList` setup runs a **long-press** (`450ms`, `10px` move tolerance) on the same bubble via `@touchstart/move/end/cancel`, emitting the same event with a synthetic `{clientX, clientY}` from the touch point (`navigator.vibrate(10)` haptic). A move/scroll or early lift cancels; the trailing tap is `preventDefault`ed so it doesn't also fire the bubble's normal tap.

## Composer buttons (`components/MessageInput.js`)
- Left→right: `[+]` attach menu (Photos&Videos / Documents), text input / Send, **camera** (line-art icon; **tap** `openCamera` → hidden `<input accept="image/*" capture="environment">`, **long-press 450ms** `openVideoCamera` → hidden `<input accept="video/*" capture="environment">` — both `pick-media`, same pipeline as a picked image/video; the trailing click after a long-press is suppressed), mic toggle.
- Camera + mic share the plain black stroke SVG style, no button background. Camera is hidden while recording.

## Send flow
- PoC is optimistic: renders the bubble immediately, reconciles when `new_message` arrives echoing its `client_message_id`.
- Server ACK for `send_message` is `{"type":"ack","for":"send_message","status":"queued"}` — no `message_id`/`created_at` (async send queue).

## Outbox / paced sender (`composables/useOutbox.js`, frontend-only, no backend change)
- **Every text `send_message` goes through the outbox** (online too), not just offline sends. `useMessageSend.sendMessage` renders the optimistic bubble (`pending:true` → 🕓) then `enqueueOutgoing(payload)` — never `sendRaw` directly. Draft-chat first send and message edits still require an open socket (need the network / not queued).
- **Why:** the server's WS `send_message` primary limit is 3/1s. Firing 6 bubbles in a row used to silently lose frames 4-6 — the server replied `{type:"error",code:"rate_limited",client_message_id}` and the old client just `logError`'d it, leaving the bubble stuck on 🕓 forever. This was a bug **before** the outbox existed.
- **Single drain loop** (`drainLoop`): pops one frame at a time, `sendRaw`, waits `SEND_INTERVAL_MS` (450ms ≈ 2.2/s). Parks when `!wsIsOpen()`; resumed by `kickDrain()` on enqueue and from `useWebsocket` `ws.onopen`.
- **Error handling** (`useWsRouter` `type:"error"` branch → `ctx.onSendError(code, cmid)`): `rate_limited` / `internal_error` → requeue at the head with **exponential backoff** (`RATE_LIMIT_BACKOFF_MS=1500 · 2^(n-1)`, capped `RATE_LIMIT_BACKOFF_MAX_MS=20000`, `rejectCount` per cmid) — the bubble **stays on 🕓** (not ⚠️) while retrying, because the server's `send_message_burst` window is **40/60s** so a big batch legitimately needs up to ~a minute to drain. Other codes → mark the bubble ⚠️. Every `_dispatch` error frame **and the receive-loop frame-flood drop** echo `client_message_id` when the payload had one (backend change — see `.claude_docs/backend_services_and_api.md`), so every reject path reconciles.
- **Ack timeout** (`ACK_TIMEOUT_MS=90000`): armed per sent frame, **left running across `rate_limited` requeues** as the hard give-up ceiling; if nothing resolves the frame in 90s it's flagged ⚠️.
- Entries leave the queue / `inFlight` map only via `removeFromOutbox` (`useWsRouter` calls it on `new_message` reconcile, `message_failed`, `message_already_sent`). Server dedupes any double-send by `client_message_id` so requeue is always safe.
- `window` `online` listener in `useWebsocket` forces an immediate reconnect (skips the 3s timer).
- Banner above the composer (`showOutboxBanner` = queue non-empty **and** socket not open): "No connection — N message(s) will send when you're back online."
- **Tab close = lost.** Queue is never persisted. But the optimistic bubbles are written through to the message cache with `pending:true`; on next load `selectChat`'s cache-hit path calls `markStalePendingAsFailed()` → those become ⚠️ "not sent" bubbles. Tapping the ⚠️ (`MessageList` `@retry-message` → `retryFailedMessage`) rebuilds the frame and re-queues.
- `logout` calls `clearOutbox()` (drops queue + timers + flags lingering bubbles).
