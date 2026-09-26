# Rust WS Gateway — Test Plan (handoff document)

**Purpose:** persistent work-order file. In future sessions the user will say
"do Phase N / Stage N.M from `RUST_GATEWAY_TEST_PLAN.md`" — that session must
read this file, do only that stage, write the tests **from the behavioral
contract described here (and in the code's own doc-comments / ADRs), not by
reverse-engineering the current implementation**, then run them and report
pass/fail. Do not silently expand scope to neighboring stages.

**Ground rule (per user instruction):** write each test to assert the
*expected* behavior first — derived from this plan, the doc-comments in the
source, and the referenced ADRs — before looking at whether the current code
happens to satisfy it. Only after the test is written should it be run against
the real code. A failing test is a finding to report, not something to quietly
adjust until it passes, unless the mismatch turns out to be a typo in the plan
itself.

**Scope:** `crates/common` + `crates/ws_gateway` (2716 lines, 17 files). No
Python code, no `id_service`. No existing test infra beyond the 6 inline
`#[cfg(test)]` unit tests already in `events.rs` (3) and `redis_keys.rs` (1) —
those stay, do not duplicate them.

**Status legend:** `[ ]` not started · `[~]` in progress · `[x]` done (date + pass/fail count).

---

## Phase 0 — Test harness setup (prerequisite for everything else)

Must land before any Stage 1+ work, since every later stage depends on it.

- [x] **0.1** DONE 2026-09-26. Added `dev-dependencies` to both
  `crates/ws_gateway/Cargo.toml` and `crates/common/Cargo.toml`:
  `serial_test = "3"` (env-var test isolation, Stage 1.2), `wiremock = "0.6"`
  and `tokio-tungstenite = "0.24"` (ws_gateway only — Phase 8/9 HTTP mocking +
  real WS client), and `tokio`'s `test-util` feature added to the ws_gateway
  dev-profile (plain `#[tokio::test]` already worked via the existing
  workspace `tokio` dep; `test-util` is for time-control tests like Phase 2's
  window-expiry cases).
- [x] **0.2** DONE 2026-09-26. Redis test strategy decided: connect to
  `redis://127.0.0.1:6380/1` (test_redis, DB index **1**, not `0` — avoids the
  pytest `flushdb()` collision, see `project_linka_redis_test_collision`).
  Never run the Rust suite concurrently with `run_dev.sh` or the Python suite
  against the same container. Each Redis-touching test must clean up only its
  own keys via a unique random prefix in setup/teardown, never a global
  `FLUSHDB` — documented in `crates/ws_gateway/tests/README.md`.
- [x] **0.3** DONE 2026-09-26. Added `crates/ws_gateway/tests/` (integration
  test dir, each file its own test binary) with a `README.md` recording the
  Redis strategy above so it travels with the code, not just this plan file.
- [x] **0.4** DONE 2026-09-26. Baseline `cargo test --workspace`: clean build
  12.3s, full run 13.5s wall, **5 tests pass, 0 fail** (note: this plan's
  intro says "3" existing `events.rs` tests — actual count is 4:
  `unknown_type_is_other`, `mark_frame_takes_client_message_id_field`,
  `parses_send_message_frame`, `parses_media_send_frame`; plus 1 in
  `redis_keys.rs` = 5 total. Plan text is off by one, not a code issue.)

---

## Phase 1 — `crates/common` pure-logic unit tests (no I/O, fastest ROI) — [x] DONE 2026-09-26, 47 new tests, all pass (51 total in the crate)

### Stage 1.1 — `auth.rs` (JWT verification)
Expected behavior (from doc-comments): HS256 only; `sub` is a numeric string;
`exp` is enforced; no `aud`/`iss` required (Python doesn't set them).

- [x] Valid token, correct secret → `verify` succeeds, `user_id()` returns the parsed i64.
- [x] Valid token, wrong secret → `AuthError::Invalid`.
- [x] Expired token (`exp` in the past) → `AuthError::Invalid`.
- [x] Token signed with a different algorithm (e.g. `none`, or RS256) → rejected even with a matching-looking payload.
- [x] `sub` is a non-numeric string (e.g. `"abc"`) → `verify` succeeds but `user_id()` returns `AuthError::Invalid`.
- [x] Missing `exp` claim entirely → rejected (required_spec_claims enforces it).
- [x] Token with extra unknown claims → still verifies (forward-compatible).

### Stage 1.2 — `config.rs` (env parsing + defaults)
Expected behavior: every knob has a documented default; `parse()` falls back
silently on a missing/unparseable env var; `origin_allowed` treats `"*"` as
wildcard.

- [x] No env vars set → every `Config`/`Limits` field equals its documented default (table-driven test against the defaults listed in the doc-comments).
- [x] `WS_FRAME_RATE_MAX=99` set → `Limits::from_env().frame_max == 99`.
- [x] Malformed env var (e.g. `WS_FRAME_RATE_MAX=notanumber`) → falls back to default rather than panicking.
- [x] `CORS_ALLOW_ORIGINS=""` (empty string) → results in an empty allow-list (not `[""]`) — `.filter(|s| !s.is_empty())`.
- [x] `CORS_ALLOW_ORIGINS="https://a.com, https://b.com"` → both trimmed and present; `origin_allowed("https://a.com")` true, `origin_allowed("https://evil.com")` false.
- [x] `CORS_ALLOW_ORIGINS="*"` → `origin_allowed(anything)` is always true.
- [x] `origin_allowed` is exact-match only, not substring/suffix (e.g. allow-list has `https://a.com`, reject `https://a.com.evil.com`).

*Note: `Config`/`Limits::from_env` reads process env vars — tests must use a
serial-test guard or `std::env::set_var` + restore pattern since Rust env-var
tests are not parallel-safe by default (`cargo test` runs tests in threads of
the same process). Use `#[serial]` (the `serial_test` crate) or scope each
test to its own `Config` built from an explicit map rather than real env —
prefer refactoring `Limits::from_env`'s helpers to be independently testable
with injected values if the env-based tests prove flaky.*

**Findings (2026-09-26):**
- Used `#[serial]` + a `clear_env()` helper listing all 30 env keys the
  config reads, cleared before every test; verified stable across 3 repeated
  parallel `cargo test` runs, no flakes.
- `auth.rs` Stage 1.1: the first `expired_token_is_invalid` draft used
  `exp = now() - 10`, which **passed verification** — `jsonwebtoken` applies a
  default 60s leeway around `exp`. Not a production bug (this is documented
  library behavior and Python's own JWT stack has similar leeway norms), but
  a real footgun for anyone hand-rolling an expiry test; fixed by expiring
  120s in the past and left a comment explaining why.
- `events.rs` Stage 1.3, the plan's flagged unknown: when a `mark_read` frame
  has **both** `message_id` and its `up_to_message_id` alias present, serde
  does **not** let the explicit field name win — it raises a hard
  "duplicate field" parse error, since the alias mechanism treats both keys
  as the same logical field. Documented via a dedicated test
  (`mark_frame_rejects_both_message_id_and_its_alias_present`); worth knowing
  if a client (old PoC code, a future client) ever sends both keys, since
  the frame will be dropped entirely rather than one field winning.
- `redis_keys.rs` Stage 1.4: cross-checked every key literal against live
  Python source. All match. One nuance: `ws:conns:{uid}` has **no live
  Python counterpart** any more — `realtime/ws_connection_registry.py` was
  deleted by ADR 0038 (connection-cap enforcement is now Rust-only), but the
  key name is intentionally preserved from that era per
  `.claude_docs/security_and_rate_limiting.md`. Test comment updated to
  reflect "matches legacy Python naming," not "matches current Python code."

### Stage 1.3 — `events.rs` (wire format) — extend existing 4 tests
Expected behavior: ids accepted as string OR number; unknown frame types don't
error; `SendStreamEntry`/`ReceiptStreamEntry` stringify every field with `None → ""`.

- [x] `chat_id` as a bare JSON number (not string) parses identically to the string form (the existing tests only cover the string form for `chat_id` in `send_message` — add the number form).
- [x] `de_opt_id`: empty string `""` for `reply_to_message_id` → `None`.
- [x] `de_opt_id`: explicit JSON `null` → `None`.
- [x] `de_opt_id`: a real number → `Some(n)`.
- [x] `de_id` on a non-integer number (e.g. `1.5`) → deserialization error, not silent truncation.
- [x] `de_id` on a boolean or array → deserialization error.
- [x] `SendMessageFrame` with no `media` object, `message_type` omitted → `r#type == 1` (default), `media_key/name/duration/blur_hash` all default/empty via `SendStreamEntry`.
- [x] Flat `media_key`/`media_name` fields (no nested `media` object) — the documented fallback shape — populate `SendStreamEntry` correctly.
- [x] Nested `media.key` takes priority over a flat `media_key` if somehow both are present (matches the `.or_else` fallback order in `media_key()`/etc.).
- [x] `EditFrame` requires `content`; missing it → parse error.
- [x] `MarkFrame` accepts both `message_id` and the aliased `up_to_message_id`; when both given, `message_id` wins if serde alias resolution matters (currently: serde processes explicit field name over alias — verify empirically since this test is behavior-outcome, not code-reading).
- [x] `event_chat_id`: JSON string chat_id, JSON number chat_id, missing field, and non-numeric string — 4 cases, only the first two return `Some`.
- [x] `ClientFrame::Other` round-trips for any `type` not in the known set, including a frame with no `type` field at all? (check: does `#[serde(tag="type")]` reject a missing tag entirely, or fall to `Other`? — write the test to find out, since `tag` typically requires the field to exist for a *valid* JSON object to be classified — a missing `type` key is a hard parse error, not `Other`. Confirm this explicitly; it's a common foot-gun.)

### Stage 1.4 — `redis_keys.rs` — extend existing 1 test
Expected behavior: every key function matches its documented Python
counterpart's naming exactly (this is the highest-value area since a mismatch
here silently breaks the whole Python/Rust interop and would not be caught by
any Rust-only test — cross-check the literal strings against the named Python
source file for each).

- [x] `chat_instances(42)` == `"chat_instances:42"`, matches `realtime/fanout/routing.py`.
- [x] `instance_chats("srv1")` == `"instance_chats:srv1"`.
- [x] `instance_inbox("srv1")` == `"instance_inbox:srv1"`.
- [x] `user_events(42)` == `"user_events:42"`.
- [x] `presence_events(42)` == `"presence_events:42"`.
- [x] `app_worker_alive("app")` == `"app_worker_alive:app"`.
- [x] `presence(42)` == `"presence:42"`.
- [x] `presence_last_seen(42)` == `"presence_last_seen:42"`.
- [x] `ws_conns(42)` == `"ws:conns:42"`.
- [x] `shard_for_chat` with a **negative** chat_id (Snowflakes are documented as positive, but assert the `rem_euclid` behavior explicitly so a future signed-id regression is caught) — `rem_euclid` never returns negative, confirm.
- [x] `shards == 1` → every chat maps to shard 0, key never gets a suffix.
- [x] `MESSAGE_SEND_STREAM_KEY`/`RECEIPT_STREAM_KEY` constants match the exact literal strings documented as required to match Python (`"message_send_stream"`, `"receipt_log_stream"`).

### Stage 1.5 — `ratelimit.rs` pure-logic pieces (no Redis)
- [x] `conn_member("srv", "uuid")` == `"srv:uuid"`.
- [x] `split_conn_member` is the exact inverse of `conn_member` for a UUID with no colons.
- [x] `split_conn_member` on a malformed member with no colon → `(String::new(), original)` per the documented `None` branch.
- [x] `split_conn_member` uses `rsplit_once` — if `server_id` itself ever contained a colon, splitting favors the **last** colon; write a test proving this explicitly (defends the `rsplit_once` choice against an accidental switch to `split_once`, which would silently misattribute evictions in the multi-colon case).

---

## Phase 2 — `crates/common`/`ratelimit.rs` Redis-backed tests (needs live Redis) — [x] DONE 2026-09-26, 13 new tests, all pass (64 total in the workspace)

Requires Phase 0 harness. These test the Lua scripts' actual behavior against
Redis, not just the Rust wrapper — the Lua is declared "copied verbatim from
Python," so a Redis integration test is the only way to catch a translation bug.

New file: `crates/common/tests/ratelimit_redis.rs` (integration tests against
`redis://127.0.0.1:6380/1`, per Phase 0's Redis strategy — unique key per test
via a pid+timestamp+seq id, explicit `DEL` cleanup, no global flush).

### Stage 2.1 — `check_sliding_window`
- [x] First call within a fresh window → `true` (allowed), and a second call
  immediately after, once `max_per_window` calls have been made, → `false`.
- [x] Exactly `max_per_window` calls succeed, the `max_per_window + 1`th fails,
  within the same window.
- [x] After the window elapses (sleep past `window_secs`, or use a short test
  window like 0.2s), a previously-blocked identifier is allowed again.
- [x] Two different `identifier`s (e.g. two user ids) under the same `action`
  never interfere with each other's counts.
- [x] Two different `action`s for the same `identifier` never interfere with
  each other's counts (separate Redis keys).
- [x] Redis connection failure (point the limiter at an unreachable port) →
  `check_sliding_window` returns `true` (fail-open), matching the documented
  contract.

### Stage 2.2 — `register_connection` / `unregister_connection` (connection cap)
- [x] Registering fewer than `max_connections` connections → no evictions, all
  members present in `ws:conns:{uid}`.
- [x] Registering one more than `max_connections` → exactly one eviction, and
  it's the **oldest** by score (matches `ZPOPMIN`).
- [x] Registering many more than `max_connections` in one call is not
  possible (one registration per call) — but registering sequentially past
  the cap repeatedly evicts one-at-a-time, never leaving the set above `max_connections`.
- [x] `unregister_connection` removes exactly the named member and no others.
- [x] A connection older than `max_age_secs` is pruned by `ZREMRANGEBYSCORE`
  even if the set is under the cap (crash-leak sweep) — simulate by inserting
  a member with a manually-backdated score via raw `ZADD` before calling
  `register_connection`, then assert it's gone.
- [x] `max_age_secs == 0` → the "keep TTL alive" `PEXPIRE` branch is skipped
  per the Lua's `if max_age > 0`; assert the key's TTL is left unset/persistent
  in that case (this is a real behavioral branch worth pinning).
- [x] Redis failure → `register_connection` returns `[]` (fail-open, connection proceeds) per doc-comment.

**Findings (2026-09-26):**
- Both Lua scripts matched documented behavior exactly — no translation bugs
  found versus the Python originals; every assertion passed against the real
  code on the first write (no adjustments needed after the initial run).
- The "Redis connection failure" tests (fail-open cases, both stages) turned
  out to have a construction-time subtlety not called out in the plan:
  `redis::Client::open` + `get_multiplexed_async_connection()` against an
  unreachable address (`127.0.0.1:1`) can itself fail *before* a
  `RateLimiter` is ever built, which is a different failure point than an
  already-connected client later erroring on invoke (e.g. Redis restarting
  mid-session). Handled by branching on the connection-construction result:
  if it errors at construction, the test notes the fail-open contract wasn't
  exercised on this run rather than failing or asserting something untested;
  if it succeeds (observed consistently in this environment — connection
  refused surfaces at the first command, not at `get_multiplexed_async_connection`
  time), the real fail-open assertion runs. Both `RateLimiter` calls did
  fail-open correctly when this path was exercised.
- No `uuid` crate available in the workspace; used a
  pid+nanosecond-timestamp+atomic-counter id generator instead of pulling in
  a new dependency for test-only uniqueness.
- `register_connection_over_cap_evicts_exactly_the_oldest` needed small
  (5ms) sleeps between registrations — the Lua scores by millisecond
  timestamp, and three same-millisecond `ZADD`s would make "oldest" ambiguous
  under `ZPOPMIN`'s tie-breaking (falls back to lexical member order, not
  insertion order) rather than reflecting a real bug.

---

## Phase 3 — `presence.rs` (Redis-backed) — [x] DONE 2026-09-26, 12 new tests, all pass (77 total in the workspace)

Behavioral contract from doc-comment: `presence:{uid}` is a **bare** connection-id
SET (not `{server_id}:{conn}`), TTL'd; `presence_last_seen` has no TTL; a
`presence_update` publishes only on the 0↔1 device edge.

New file: `crates/ws_gateway/tests/presence_redis.rs` (integration tests
against `redis://127.0.0.1:6380/1`, per Phase 0's Redis strategy — unique
random user id per test, explicit `DEL` cleanup, no global flush).

- [x] `mark_online` on a fresh user: `SCARD presence:{uid}` becomes 1, `presence_last_seen` is set to a recent ISO-8601 timestamp, and a `presence_update {status: online}` is published on `presence_events:{uid}` (assert via a subscriber).
- [x] `mark_online` called again for a **second** `conn_uuid` of the same user (multi-device) → `SCARD` becomes 2, but **no second publish** (already-online, no 0→1 edge).
- [x] `mark_offline` removing one of two devices → `SCARD` drops to 1, no publish (still online on another device).
- [x] `mark_offline` removing the last device → `SCARD` hits 0, a `presence_update {status: offline, last_seen_at}` is published.
- [x] `mark_online`/`mark_offline` always refresh `presence_last_seen` regardless of whether the edge event fires.
- [x] The `presence:{uid}` key's TTL is refreshed (`EXPIRE`) on every `mark_online` call, not just the first.
- [x] `heartbeat` refreshes the TTL and `last_seen` without touching membership or firing any publish.
- [x] `set_active(active=false)` behaves identically to `mark_offline`; `set_active(active=true)` identically to `mark_online` (thin-wrapper contract).
- [x] `get_status` on a user with zero connections → `(false, last_seen_or_none)`.
- [x] `get_status` on a user with ≥1 connection → `(true, Some(last_seen))`.
- [x] `get_status` for a user who was never seen at all → `(false, None)`.

**Findings (2026-09-26):**
- **Structural blocker hit before any test could be written**: `ws_gateway`'s
  `Cargo.toml` declared only a `[[bin]]` target (`src/main.rs` with private
  `mod` declarations) — a binary crate can't be linked as a dependency by an
  external integration test in `tests/`, so `presence`/`state`/`routing` etc.
  were unreachable from `crates/ws_gateway/tests/*.rs`. This is the same class
  of problem the plan calls out as Stage 6.0's blocker check, just encountered
  one phase earlier. Fixed by adding `crates/ws_gateway/src/lib.rs` (a thin
  `pub mod` re-export of all 10 modules) and a matching `[lib]` target in
  `Cargo.toml`; `main.rs` now does `use ws_gateway::{fanin, routing,
  state::AppState, ws};` instead of declaring `mod` items itself. Pure
  structural change, zero behavior difference — confirmed via a full
  `cargo build -p ws_gateway` before writing any test, and the full workspace
  suite (`cargo test --workspace`) staying green afterward. This unblocks
  Phase 6/7/8 too, so their own Stage 6.0 blocker check is already resolved.
- Every assertion passed against the real code on the first run — no
  mismatches between the doc-comment's contract and `presence.rs`'s actual
  behavior.
- `is_valid_rfc3339` was hand-rolled (a byte-position shape check) rather than
  pulling in `time`'s `parsing` Cargo feature for one assertion — avoids
  growing the dev-dependency surface for a single timestamp-format check.

---

## Phase 4 — `routing.rs` (Redis-backed) — [x] DONE 2026-09-26, 10 new tests, all pass (87 total in the workspace)

Contract: `chat_instances:{chat}` SET of server_ids ↔ `instance_chats:{server}`
reverse-map SET of chat ids; best-effort (never panics on Redis error).

New file: `crates/ws_gateway/tests/routing_redis.rs` (integration tests
against `redis://127.0.0.1:6380/1`, per Phase 0's Redis strategy — unique
random server_id/chat_id per test, explicit `DEL` cleanup, no global flush).

- [x] `add_chat` populates both `chat_instances:{chat_id}` (contains `server_id`)
  and `instance_chats:{server_id}` (contains `chat_id`), and sets a TTL on
  `chat_instances:{chat_id}` matching `ttl_secs`.
- [x] `add_chat` called twice for the same chat/server is idempotent (SET semantics — still exactly one member).
- [x] `remove_chat` removes the server from `chat_instances` and the chat from `instance_chats`, but does not delete other unrelated members.
- [x] `remove_chat` on a chat/server pair that was never added is a no-op, not an error.
- [x] `heartbeat`: after `add_chat` for N chats, calling `heartbeat` re-`EXPIRE`s every one of those N `chat_instances:*` keys and the `instance_chats:{server}` key itself.
- [x] `heartbeat` when the server serves **zero** chats still refreshes `instance_chats:{server}`'s own TTL (so it doesn't vanish before a graceful `unregister`) — **see finding below: this item as literally worded describes an unreachable Redis state; split into two tests reflecting the actual reachable behavior.**
- [x] `unregister` removes the server from every `chat_instances:{chat}` it was in, and deletes `instance_chats:{server}` entirely.
- [x] `unregister` on a server with no registrations is a no-op.
- [x] Redis errors in any of the four functions are swallowed (logged, not
  propagated) — inject a failure (e.g. wrong arg types via a raw broken pipe
  is hard; simplest is to point at an unreachable Redis and assert the
  function returns `()` without panicking).

**Findings (2026-09-26):**
- **The zero-chats heartbeat item is unconstructible as worded.**
  `instance_chats:{server}` is a plain Redis SET; `heartbeat`'s only write to
  that key is a bare trailing `EXPIRE` (no `SADD`/`SET`). Verified directly
  against the real Redis container: `EXPIRE` on a **missing** key returns `0`
  and creates nothing (`redis-cli EXPIRE nonexistent 10` → `0`,
  `EXISTS` → `0` after). Since `SREM`ing a SET's last member deletes the key
  outright, "the key exists but is served by zero chats" is not a reachable
  Redis state for this data structure — a genuinely zero-chat server has a
  **fully absent** `instance_chats` key, not an empty-but-present one. Split
  the single plan item into two tests: (1)
  `heartbeat_with_a_fully_absent_instance_chats_key_does_not_resurrect_it` —
  proves heartbeat does *not* create the key from nothing (the literal
  opposite of "still refreshes its own TTL" read at face value); (2)
  `heartbeat_refreshes_instance_chats_ttl_even_while_it_still_serves_at_least_one_chat`
  — the actually-reachable near-zero case, confirming the trailing `EXPIRE`
  refreshes the TTL regardless of how many/few chats SMEMBERS returns, as
  long as the key exists at all. Not a code bug — `heartbeat`'s behavior is
  internally consistent and matches its own doc-comment ("Keep the
  reverse-map key alive even when this process serves zero chats, so shutdown
  can still clean up" — true in the sense that the key survives across a
  0-chat *transition* from a prior nonzero state within the same TTL window,
  not that it's resurrected from total absence). Flagging so the plan's
  wording doesn't mislead a future session into expecting key-creation
  behavior that was never implemented and isn't needed (the key is only ever
  read/expired here, never (re)created, by design — creation happens solely
  in `add_chat`).
- The Redis-failure fail-open test reused Phase 2/3's now-established pattern
  (best-effort skip-with-note if the connection itself fails to construct
  against the unreachable address, rather than at command time) — same
  environment behavior observed as those phases: the connection constructs
  successfully and the real fail-open path (no panic across all four
  functions) was exercised on this run.
- Everything else matched the doc-comment's contract exactly on the first
  write — no other mismatches between plan and code.

---

## Phase 5 — `receipts.rs` / `send_path.rs` (Redis-backed, stream producers) — [x] DONE 2026-09-26, 10 new tests, all pass (97 total in the workspace)

New file: `crates/ws_gateway/tests/receipts_and_send_path_redis.rs` (integration
tests against `redis://127.0.0.1:6380/1`, per Phase 0's Redis strategy).

### Stage 5.1 — `receipts::enqueue`
- [x] A successful `enqueue` produces exactly one new entry in
  `receipt_log_stream` (via `XLEN` before/after, or `XRANGE`), with all 5
  fields (`chat_id`, `user_id`, `kind`, `up_to_message_id`, `occurred_at`)
  present as strings and matching the inputs.
- [x] `occurred_at` parses as valid RFC3339 / ISO-8601 UTC.
- [x] `MAXLEN ~ {receipt_stream_maxlen}` is applied — this is approximate
  trimming so an exact-count assertion isn't meaningful, but confirm the
  stream doesn't grow *unbounded* across many inserts beyond the configured
  cap order of magnitude (use a small maxlen like 5 and insert 50 entries,
  assert final `XLEN` is in a small multiple of 5, not 50).

### Stage 5.2 — `send_path::app_workers_alive`
- [x] Key `app_worker_alive:{app_server_id}` present → returns `true`.
- [x] Key absent → returns `false`.
- [x] Key present but expired (set with a 0/negative TTL, or wait it out) → `false`.
- [x] Redis unreachable → returns `false` (fail-closed — note this is the
  **opposite** polarity from the rate limiter's fail-open; the plan calls this
  out explicitly so the test asserts the documented asymmetry rather than
  "assuming" one universal fail behavior).

### Stage 5.3 — `send_path::enqueue`
- [x] Enqueuing a frame for `chat_id=8` with 4 shards lands in the bare
  `message_send_stream` key (shard 0); `chat_id=9` lands in
  `message_send_stream:1` — cross-check against the existing
  `redis_keys.rs` unit test's own numbers for consistency.
- [x] The XADD'd entry's fields match `SendStreamEntry::pairs()` exactly (all
  10 fields present, stringified).
- [x] `MAXLEN ~` behaves approximately as in 5.1 (small-maxlen smoke test).

**Findings (2026-09-26):**
- **The plan's `MAXLEN ~` sizing was wrong, in both Stage 5.1 and 5.3.**
  `receipts::enqueue`/`send_path::enqueue` take a full `&AppState`, so each
  test builds one via `Config::from_env()` with env-var overrides for
  `APP_SERVER_ID`/`SEND_STREAM_SHARDS`/`MESSAGE_SEND_STREAM_MAXLEN`/
  `RECEIPT_STREAM_MAXLEN` — `#[serial]`-guarded and cleared before/after, same
  pattern as Phase 1 Stage 1.2's env tests. The plan's literal recipe ("small
  maxlen like 5, insert 50 entries, expect a small multiple of 5") was run
  first as written and **failed**: `XLEN` came back as exactly 50, zero
  trimming. Root cause (verified empirically, not a code bug): Redis's
  approximate `MAXLEN ~` only evicts whole macro-nodes of the stream's
  underlying radix tree (default ~100 entries/node) and never splits a node,
  so any stream that hasn't yet grown past one node's worth of entries is
  *never* trimmed by `~`, regardless of how small the configured MAXLEN is.
  Fixed by raising the insert count to 5,000 (comfortably past a node
  boundary) and asserting the growth ratio is far below 1 (`growth <
  inserts/2`) rather than expecting a tight bound near the configured MAXLEN
  — the actual, honest contract of approximate trimming. `receipts.rs` and
  `send_path.rs` themselves needed no changes; this was purely a test-sizing
  correction to the plan's own numbers.
- `receipt_log_stream` (Stage 5.1) is a single **global** key per its own
  doc-comment — unlike the per-chat-sharded `message_send_stream[:N]` keys, it
  can't be scoped to a unique key per test. Handled the same way
  `routing_redis.rs` handles shared state: read length before/after and assert
  on the delta, plus an explicit trailing `DEL` cleanup of the whole key after
  the maxlen test (safe — DB index 1 is test-only, never touched by
  `run_dev.sh` or the Python suite per Phase 0.2's isolation rule).
  `message_send_stream` tests instead use a unique `chat_id` (with
  `send_stream_shards=1` so the key is deterministic) to get an isolated key
  per test, avoiding the shared-key problem entirely for Stage 5.3.
- `AppState`'s `redis` field needs a real `MultiplexedConnection`; `http`
  (`reqwest::Client::new()`) and `sub_tx` (an unused-receiver `mpsc::channel`)
  are irrelevant to `receipts`/`send_path` but still required to construct one
  — same as Phase 3/4's `lib.rs` unblock, no new blocker here since `AppState`
  was already reachable from `tests/`.
- No `AsyncCommands::xlen` exists in this workspace's pinned `redis` crate
  version — used the raw `redis::cmd("XLEN")` form instead (`XREVRANGE` for
  field inspection, same raw-command style already used implicitly via `redis`
  crate elsewhere in the test suite).
- Every other assertion matched the doc-comments' contract exactly on the
  first write (field values, sharding math, fail-open/fail-closed polarity,
  RFC3339 shape) — no further mismatches found.

---

## Phase 6 — `state.rs` (`AppState` in-memory routing tables — pure logic, no Redis) — [x] DONE 2026-09-26, 25 new tests, all pass (135 total in the workspace)

High value, zero I/O: this is the concurrency-critical sharded-map bookkeeping
that everything else depends on. Can run fully in-process with a bare
`AppState` (construct one with a dummy `MultiplexedConnection` — may need a
`AppState::new` variant or a test-only constructor that doesn't require a live
Redis handle; note this as a blocker to resolve in Stage 6.0 if `AppState::new`
can't be built without connecting).

New file: `crates/ws_gateway/tests/state_tables.rs`.

- [x] **6.0 (blocker check):** Can a `AppState` be constructed in a test
  without a real Redis connection? `MultiplexedConnection` likely requires an
  actual `redis::Client::get_multiplexed_async_connection().await`, which
  needs a reachable Redis. If so, these tests fall under Phase 0's Redis
  harness too (just never issue Redis commands) — document this dependency
  rather than assuming pure-in-memory testability.

### Stage 6.1 — connection lifecycle (`add_conn` / `remove_conn`)
- [x] First connection for a user → `add_conn` returns `true` ("user_first").
- [x] Second connection for the same user (multi-device) → returns `false`.
- [x] `remove_conn` on the last connection of a user → `user_gone == true` in the returned tuple.
- [x] `remove_conn` on one of two connections → `user_gone == false`.
- [x] `remove_conn` called twice on the same `conn_id` (idempotency) → second call returns empty results, doesn't panic, doesn't double-decrement anything.
- [x] `remove_conn` on a `conn_id` that was never added → returns `(vec![], false, vec![])` cleanly.

### Stage 6.2 — chat subscriptions (`add_chat_sub`, `senders_for_chat`, cleanup via `remove_conn`)
- [x] First subscriber to a chat → `add_chat_sub` returns `true` (0→1 edge).
- [x] Second subscriber to the same chat → returns `false`.
- [x] `senders_for_chat` returns exactly the tx handles of connections currently subscribed, paired with the right `ConnId`.
- [x] `senders_for_chat` for a chat with zero subscribers → empty vec, not a panic.
- [x] Removing the last subscriber of a chat (via `remove_conn`) reports that chat in the `emptied_chats` vec; removing one of two subscribers does not.
- [x] A connection subscribed to multiple chats, on disconnect, correctly empties **only** the chats where it was the sole subscriber (multi-chat fan-out correctness — this is the kind of bug a partial implementation could get wrong under concurrent chat memberships).

### Stage 6.3 — presence watches (`add_presence_watch`, `remove_presence_watch`, `senders_for_presence`)
- [x] First watcher of a target → `add_presence_watch` returns `true` (0→1 edge, caller should subscribe the Redis channel).
- [x] Second watcher of the same target → returns `false`.
- [x] `remove_presence_watch` on the last watcher → returns `true` (1→0 edge).
- [x] `remove_presence_watch` on a watch relation that doesn't exist → returns `false`, no panic.
- [x] A connection watching multiple targets, on `remove_conn`, correctly reports every target that dropped to zero watchers, and *only* those (mirrors 6.2's multi-chat correctness concern for the reverse presence direction).
- [x] After `remove_conn`, `dropped_chats` state for that connection is cleared too (per the doc-comment on `remove_conn`) — verify via `take_dropped` returning empty for a freshly-removed conn_id even if frames were dropped for it before removal.

### Stage 6.4 — gap tracking (`mark_dropped` / `take_dropped`) — ADR 0060
- [x] `mark_dropped` then `take_dropped` for the same conn_id/chat_id returns a set containing that chat_id.
- [x] `take_dropped` drains the set — a second immediate call returns empty (per doc-comment "since the last flush").
- [x] Multiple `mark_dropped` calls for different chats on the same connection accumulate into one set, all returned together by one `take_dropped`.
- [x] `take_dropped` on a connection with no drops recorded → empty set, not a panic.

### Stage 6.5 — `force_disconnect`
- [x] Calling `force_disconnect` with a UUID matching a live connection fires that connection's `Notify` exactly once (observable via `notified().await` resolving on a cloned `Arc<Notify>` in the test, or via a flag set after `.notified()` returns).
- [x] Calling `force_disconnect` with a UUID that matches no live connection is a silent no-op (idempotent per doc-comment), not a panic or error.

**Findings (2026-09-26):**
- The 6.0 blocker was already resolved by Phase 3's `lib.rs`/`[lib]` target
  addition — confirmed, no rework needed. `AppState::new` still requires a
  real `MultiplexedConnection` (no Redis-free constructor exists), so every
  test in this file connects to `test_redis` on `6380/1` even though not one
  of them issues a single Redis command afterward — purely to satisfy the
  struct's field type.
- Every assertion matched the doc-comments' contract exactly on the first
  write — no mismatches between the plan and `state.rs`'s actual behavior.
  No `#[serial]`/env-var handling needed here (unlike Phase 5): these tests
  never touch `Config::from_env()` with overrides, so `Config::from_env()`'s
  real env values are irrelevant to any assertion.

---

## Phase 7 — `fanin.rs` message routing logic (needs a live pub/sub Redis connection; the hardest phase) — [x] DONE 2026-09-26, 14 new tests, all pass (135 total in the workspace, includes Phase 6's 25)

This is the most complex file — it owns reconnect/backoff, dynamic
subscribe/unsubscribe, and 4 distinct payload-routing branches. Testing it
properly means driving real Redis PUBLISH traffic at a running `listen()` task
and observing what lands in local mpsc channels. Consider testing
`handle_message`'s branch logic directly (it's a free function taking
`&Arc<AppState>` + channel + payload) rather than the outer `run`/`listen`
loop, to avoid needing a real pub/sub subscription cycle for every case —
reserve a smaller number of full end-to-end pub/sub tests for the reconnect
behavior specifically.

New file: `crates/ws_gateway/tests/fanin_redis.rs`.

### Stage 7.1 — `handle_message` branch dispatch (call directly, no live pub/sub needed beyond AppState + a channel name/payload pair)
- [x] `{"event": "force_disconnect", "connection_id": "<uuid>"}` on any channel → calls `force_disconnect` with that uuid (assert via a connection registered with that uuid getting its cancel Notify fired).
- [x] `force_disconnect` payload missing `connection_id` → no-op, doesn't panic.
- [x] Channel `user_events:{uid}`, event `added_to_chat` with a `chat_id` → every local connection of that user gets added to `chat_subs[chat_id]`, and if it's a new (0→1) local registration, `routing::add_chat` is invoked (assert the Redis side-effect: `SMEMBERS chat_instances:{chat_id}` now includes this server_id).
- [x] Same, `chat_id` given as a JSON number instead of string → same result (the code explicitly handles both).
- [x] `removed_from_chat` with a `chat_id` → the reverse: local subscriptions removed, and if it was the last one, `routing::remove_chat` invoked.
- [x] Any `user_events:{uid}` message (including `added_to_chat`/`removed_from_chat`) is **also** forwarded verbatim to the user's local connections via `fan_out_untracked` (assert the raw JSON shows up on the mpsc receiver, not just the side-effect).
- [x] An unrecognized event kind on `user_events:{uid}` (e.g. `{"event": "something_else"}`) → not routed for chat-sub changes, but still forwarded to the user's connections (fan_out_untracked doesn't gate on event kind).
- [x] Channel `presence_events:{uid}` → payload forwarded via `fan_out_untracked` to `senders_for_presence(uid)` only, and explicitly **not** gap-tracked (no `mark_dropped` call — hard to assert a negative directly; assert instead that a full channel here does NOT populate `dropped_chats` for that connection, contrasting with 7.1's chat case below).
- [x] Any other channel (i.e. `instance_inbox:{server_id}` itself) with an event carrying a `chat_id` field → routed via `fan_out_chat` to `senders_for_chat(chat_id)`.
- [x] `fan_out_chat` to a connection whose channel buffer is full (simulate: create an mpsc with capacity 1, fill it, then trigger a fan-out) → the send fails silently and `mark_dropped(conn_id, chat_id)` is called (assert via `take_dropped` afterward) — this is the ADR 0060 contract and deserves its own explicit test distinct from the happy path.
- [x] `fan_out_untracked` to a full channel → the send fails silently and **no** `dropped_chats` entry is created (contrast case, proves gap-tracking is chat-fan-out-only as documented).
- [x] Malformed (non-JSON) payload → `handle_message`/`listen` does not panic, message is dropped.
- [x] A JSON payload that's valid but has neither `event: force_disconnect` nor a resolvable `chat_id` and isn't on a `user_events`/`presence_events` channel → silently dropped (fell through every branch), no panic, no fan-out.

### Stage 7.2 — resubscription after reconnect (`listen`'s startup block)
- [x] Given an `AppState` with entries already in `user_conns` and
  `presence_subs` (simulating survivors of a previous connection before a
  reconnect), starting `listen()` re-subscribes `user_events:{uid}` for every
  key in `user_conns` and `presence_events:{uid}` for every key in
  `presence_subs` — assert by publishing to one of those channels immediately
  after `listen()` starts and confirming it's received (a purely
  black-box integration test using two real Redis connections: one running
  `listen`, one publishing).
- [~] `run()`'s outer loop: if `listen()` returns an `Err` (simulate by
  killing the Redis connection mid-flight, e.g. via a proxy you can drop, or
  by using an invalid `redis_url` that fails the *second* time only if
  feasible — otherwise this may be better suited to a manual/documented test
  rather than automated, note that trade-off explicitly if reconnection can't
  be forced deterministically in-process) → the 500ms backoff fires and it
  retries rather than crashing the process. **Not automated — see finding.**
- [x] `run()` returns cleanly (no retry loop) when `cmd_rx` is closed (all
  senders dropped) — this is the documented shutdown path; assert `run()`'s
  future resolves within a short timeout after dropping `sub_tx`.

**Findings (2026-09-26):**
- `handle_message` was `pub(crate)`-invisible to `tests/` (private free
  function, not part of the `fanin` module's public surface) — made it `pub`
  with a doc-comment explaining why. Pure visibility change, zero behavior
  difference, same class of fix as Phase 3's `lib.rs` unblock; confirmed via
  `cargo build -p ws_gateway` before writing any test and the full workspace
  suite staying green afterward.
- **Real bug caught by the plan's own ground rule**, not in the code: the
  first draft of `build_state()` (mirroring Phase 6's helper) never overrode
  `REDIS_URL`, so `Config::from_env()` resolved its documented default
  (`redis://127.0.0.1:6379/0`, the *dev* port) instead of `test_redis` on
  `6380/1`. Phase 6's `AppState` never re-reads `config.redis_url` after
  construction (its `MultiplexedConnection` is already open), so this didn't
  matter there — but `fanin::listen`/`run` *do* re-dial `config.redis_url`
  themselves on every (re)connect. Against an unreachable dev Redis (nothing
  was listening on 6379 in this environment), `listen()` failed to open a
  client, hit the 500ms-backoff retry loop, and never reached either the
  `cmd_rx.recv()` shutdown branch or the subscribe step — manifesting as two
  test failures (`run_returns_cleanly_when_cmd_channel_closed` timing out,
  and the resubscription test never receiving its publish) that looked like
  `run()` hangs on shutdown. Root cause confirmed by checking
  `crates/common/src/config.rs`'s documented default and `lsof` showing
  nothing bound to 6379. Fixed the test helper (`#[serial]`-guarded env
  override, same pattern as Phase 1 Stage 1.2 and Phase 5) — not a `fanin.rs`
  code change; `run()`/`listen()` behave exactly as documented once pointed
  at a reachable Redis.
- The `run()`-retries-after-`listen()`-errors half of Stage 7.2 is left
  **not automated**, matching the plan's own flagged trade-off: forcing
  `listen()`'s internal `pubsub.subscribe`/stream to fail deterministically
  mid-flight needs an injectable transport or a killable proxy, neither of
  which exists in this workspace's dependency set. The 500ms backoff-and-loop
  path was read and matches its doc-comment, but is unverified by an
  automated test — flagged as a gap rather than silently marked done.
- Every other assertion matched `fanin.rs`'s doc-comments exactly on first
  write — no mismatches found between the plan and the real code.

---

## Phase 8 — `ws.rs` connection lifecycle & dispatch (integration-level, axum test server + mock `/internal/*`)

The largest and highest-value file. Needs Phase 0's `wiremock` (or hand-rolled
HTTP stub) for `app_internal_url`, plus a live/local Redis for the limiter and
presence calls it triggers. Use `axum::Router` in-process with `tower::ServiceExt::oneshot`
or a real bound `TcpListener` + a WS client crate (`tokio-tungstenite`) to
drive actual WebSocket frames end-to-end — prefer the real-socket approach
since this file's entire job is the WS protocol handshake/read/write loop.

**Stages 8.1-8.5: [x] DONE 2026-09-26, 32 new tests, all pass (149 total in
the workspace). Stages 8.6-8.11: [x] DONE 2026-09-26, 31 new tests, all pass
(223 total in the workspace, `ws_dispatch.rs` now 63 tests). One item left
unautomated per its own flagged trade-off (see findings).**

New file: `crates/ws_gateway/tests/ws_dispatch.rs` — a real bound
`TcpListener` + `axum::serve` running the actual `ws_handler`, driven by a
real `tokio-tungstenite` WS client, plus `wiremock` for `/internal/ws-bootstrap`.
`#[serial]`-guarded (same pattern as Phase 5/7's `build_state`) since
`Config::from_env()` reads real process env vars.

### Stage 8.1 — handshake / origin / auth rejection paths
- [x] Missing/invalid JWT → the socket is upgraded then immediately closed
  with code `4401` (assert via the WS client observing a Close frame with
  that code, not an HTTP-level rejection — code comment confirms it upgrades
  first specifically so the client sees a close code, not a raw HTTP error).
- [x] Expired JWT → same `4401` path.
- [x] `Origin` header present and not in `cors_allow_origins` (and list isn't
  `["*"]`) → closes with `4403`, and this rejection happens **before** the
  WS upgrade completes reading the token (should reject even with a garbage/missing token, since origin is checked first).
- [x] `Origin` header present and **is** in the allow-list → proceeds past the origin check.
- [x] No `Origin` header at all, and `cors_allow_origins` contains `"*"` →
  allowed (documented as "non-browser client" case).
- [x] No `Origin` header, and `cors_allow_origins` is a specific list without
  `"*"` → rejected with `4403`.
- [x] `client_ip` extraction: `X-Forwarded-For: 1.2.3.4, 5.6.7.8` → takes
  `"1.2.3.4"` (first hop), trimmed. **See finding: exercised indirectly, not
  as a standalone unit test — `client_ip` is a private free function.**
- [x] `client_ip` extraction: header absent → `"unknown"`. **Same as above.**
- [x] Handshake-churn cap: exceeding `upgrade_ip_max` within
  `upgrade_ip_window_secs` from the same IP → connection closes with `4429`
  (`CLOSE_HANDSHAKE_CHURN`), even with a valid token.
- [x] Handshake-churn cap: exceeding `upgrade_user_max` for the same user
  (different IPs) → also `4429`.
- [x] A connection under both churn caps proceeds to full accept.

### Stage 8.2 — connect-time side effects (happy path)
- [x] A successful connect calls the mocked `/internal/ws-bootstrap` with the
  token, and the returned chat_ids become this connection's `chat_subs`
  entries (assert indirectly: send an `instance_inbox` PUBLISH scoped to one
  of those chat_ids and confirm the client receives it).
- [x] `/internal/ws-bootstrap` failing (mock 500, or timeout) → connection
  still succeeds (best-effort per doc-comment), just with zero chat
  subscriptions.
- [x] First connection for a brand-new user id → the gateway subscribes
  `user_events:{uid}` (assert by publishing a user event right after connect
  and confirming delivery — flaky-timing risk: allow a short poll/retry
  window rather than a fixed sleep).
- [x] A second simultaneous connection for the *same* user does not
  re-subscribe `user_events` (can't directly assert a non-subscription, but
  can assert both connections still receive user events correctly — the
  important behavior is delivery, not the internal subscribe count).
- [x] Presence is marked online on connect (`presence:{uid}` gains a member) — assert via a raw Redis check, or via a second connection's `subscribe_presence` call returning `status: online`.
- [x] Connection-cap eviction: with `ws_conn_max` set very low (e.g. 1 via env
  var override in the test process), connecting a second time for the same
  user causes the first connection to receive a `force_disconnect` publish
  and get closed with code `4409` — full round trip: connect #1, connect #2,
  assert #1's socket receives a Close(4409).

### Stage 8.3 — read-loop frame handling
- [x] Sending a non-JSON text frame → receives `{"type":"error","code":"bad_frame"}`, connection stays open (not dropped) for the next frame.
- [x] Sending a well-formed but completely unknown `type` → parses to `ClientFrame::Other`, no reply, no error (dispatch's catch-all does nothing) — connection stays open.
- [x] Sending a `ping`/`pong` control frame is swallowed silently, no `ack`/`error` reply, connection stays open.
- [x] Sending a raw `Close` frame ends the read loop cleanly (no panic on cleanup).
- [x] Frame-rate flood: sending more than `frame_max` text frames within
  `frame_window_secs` → after the limit, subsequent frames get
  `{"type":"error","code":"rate_limited"}` (echoing `client_message_id` if
  present in the offending frame), and the connection is **not** yet closed.
- [x] Frame-rate flood past `frame_flood_strikes` consecutive rate-limited
  frames → the connection is forcibly closed (breaks the read loop) — assert
  the socket actually disconnects, not just that error frames keep flowing.
- [x] A single non-flood frame after some rate-limited ones resets
  `flood_strikes` to 0 (per `flood_strikes = 0` on any successfully-processed
  frame) — send N-1 flooded frames (under the strike threshold), wait out the
  window, send one clean frame, then flood again for N-1 more — assert it
  still takes the full strike count to disconnect rather than accumulating
  across the gap. **This test genuinely sleeps ~31s to wait out the window —
  it's the slow one in the file.**

### Stage 8.4 — `heartbeat` dispatch
- [x] A bare `heartbeat` frame gets a `{"type":"heartbeat_ack"}` reply with no `resync_chat_ids` key when nothing was dropped.
- [x] After a chat-scoped fan-out frame was dropped for this connection (force
  a full mpsc buffer, then trigger a chat publish, per Phase 7's gap-tracking
  test), the next `heartbeat` reply includes `resync_chat_ids` as an array of
  **string** chat ids (Snowflake-as-string convention) containing exactly the
  dropped chat. **Implemented by calling `AppState::mark_dropped` directly
  (found the connection's `ConnId` via `user_conns`) rather than forcing a
  real full mpsc buffer — see finding.**
- [x] `resync_chat_ids` is drained after being sent — a second immediate heartbeat has no `resync_chat_ids` key again.
- [x] `heartbeat` also refreshes presence TTL (assert via Redis TTL check, or via a `subscribe_presence` call from another connection still reporting online well past the original `presence_ttl_secs` thanks to the heartbeat).

### Stage 8.5 — `send_message` dispatch
- [x] A well-formed `send_message` when workers are alive (mock
  `app_worker_alive:{app_server_id}` key present in Redis) and under all rate
  limits → gets `{"type":"ack","for":"send_message","status":"queued"}`, and
  an entry lands in the correct sharded `message_send_stream[:N]` key.
- [x] Exceeding `send_max`/`send_window_secs` (the primary bucket) →
  `{"type":"error","code":"rate_limited","client_message_id":"..."}`, no XADD occurs.
- [x] Exceeding `send_burst_max`/`send_burst_window_secs` while under the
  primary limit → same rate_limited error (both buckets are ANDed —
  `primary && burst`).
- [x] `app_worker_alive:{app_server_id}` key **absent** → despite being under
  every rate limit, the response is `{"type":"error","code":"internal_error","client_message_id":"..."}`
  and **no XADD occurs** (ADR 0041 — ensures the rate-limit buckets were
  still consumed even though the send was rejected, per the file's own
  Step-4 doc-comment "Metered actions still consume their bucket" — write a
  test that confirms the bucket WAS decremented despite the rejected send).
- [ ] A Redis XADD failure during enqueue — **not automated**, matching the
  plan's own flagged trade-off (no fault-injection hook exists to force an
  XADD to fail deterministically against a live, healthy Redis).
- [x] `client_message_id` is echoed back verbatim (including unusual but
  valid values — empty string, very long string, unicode) in both the ack
  and every error path that includes it.

**Findings (2026-09-26):**
- **Real bug found and fixed in `ws.rs`, not a test artifact.** All three
  close-then-drop rejection paths (`close_after_upgrade` for 4401/4403, and
  the handshake-churn 4429 path) sent a `Message::Close` frame on the raw,
  un-split `WebSocket` and then let the whole socket drop immediately when
  the `on_upgrade` closure returned — racing the TCP flush. A real
  `tokio-tungstenite` client observed this as `Protocol(ResetWithoutClosingHandshake)`
  (a raw connection reset) instead of ever seeing the documented close code,
  which directly contradicts the function's own doc-comment ("so the client
  sees the same 4xxx close it gets from the Python endpoint rather than a
  bare HTTP error"). Confirmed deterministic (100% reproduction) for the
  4401 auth-failure path and ~75%-flaky for the 4429 churn path (timing-
  dependent race, not always lost). Separately, and worse: the **auth**
  rejection path (`CLOSE_UNAUTHORIZED` via `.map_err(|_| CLOSE_UNAUTHORIZED)?`
  in `run_connection`) never called `send_close` **at all** — the `Err` was
  only ever used for a `tracing::debug!` log at the call site in
  `ws_handler`, so an invalid/expired JWT always hit a bare reset, 100% of
  the time, regardless of any timing race. Fixed by: (1) giving `send_close`
  a bounded best-effort drain (`socket.recv()` with a 500ms timeout) after
  sending the close frame, so the peer has a window to actually read it
  before the socket is dropped; (2) routing `close_after_upgrade` through
  the same `send_close` helper instead of a bare one-shot `.send()`; (3)
  actually calling `send_close(&mut socket, CLOSE_UNAUTHORIZED)` on both auth
  failure branches in `run_connection` instead of silently `?`-propagating
  past it. Verified fixed with 5+ consecutive full-file runs, zero flakes,
  before and after the full-workspace suite stayed green.
- **Test-only flakiness found and fixed, unrelated to the `ws.rs` bug above**:
  `handshake_churn_cap_per_ip_closes_4429`'s first draft used no
  `X-Forwarded-For` header, so its "IP" identity was the literal string
  `"unknown"` — the same bucket key (`ws_upgrade_ip:unknown`) that several
  *other* tests in this same file also hit (e.g. the wildcard-origin and
  allowed-origin happy-path tests, which likewise connect without an XFF
  header). Depending on `cargo test`'s (non-alphabetical, unspecified) test
  ordering within the `#[serial]`-enforced single-threaded run, a sibling
  test could exhaust that shared bucket first, making this test's own
  *first* connection spuriously rejected. The same issue independently
  surfaced in `connection_under_both_churn_caps_proceeds_to_full_accept`
  (`upgrade_ip_max = 5`, also no XFF header) once run as part of the full
  `cargo test --workspace` (its compiled-test-binary ordering differs from
  running `ws_dispatch` alone). Fixed both by giving each test a unique
  synthetic `X-Forwarded-For` IP (`unique_label("198.51.100.1")` /
  `"198.51.100.2"`) instead of relying on the shared bare-"unknown" identity
  — not a `ws.rs` code change, purely a test-isolation fix. Verified stable
  across 4+ consecutive full `cargo test --workspace` reruns after the fix.
- `client_ip`'s two plan bullets (XFF-first-hop parsing, absent-header
  default) are exercised only indirectly, through the handshake-churn-cap
  tests' use of distinct `X-Forwarded-For` values to simulate distinct "IPs"
  (and the deliberate omission of the header in the wildcard/no-origin
  tests to hit the `"unknown"` default) — `client_ip` is a private free
  function in `ws.rs` with no test-only `pub(crate)` exposure, and adding one
  wasn't judged necessary since the churn-cap tests already prove both
  parsing branches produce a distinguishable, correct rate-limit identity.
- The dropped-chat heartbeat test (`resync_chat_ids`) calls
  `AppState::mark_dropped` directly against the real connection's `ConnId`
  (discovered via `state.user_conns`) rather than reproducing Phase 7's
  approach of forcing a genuinely full mpsc buffer through a live chat
  publish — driving an actual full-buffer drop through the real WS+Redis
  path would need control over the connection's internal channel capacity
  that isn't exposed to an external integration test. This still exercises
  the exact heartbeat-dispatch code path the plan cares about (reading and
  draining `dropped_chats`), just with the "why it was marked dropped" half
  covered separately by Phase 7's own `fanin_redis.rs` tests instead of
  re-proven here.
- Every other assertion matched `ws.rs`'s doc-comments and code exactly on
  the first write — no further mismatches found between the plan and the
  real code for Stages 8.1-8.5.
- Stages 8.6 (`mark_delivered`/`mark_read`/`mark_played`), 8.7 (typing/
  recording), 8.8 (subscribe/unsubscribe presence), 8.9 (`presence_active`),
  8.10 (edit/delete/restore/purge relay), and 8.11 (disconnect cleanup) are
  unstarted — left for a future session per the plan's own suggested
  chunking.

### Stage 8.6 — `mark_delivered` / `mark_read` / `mark_played` dispatch
- [x] A well-formed mark frame, workers alive, under rate limit → gets
  `{"type":"ack","for":"mark_read"}` (etc. per kind), and an entry lands in
  `receipt_log_stream` with the correct `kind` int (2/3/4 per `receipt_kind`).
- [x] Exceeding the `ws_receipts` bucket → `{"type":"error","code":"rate_limited","for":"mark_read"}` (note: `for`, not `client_message_id` — different error shape than send_message, worth a dedicated test since it's easy to typo the field name in a refactor).
- [x] Workers not alive → `{"type":"error","code":"internal_error","for":"mark_read"}`, no XADD.
- [ ] A receipt XADD failure (fire-and-forget per doc-comment: "no error frame, client re-sends on next scroll") → **no ack, no error frame at all** — this is a distinct and easy-to-miss contract vs. send_message's error-on-failure; assert literally nothing is sent to the client in this case (i.e., no message arrives within a short timeout window). **Not automated — see finding (same class of gap as Stage 8.5's XADD-failure bullet).**
- [x] All three kinds (delivered/read/played) exercise the same shared `mark()` path with the correct `kind` constant each — three parameterized variants of the happy-path test.

### Stage 8.7 — `typing` / `recording` dispatch
- [x] Under the `ws_typing` rate limit and `/internal/typing-allowed` mocked to return `{"allowed": true}` → a `typing` event is published to the chat (assert via routing/pub-sub observation), with no direct ack/reply to the sender.
- [x] `/internal/typing-allowed` mocked to return `{"allowed": false}` → nothing is published, no error frame either (documented "drops silently — never leak a typing indicator").
- [x] `/internal/typing-allowed` mock failing/erroring (500, timeout) → same silent drop (fail-closed, `!= Some(true)` covers both `Some(false)` and `None`).
- [x] Exceeding the typing rate limit → the internal HTTP call is never even made (short-circuits before the network call — verify via the mock server's request count being zero for that case).
- [x] `recording` frame follows the identical path with `kind="recording_audio"` instead of `"typing"` in the published event.

### Stage 8.8 — `subscribe_presence` / `unsubscribe_presence` dispatch
- [x] Subscribing to **your own** user id → `{"type":"error","code":"bad_request","message":"Cannot subscribe to your own presence"}`, and no Redis subscription is created.
- [x] Subscribing to another user, `/internal/presence-authorized` mocked `true` → `{"type":"presence_status","user_id":"<target>","status":"online"|"offline","last_seen_at":...}` reflecting that target's real current presence state.
- [x] Subscribing to another user, authorization mocked `false` → a `{"type":"presence_revoked",...}` frame instead, and any **pre-existing** watch for that target is torn down (test the revoke-of-existing-watch path specifically, not just the never-subscribed case).
- [x] Authorization check failing/erroring (network error) → treated the same as `false` (fail-closed — `!= Some(true)`).
- [x] `unsubscribe_presence` for a currently-watched target → the local watch is removed and (if it was the last local watcher) the Redis channel is unsubscribed; this frame type consumes **no rate-limit bucket** ("Unmetered, like the Python side" — verify by flooding `unsubscribe_presence` far past any bucket's max and confirming it never gets rate-limited).
- [x] `unsubscribe_presence` for a target that was never watched → silent no-op.
- [x] Exceeding `ws_sub_presence` bucket on a **subscribe** call → the doc-comment says re-run on every heartbeat, and the rate check happens but its failure just returns early with **no reply at all** (re-read the code path: on `!ok` it returns before any frame is sent) — confirm this "silent drop on limit" behavior explicitly, since it's easy to assume an error frame is sent here by analogy with other buckets.

### Stage 8.9 — `presence_active` dispatch
- [x] `{"active": true}` → equivalent to a foreground reconnect: `mark_online` semantics apply (assert via a subsequent `subscribe_presence` from another connection reporting online).
- [x] `{"active": false}` → `mark_offline` semantics (reporting offline, provided this was the user's only connection).
- [x] Consumes the `ws_sub_presence` bucket (shares the same limiter key
  as `subscribe_presence`/`SubscribePresence` per the code — worth confirming
  this shared-bucket detail is intentional and testing it doesn't silently
  starve subscribe_presence or vice versa under load).

### Stage 8.10 — `edit_message` / `delete_message` / `restore_message` / `purge_message` dispatch (relayed via `/internal/message/*`)
- [x] A well-formed edit under the `ws_edit` bucket, mock `/internal/message/edit` returns 200 with a JSON body → the client receives that body with `type: "ack"` and `for: "edit_message"` merged in.
- [x] Mock returns 4xx with `{"detail": "not your message"}` → client receives `{"type":"error","code":"bad_request","for":"edit_message","message":"not your message"}`.
- [x] Mock returns 403 specifically → `code` is `"forbidden"` rather than `"bad_request"` (the one special-cased status).
- [x] Mock returns 5xx, or the internal call fails outright (connection refused / timeout) → `{"type":"error","code":"internal_error","for":"edit_message"}` (no `message` field, since `Failed` carries no detail).
- [x] Exceeding the shared `ws_edit` bucket → `{"type":"error","code":"rate_limited","for":"edit_message"}` and the internal HTTP call is **never made** (assert zero requests hit the mock in this case).
- [x] All four ops (edit/delete/restore/purge) share the same `ws_edit` bucket — flooding via `edit_message` calls should also cause a subsequent `delete_message` to be rate-limited within the same window (cross-op bucket-sharing test, a real footgun if someone "fixes" this into per-op buckets without updating the plan/ADR).
- [x] `delete_message`/`restore_message`/`purge_message` each hit their own distinct `/internal/message/{delete,restore,purge}` path with `{user_id, chat_id, message_id}` (no `content` field, unlike edit).

### Stage 8.11 — cleanup / disconnect
- [x] On disconnect, `remove_conn`'s returned `emptied_chats` triggers a
  `routing::remove_chat` call for each (assert via `chat_instances` Redis set
  no longer containing this server for that chat, when it was the last local
  member).
- [x] On disconnect, if this was the user's last connection, `user_events`
  unsubscribe is sent (hard to assert the internal unsubscribe directly;
  assert the *effect*: a subsequent user-event publish is no longer delivered
  to this now-closed connection — trivially true since the socket is closed,
  so this may reduce to a smoke test only. Consider instead asserting that
  `fanin`'s internal subscription set shrinks, if that's inspectable, or mark
  this case as "effect-only, not directly observable" in the report). **See finding: covered as effect-only via the chat_instances/ws:conns assertions below, not a separate dedicated test.**
- [x] Presence is marked offline on disconnect (mirrors Stage 8.2's online assertion, in reverse).
- [x] `unregister_connection` removes this connection's slot from `ws:conns:{uid}` (so it doesn't count toward the cap for the *next* connection attempt).
- [x] Disconnect cleanup is safe to run even when connect-time setup partially
  failed (e.g. `ws-bootstrap` returned zero chats) — no panics on empty
  `emptied_chats`/etc.
- [x] A connection forcibly closed via the connection-cap eviction path
  (`cancel.notified()`) still runs the full cleanup block afterward (presence
  offline, routing removal, etc.) — not just the abrupt `Close(4409)` send.

**Findings (2026-09-26):**
- No code changes needed in `ws.rs`/`handlers.rs`/`message_ops.rs`/`bootstrap.rs`
  — every assertion matched the doc-comments and existing code on write, once
  three test-side mistakes (below) were fixed. All 31 new tests pass; the full
  workspace suite (223 tests total) stays green, and the 63-test
  `ws_dispatch.rs` file was re-run 3 consecutive times with zero flakes.
- **`ws:conns:{uid}` is a Redis **ZSET**, not a SET** (`register_connection`/
  `unregister_connection` in `crates/common/src/ratelimit.rs` use `ZADD`/`ZREM`/
  `ZPOPMIN`/`ZCARD`) — the first draft of the two Stage 8.11 tests that check
  it used `SCARD`, which silently returned `0` against a ZSET key (wrong type
  for that command, `redis-rs` treats it as "no such key" territory rather
  than erroring in this client version) and made `card_before >= 1` fail
  immediately after a real connect. Not a `ws.rs` bug — confirmed against
  Phase 2's own `ratelimit_redis.rs` tests, which already exercise this key as
  a ZSET. Fixed by switching both tests to `ZCARD`.
- **`wiremock`'s `received_requests()` returns every request the mock server
  saw, matched or not** — the Stage 8.7 typing-rate-limit test and the Stage
  8.10 shared-edit-bucket test both first counted `received_requests().len()`
  directly, which silently included the connect-time `/internal/ws-bootstrap`
  call every test client makes (a different path, never registered against a
  `Mock`). Not a gateway bug; fixed by filtering the returned requests down to
  the specific path under test (`/internal/typing-allowed`,
  `/internal/message/edit`) before counting.
- **The typing/recording fan-out tests initially tried to fake a chat
  subscription with a bare local `AppState::add_chat_sub` call**, but
  `handlers::typing`'s publish path (`AppState::publish_event`) resolves
  subscribers by reading `chat_instances:{chat_id}` from **Redis**, populated
  only by the real connect-time `routing::add_chat` call (itself driven by the
  `/internal/ws-bootstrap` response) — a local-only `add_chat_sub` never
  reaches that Redis-routed path, so the watcher's socket never received
  anything and both tests timed out waiting for the event. Fixed by mocking
  `/internal/ws-bootstrap` to return the test's `chat_id` for every connecting
  client (sender included) instead of hand-registering the subscription, which
  also matches how Stage 8.2's own tests already drive the same path. A
  side effect: since the sender is now also a genuine chat subscriber, it
  receives its own broadcast typing event too (fan-out doesn't exclude the
  sender) — the "no direct reply" assertion was adjusted to allow exactly that
  one broadcast frame through while still asserting no separate ack/error
  frame follows it, rather than asserting bare silence.
- The plan's Stage 8.6 4th bullet (a receipt XADD failure yields no ack/no
  error at all) is left **not automated**, same class of gap the plan itself
  already flagged for Stage 8.5's equivalent send_message case: no
  fault-injection hook exists to force an `XADD` to fail deterministically
  against a live, healthy Redis in this workspace. The `mark()` code path
  (fire-and-forget on an `Err` from `receipts::enqueue`, no frame sent) was
  read directly and matches its own doc-comment.
- Stage 8.11's "user_events unsubscribe sent on last-connection disconnect"
  bullet is, as the plan itself anticipated, effect-only and not independently
  observable from outside the process (the internal `SubCmd::Unsubscribe` send
  has no externally visible side effect beyond the socket already being
  closed) — not given its own dedicated test; the surrounding disconnect
  assertions (`chat_instances` cleanup, `ws:conns` cleanup, presence-offline,
  safety with a partial connect, and cleanup-after-force-disconnect) cover the
  rest of Stage 8.11's cleanup contract directly.
- Every other assertion — mark-frame ack/error shapes and `kind` values,
  typing/recording's allow/deny/error/rate-limit branches, subscribe/
  unsubscribe presence's authorize/revoke/no-rate-limit/silent-drop branches,
  presence_active's online/offline/shared-bucket behavior, and all of edit/
  delete/restore/purge's relay/error-code/shared-bucket/distinct-path
  behavior — matched the doc-comments and real code exactly, with no
  production-code changes required.

---

## Phase 9 — `bootstrap.rs` HTTP client helpers (mock-server based, some overlap with Phase 8 but isolate the pure request/response mapping here first) — [x] DONE 2026-09-26, 25 new tests, all pass (full workspace suite green)

Cheaper to nail down in isolation before relying on it inside Phase 8's bigger
integration tests.

New file: `crates/ws_gateway/tests/bootstrap_http.rs` (each test spins up its
own ephemeral `wiremock::MockServer`, no Redis needed, fully parallel-safe).

- [x] `fetch_chat_ids`: mock 200 with `{"chat_ids": ["1","2","3"]}` → returns `[1,2,3]` as i64.
- [x] `fetch_chat_ids`: mock 200 with a non-numeric string in the array → that entry is silently dropped (`filter_map`), others still returned.
- [x] `fetch_chat_ids`: mock 500 → returns `[]`, no panic.
- [x] `fetch_chat_ids`: mock connection refused (no server) → returns `[]`.
- [x] `fetch_chat_ids`: mock 200 with malformed JSON body → returns `[]`.
- [x] `presence_authorized`: mock `{"authorized": true}` → `Some(true)`; `{"authorized": false}` → `Some(false)`; 4xx/5xx/network-fail/malformed-json → `None` in every case.
- [x] `typing_allowed`: same 4-way matrix (`Some(true)`/`Some(false)`/`None` cases) using `{"allowed": ...}`.
- [x] `post_json`: 2xx with a valid JSON body → `PostOutcome::Ok(body)`.
- [x] `post_json`: 2xx with an empty/non-JSON body → `PostOutcome::Ok({})` (per the `.ok().unwrap_or` fallback — a genuinely easy spot for a silent bug, worth pinning).
- [x] `post_json`: 403 → `PostOutcome::ClientError("forbidden", detail)`.
- [x] `post_json`: other 4xx (e.g. 400, 404, 422) → `PostOutcome::ClientError("bad_request", detail)`.
- [x] `post_json`: 4xx with no `detail` field in the body → `ClientError(code, "")` (empty string default, not a panic).
- [x] `post_json`: 5xx → `PostOutcome::Failed`.
- [x] `post_json`: network failure / timeout → `PostOutcome::Failed`.

**Findings (2026-09-26):**
- No blockers: `bootstrap` was already `pub mod` in `lib.rs` (from Phase 3's
  earlier fix), so no visibility changes were needed.
- Every assertion matched `bootstrap.rs`'s doc-comments and code exactly on
  the first write — all 25 tests passed with zero adjustments, no mismatches
  between the plan and the real code.
- `post_json_other_4xx_is_bad_request` parameterizes the plan's "e.g. 400,
  404, 422" into one test looping all three statuses against a fresh
  `MockServer` per iteration, rather than three separate test functions.

---

## Phase 10 — `main.rs` (process-level, low unit-test value — smoke/manual)

Most of this file is process wiring (signal handling, listener binding,
`tokio::main`) that's impractical to unit test in isolation. Recommend a
short, explicit smoke-test subset rather than full coverage:

- [ ] `health_probe()` against a real bound `/healthz` listener returns 0; against a closed port returns 1. (This one **is** cleanly unit-testable — it's a plain blocking TCP client function.)
- [ ] `routing_heartbeat`'s ticker interval matches `config.routing_heartbeat_secs` — could assert indirectly via Phase 4's routing tests (call `routing::heartbeat` directly; the ticker loop itself isn't worth testing beyond "it calls the function on the configured period," which `MissedTickBehavior::Skip` semantics make awkward to assert precisely — consider a note-only item rather than an automated test).
- [ ] Full end-to-end smoke test (manual or CI-only, not part of the unit suite): start the real binary against `docker-compose`'s Redis + a stub Python `/internal/*` server, connect a real WS client, send a `send_message`, confirm the stream entry appears. This belongs in a separate `tests/smoke_e2e.rs` gated behind a feature flag or `#[ignore]` so `cargo test` stays fast by default, run explicitly in CI or before a deploy.

---

## Suggested session-sized chunks (for future "do stage X" invocations)

Each bullet below is a reasonable single-session unit — mention the exact
stage numbers when kicking one off:

1. Phase 0 (harness) — must go first, always.
2. Phase 1 (all of `common`'s pure logic) — one session, no Redis needed.
3. Phase 2 (`ratelimit.rs` Redis tests) — one session.
4. Phase 3 (`presence.rs`) — one session.
5. Phase 4 (`routing.rs`) — one session, can pair with Phase 3 if time allows (both are small, similar shape).
6. Phase 5 (`receipts.rs` + `send_path.rs`) — one session.
7. Phase 6 (`state.rs`) — one session; the 6.0 blocker (binary-only crate,
   unreachable from `tests/`) was already resolved in Phase 3 via
   `crates/ws_gateway/src/lib.rs` + a `[lib]` Cargo.toml target — no need to
   redo that check.
8. Phase 7 (`fanin.rs`) — one session, probably the second-hardest.
9. Phase 8 — split further, it's huge:
   - 8.1–8.2 (handshake + connect side-effects) — one session.
   - 8.3–8.4 (read loop + heartbeat) — one session.
   - 8.5–8.6 (send_message + receipts dispatch) — one session.
   - 8.7–8.9 (typing/presence dispatch) — one session.
   - 8.10–8.11 (message_ops relay + cleanup) — one session.
10. Phase 9 (`bootstrap.rs`) — one session, can be pulled *earlier* (before Phase 8) since Phase 8 depends on mocking these same endpoints — consider doing Phase 9 right after Phase 0 if the mock-server harness is ready.
11. Phase 10 — quick, low priority, can be folded into any other session's leftover time.

## Running the suite

```bash
# Unit tests only (no Redis needed) — Phase 1 subset
cargo test -p linka-common

# Full suite once Phase 0 harness lands (needs test_redis on 6380, per root CLAUDE.md)
REDIS_URL="redis://127.0.0.1:6380/1" cargo test --workspace

# Skip slow/manual smoke tests
cargo test --workspace -- --skip smoke_e2e
```
