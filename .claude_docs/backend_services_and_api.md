# Backend Services, Routers & API

Read this before touching `services/`, `routers/`, auth, chat/group membership, or system messages.

## Stack
FastAPI (REST + WebSocket) · PyJWT · Pydantic v2 · pytest/pytest-asyncio/httpx.

## Layout
- `services/` — `auth_service`, `user_service`, `chat_service`, `message_service`, `presence_service`, `realtime_service` (Redis pub/sub), `rate_limit_service`, `notification_service` (push stub), `avatar_service`, `connection_manager` (per-process WS state).
- `services/messaging/` — the messaging domain, split from the old monolithic `message_service.py`. `message_service.py` is a **thin facade** re-exporting the public API (import from it as before). Modules:
  - `errors.py` (exception types)
  - `common.py` (`SYSTEM_MESSAGE_TYPE`, `_check_content_length`)
  - `media_validation.py` (`_validate_media`, `MediaAttachment`)
  - `send.py` (`process_outgoing`, `send_system_message`, `fan_out_message`, idempotency)
  - `edit_delete.py` (`edit_message`, `delete_message`)
  - `read_api.py` (`get_message_history` — attaches derived status + presigned `media_url`)
  - `receipts.py` (`mark_as_delivered/read/played`, `get_message_receipts`, detailed-log enqueue)
  - Tests monkeypatch `message_service.MAX_MESSAGE_CONTENT_LENGTH` / `RECEIPT_NAMED_LIST_MAX_MEMBERS` on the facade — submodules read those back off the facade at call time, so keep them importable there.
- `services/storage/` — see storage_and_media.md.
- `services/settings/` — per-user settings. `schema.py` (`DEFAULT_USER_SETTINGS`, `validate_patch`, `merge_with_defaults`, `apply_patch`), `service.py` (`get_user_settings` / `update_user_settings`), `errors.py` (`SettingsValidationError`). Storage = `user_settings.settings` JSONB (see database_schema.md, ADR 0002). Endpoints `GET`/`PATCH /users/me/settings` in `routers/users.py`. Privacy field: `privacy.online`, `everyone`|`contacts`|`nobody` (default `everyone`) — the **single** presence-privacy control, gating the online indicator **and** last-seen together (there is no separate `privacy.last_seen`; it was removed and `merge_with_defaults` prunes the retired key from stale blobs). **Enforced** at presence-subscribe time (`_presence_authorized` in `routers/websocket.py`, using `settings_service.get_online_visibility`): `nobody` → deny; `contacts` → requires a `PrivateChatPair`; `everyone` → any authed user. Not re-checked per push — the client re-subscribes on its heartbeat and a now-disallowed watcher gets `presence_revoked` (see realtime_and_redis.md). Last-seen (`last_seen_at`) only ships over this gated channel, so it needs no separate check; `presence_service` tracks it as the timestamp of the user's most recently connected device (re-stamped on every connect/heartbeat/disconnect — see realtime_and_redis.md). `privacy.online` is **also** enforced on the typing/recording indicator: in a 1:1 chat, if the sender's own `privacy.online` denies the other participant, `_publish_typing` silently drops the event (see realtime_and_redis.md). Second privacy field: `privacy.read_receipts` (bool, default `true`) — blue-tick privacy, **asymmetric / per-reader, 1:1-only** (ADR 0003; same shape as `privacy.online`). Reader R sends READ/PLAYED iff **R's own** setting is `true` — the sender's setting is irrelevant. Helper `services/messaging/receipt_privacy.py`: `reader_hides_read_receipts(chat_id, reader_id)` (fan-out — `mark_as_read`/`mark_as_played` skip publishing `read_receipt`/`played_receipt` when the acting `user_id` hid theirs), `read_receipts_hidden_for_message(chat_id, sender_id)` (read views — keyed on the *other* participant: `get_message_history` masks only `sender_id==viewer` messages, `chat_service.get_chat_list` masks `last_message_status`, detailed `GET .../receipts` zeroes `read`/`played` + empties `*_by` + `pending`=other participant), `mask_status`. Groups (>2) never masked. **Watermarks & detailed-log writes still happen** so the reader's `unread_count` clears — mask is presentation-only. `delivery_receipt` (grey ticks) unaffected. Shortcut: `settings_service.get_read_receipts_enabled`.
- `services/receipts/` — `receipt_log.enqueue_receipt_event` (Redis Stream) + `worker.run_forever` (one task/process, started in `main.py` lifespan).
- `services/fanout/` — see realtime_and_redis.md.
- `utils/id_client.py` — client for the Rust Snowflake ID service (ADR 0011). `async next_id() -> int` is the id-minting entry point for **async** call sites. `config.ID_SERVICE_ADDR` empty → delegates to `utils.snowflake.next_id` (in-process, the default). Set → one **unary** `NextId` gRPC per id (never batched — id timestamp is the `created_at` partition-routing key); RPC error/timeout → local-generator fallback + one-time warning. Lazy shared `grpc.aio` channel; `id_client.close()` called from the `main.py` lifespan shutdown. **Wired:** `auth_service` (user id), `chat_service` ×2 (chat id), `messaging/send` ×2 (message + system-message id) — all now `await next_id()`. **Deliberately NOT wired** (sync call paths, id is not a `created_at` routing key): `receipts/receipt_log._rows_from_entries` (row placed by `occurred_at`), `storage/client.build_object_key` (object-key nonce) — both keep `from utils.snowflake import next_id` with a comment. Stubs `utils/snowflake_pb2*.py` are generated by `scripts/gen_proto.sh` (checked in, no build step) from `proto/snowflake.proto`; deps `grpcio`/`grpcio-tools` in requirements.
- `routers/` — REST (`auth`, `users`, `chats`, `messages`) + `websocket` (`/ws`); thin pass-throughs to services. Error→HTTP mapping centralized in `main.py`.

