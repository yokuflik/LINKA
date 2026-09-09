# 33. Inject rate-limit / size caps into services and routers

Date: 2026-09-09

## Status

Accepted

## Context

The REST API tests (`tests/api/test_rest_api.py`,
`tests/test_scheduled_messages_api.py`) and several service-unit tests
(`tests/modules/auth/test_auth_service.py`,
`tests/modules/messaging/test_message_service.py`,
`tests/modules/messaging/test_scheduled_service.py`,
`tests/modules/receipts/test_receipts.py`,
`tests/modules/chats/test_chat_service.py`) override individual limits with
`monkeypatch.setattr(<consumer module>, "<NAME>", value)`.

This only works because the consumer does `from config import NAME`, binding
the name as a **module global** on the consumer. It is fragile: the patch
target is the *importer*, not the config source, so it silently stops
working the moment a module is refactored to `import config` /
`config.settings.NAME`. ADR 0029 deliberately left seven modules un-migrated
for exactly this reason (see the `env_handoff.md` D2 note).

## Decision

Pass the tunable caps in as **parameters**, defaulted from config, instead of
reading them off a module global.

### 1. A `Limits` value object per feature

Each feature that has test-tuned caps gets a small frozen dataclass in its
package, e.g. `modules/auth/limits.py`. Auth's object is named `AuthPolicy`
(it also carries non-limit knobs like token expiry); the purely
rate-limit ones stay `*Limits`:

```python
@dataclass(frozen=True)
class AuthPolicy:
    otp_request_max: int = OTP_REQUEST_RATE_LIMIT_MAX
    otp_request_window_s: int = OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS
    otp_verify_max_attempts: int = OTP_VERIFY_MAX_ATTEMPTS
    account_create_ip_max: int = ACCOUNT_CREATE_IP_RATE_LIMIT_MAX
    account_create_ip_window_s: int = ACCOUNT_CREATE_IP_RATE_LIMIT_WINDOW_SECONDS
    refresh_jti_max: int = REFRESH_JTI_RATE_LIMIT_MAX
    refresh_jti_window_s: int = REFRESH_JTI_RATE_LIMIT_WINDOW_SECONDS
    access_token_expire_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES

DEFAULT_AUTH_POLICY = AuthPolicy()
```

Same shape for:
- `modules/auth/limits.py` — `AuthPolicy` (above) + the per-IP caps the
  router enforces (`otp_request_ip_*`, `otp_verify_ip_*`, `refresh_ip_*`).
- `modules/messaging/limits.py` — `MessagingLimits`
  (`max_message_content_length`, `receipt_named_list_max_members`,
  `msg_history_max_limit`, `msg_history_rate_*`, `detail_read_rate_*`,
  `upload_ticket_rate_*`, `upload_ticket_ip_rate_*`).
- `modules/messaging/limits.py` — `ScheduledLimits`
  (`max_pending_per_user`, `min_lead_seconds`, `max_lead_days`,
  `write_rate_max`, `write_rate_window_s`).
- `modules/chats/limits.py` — `ChatLimits` (`max_initial_group_members`).

The field defaults are the **only** remaining `from config import NAME` in
these modules — read once at class-definition time is acceptable because a
test never patches a dataclass default; it constructs `AuthPolicy(otp_request_max=2)`.

### 2. Service functions take `limits=DEFAULT_*_LIMITS`

Every service function that currently reads a tuned global gets a trailing
keyword-only parameter — `policy: AuthPolicy = DEFAULT_AUTH_POLICY` for auth,
`limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS` etc. elsewhere — and
reads `policy.otp_request_max` instead of the global. Non-tuned config
(`JWT_SECRET_KEY`, stream keys, …) stays a direct `from config import` /
`settings.X` — it is not injected.

Affected service functions:
- `auth_service`: `request_otp`, `verify_otp_and_login`,
  `verify_firebase_and_login`, `refresh_access_token`,
  `_find_or_create_and_issue`, `_issue_token_pair` (token expiry).
- `message_service` (`send.py` / `receipts.py`): `process_outgoing`
  (content length), `get_message_receipts` (named-list cap).
- `scheduled_service`: `schedule_message` (pending cap, lead window),
  `reschedule` (lead window).
- `chat_service` (`creation.py`): `create_group_chat`
  (`max_initial_group_members`).

### 3. Routers build the object and pass it

Each router constructs its feature's `Limits` once at module import
(`_LIMITS = MessagingLimits()`) and passes it into service calls. The per-IP
caps the router itself enforces come off the same object. For the routes the
tests need to tune, the object is exposed as a **FastAPI dependency** so
tests use `app.dependency_overrides`:

```python
def get_messaging_limits() -> MessagingLimits:
    return _LIMITS

@router.get("")
async def get_message_history(..., limits: MessagingLimits = Depends(get_messaging_limits)):
    ...
```

### 4. Tests

- **Route tests** (`test_rest_api.py`, `test_scheduled_messages_api.py`):
  `app.dependency_overrides[get_auth_policy] = lambda: AuthPolicy(otp_request_max=2)`
  in a fixture / context manager. No `monkeypatch`.
- **Service-unit tests** (`test_auth_service.py`, `test_message_service.py`,
  `test_scheduled_service.py`, `test_receipts.py`, `test_chat_service.py`):
  pass `limits=AuthPolicy(otp_request_max=3)` directly to the service call.
  No `monkeypatch` of config names.
- `monkeypatch` stays only for **collaborator** stubbing that isn't config
  (`notification_service.send_push`, `chat_service.realtime_service.publish_event`)
  — that is legitimate and out of scope here.

### 5. Follow-on (ADR 0029 completion)

Once no test patches `from config import NAME` on these modules, the flat
`from config import X` re-export in `config/__init__.py` can be dropped for
everything except `realtime/` (Rust rewrite) and `scripts/`. The non-tuned
names in the seven modules move to `settings.X` (Task 4 / the env_handoff
D-items). `DEV_AUTH_WHITELIST` also moves to `settings` (it is patched by
env, not by tests).

## Consequences

- ~8 source files change (`modules/{auth,messaging,chats}/` limits +
  services + routers). No behavior change at default config.
- Auth is security-sensitive: the token-expiry and account-creation caps now
  flow through a parameter. Default path is unchanged; the parameter only
  exists so a test can shrink a window. Reviewed as part of this ADR.
- Service signatures gain one keyword-only arg each. Existing callers that
  don't pass it get the default — no call-site churn outside routers/tests.
- The `Limits` objects are the single documented place a cap is named,
  replacing scattered `from config import` blocks.