## Auth
- `auth_service.verify_otp_and_login` enforces `stored_code != code` (ADR 0009 — it was commented out for a long time; historically found re-commented during manual testing, so still worth a glance). The `[STUB] Would SMS OTP …` console print is the only "SMS provider" for non-Firebase numbers.
- **Dev whitelist:** `DEV_AUTH_WHITELIST` (default `{"1","2","3","4","5"}`) — exact phone strings that skip verification entirely, server-side, in both `request_otp` and `verify_otp_and_login`. Not real numbers. `DEV_AUTH_WHITELIST=` (empty) removes the bypass.
- **Firebase Phone Auth (ADR 0009)** — real phone verification. SMS + code check happen client-side (Firebase JS SDK + invisible reCAPTCHA). Server side:
  - `POST /auth/firebase/verify {id_token}` → `LoginOut` (`routers/auth.py`). Thin over `auth_service.verify_firebase_and_login(session, id_token)`.
  - `services/firebase_auth.py::verify_id_token` — **manual** RS256 verification against Google's cached x509 certs (`securetoken@system.gserviceaccount.com`), asserts `aud==FIREBASE_PROJECT_ID`, `iss==securetoken.google.com/<pid>`, non-expired, non-empty `sub`, `auth_time` not future. No `firebase-admin` dep, no service-account file. New dep: `cryptography` (for PyJWT RS256). Any failure → `FirebaseAuthError` → wrapped as `InvalidOTPError` → HTTP 401.
  - `verify_firebase_and_login` trusts the token's `phone_number` claim, rate-limits per phone (`firebase_verify` bucket, `OTP_VERIFY_MAX_ATTEMPTS`), then `_find_or_create_and_issue` (shared tail with `verify_otp_and_login`). **No register/login `intent` pre-check** on this path — Firebase sends the SMS before the server is involved.
  - Config: `FIREBASE_PROJECT_ID` (empty → `FIREBASE_AUTH_ENABLED=False`), `DEV_AUTH_WHITELIST` (default `{1,2,3,4,5}` — exact phone strings the **frontend** routes through the legacy `/auth/otp/*` stub instead; backend has no special-casing for them).

## Group membership / system messages
- Roles: 1 Member, 2 Admin, 3 Owner. `chat_service._require_role`: add/remove need `ROLE_ADMIN`, role change needs `ROLE_OWNER`. Endpoints: `POST /chats/{id}/members`, `PATCH /chats/{id}/members/{user_id}`, `DELETE /chats/{id}/members/{target_user_id}`, `GET /chats/{id}/members`.
- System message: `Message.sender_id = NULL` + `type = SYSTEM_MESSAGE_TYPE` (6), sent via `message_service.send_system_message`, fanned out over the normal `new_message` event. `chat_service._display_name_for` resolves any user named in the text (never embed a raw id).
- **`role_changed` is a private system message**: `content` is JSON (`{kind, actor_id, target_id, new_role}`) not plain text. Fan-out reaches everyone; the client filters to `actor_id`/`target_id` only and builds the line itself. Follow this JSON-content + client-filter pattern for any future per-recipient system message (no structured metadata field on `Message`, no migrations).

### Leaving / removing a member
- One endpoint: `DELETE /chats/{chat_id}/members/{target_user_id}` → `chat_service.remove_member`. `actor == target` is a self-leave (skips role check); otherwise actor needs `ROLE_ADMIN` **and** `target.role < actor.role`.
- **Owner cannot leave a non-empty group without a `new_owner_id` query param** (validated as a member) → else `OwnershipTransferRequiredError` / HTTP 409. Successor promoted + broadcast system message before the owner's row is removed.
- **Removal leaving zero participants deletes the whole chat** (`crud_chat.delete_chat`, cascades to Participant + Message via `ON DELETE CASCADE`).
- `chat_service._notify_removed_from_chat` publishes a `removed_from_chat` personal-channel event; `connection_manager` unsubscribes that user's live connections immediately.

## Profile edits (user + group)
- **User**: `PATCH /users/me` (name/about), `PUT/DELETE /users/me/avatar` — `routers/users.py`, thin over `user_service` / `avatar_service`. Group: `PATCH /chats/{id}` + `PUT/DELETE /chats/{id}/avatar` (admin/owner via `_require_role`).
- **Group detail changes always do two things**: (a) a **persisted system message** — `chat_service.update_group_details` announces name/description changes, `set_group_avatar`/`clear_group_avatar` announce photo changes (guarded on an actual value change); (b) a **transient `chat_updated` event** via `_broadcast_chat_update(session, chat_id)` (`title, about_text, profile_pic_url`) so open clients patch the sidebar/header live.
- **Live propagation of a user profile edit**: `user_service.broadcast_profile_update(session, user_id)` — called from all 3 user endpoints after the write. Resolves `profile_pic_url` like `UserOut` and `publish_event`s a **transient** `profile_updated` event (`user_id, display_name, about_text, profile_pic_url`) once per chat from `get_all_chat_ids_for_user`. **Never a persisted system message** (N chats = N INSERTs + history noise). Best-effort.

## Pinned chats (per-user)
- `chat_service.set_chat_pinned(session, user_id, chat_id, pinned)` → bool (False = not a participant). No role check — any member may pin any of their own chats; unlimited.
- Endpoints: `PUT /chats/{chat_id}/pin` / `DELETE /chats/{chat_id}/pin` → 204, or 404 if not a member. Per-user chat-list ordering pref — no system message.
- **Multi-device sync:** on success `set_chat_pinned` publishes `{event: "chat_pin_changed", chat_id, pinned}` on the acting user's `user_events:{user_id}` channel. `connection_manager._handle_user_channel_event` forwards *any* user-channel payload to all that user's connections (no special-casing needed), so the user's other tabs/devices re-sort live. The acting connection gets the echo too (idempotent). No dedicated worker — the personal-channel mechanism (same as `added_to_chat`) already does this.
- `ChatListItemOut.pinned` (bool) — set from `Participant.pinned_at` in `list_my_chats`. Ordering handled in `crud_participant.get_user_chats` (see database_schema.md).

## Muted chats (per-user)
- `chat_service.set_chat_muted(session, user_id, chat_id, muted_until)` → bool (False = not a participant). No role check. `muted_until=None` unmutes. Client picks the duration; server stores the timestamp verbatim. See ADR 0004 + database_schema.md.
- Endpoints: `PUT /chats/{chat_id}/mute` (body `MuteChatIn {muted_until}`) / `DELETE /chats/{chat_id}/mute` → 204, or 404 if not a member.
- **Server only acts on mute in one place**: `services/messaging/send.py::fan_out_message` skips offline push for recipients with `muted_until > now()`. Everything else (hiding badges/notifications) is the client's job.
- Multi-device sync via `chat_mute_changed` on `user_events:{user_id}` (like `chat_pin_changed`). No system message.

## Known gaps (deliberately not built)
- No REST routes for sending/editing/deleting messages (WebSocket-only by design).
- No `call_service.py` / WebRTC — explicitly deferred.
- No DB migrations; no automated partition management.
- **Forward**: no backend support at all (no endpoint/action).
- FANOUT_REWRITE_PLAN.md steps 1–4 all landed.

## Working conventions
- Prefer fixing real bugs found via testing over asking permission, but **flag security-relevant or destructive changes clearly** instead of silently reverting them.
- **Ask before backend changes** (user's standing request).
