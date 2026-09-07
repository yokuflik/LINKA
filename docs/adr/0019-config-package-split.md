# ADR 0019 — Split `config.py` into a `config/` package behind a re-export facade

**Status:** Accepted
**Date:** 2026-09-07

## Context

`config.py` had grown to ~506 lines and ~90 module-level settings spanning eight
unrelated domains (auth, Redis, transport security + rate limiting, usernames,
object storage, fan-out/receipt streams, time-partition management, plus a small
"app identity" core). It tripped Core Behavior Rule 9 (single file well over
~300 lines) and made every domain's knobs hard to locate.

~40 modules import from it, in two styles:

- `from config import X, Y` (the large majority)
- `import config` then `config.X` (`utils/id_client.py`, `services/user_service.py`)

No module uses `from config import *`. `tests/test_config.py` calls
`importlib.reload(config)` to re-evaluate an env-var default.

## Decision

Convert `config.py` into a `config/` package. This mirrors the
`message_service` → `services/messaging/` (ADR unstated) and `chat_service` →
`services/chats/` (ADR 0013) pattern: **a pure code move, public surface
preserved.**

```
config/
  __init__.py            # facade: `from .<sub> import *` for every sub-module
  app_settings.py        # server/instance identity, Snowflake + Rust ID service, message/group caps
  auth_settings.py       # JWT, OTP abuse limits, /auth/refresh + account-create limits, Firebase
  redis_settings.py      # REDIS_URL, REDIS_MAX_CONNECTIONS
  security_settings.py   # trusted proxies / hosts / CORS, per-IP REST backstop, all WS + REST rate limits
  username_settings.py   # ADR 0017 username rules, generation, and username-specific rate buckets
  storage_settings.py    # S3 endpoint/buckets/keys, presigned-URL expiries, upload size + MIME tables
  messaging_settings.py  # send-queue + fan-out streams, routing-layer TTLs, detailed receipt-log + stream
  partition_settings.py  # ADR 0005 time-partition management values
```

- `config.py` is **deleted** — a package and a same-named module cannot coexist.
- Every sub-module declares an explicit `__all__` so the facade's `import *`
  re-exports **only** the settings, keeping `config`'s namespace byte-for-byte
  what it was (no leaked `os` / `random` / `uuid`).
- Group boundaries were chosen so **no setting references a name from another
  group**. Every derived value (`USERNAME_REGEX`, `S3_AVATARS_PUBLIC_BASE_URL`,
  `MAX_UPLOAD_BYTES_BY_KIND`, `UPLOAD_BUCKET_BY_KIND`, `RECEIPT_KINDS`, …)
  depends only on names in its own sub-module.

### Test impact

`importlib.reload(config)` on a package re-runs `__init__.py` but returns the
**cached** sub-modules, so an env-var default is not re-evaluated. The two
`tests/test_config.py` cases that rely on this now reload
`config.app_settings` directly and assert against that module. No runtime
reload logic is added to the package — production code stays clean.

## Consequences

- All existing `from config import X` and `import config` / `config.X` usages
  work unchanged; no caller edits.
- Each domain's knobs now live in a ~30–150 line file, all under Rule 9's limit.
- Adding a setting: put it in the right sub-module and its `__all__`; the facade
  picks it up automatically.
- Slight indirection cost when reading `config/__init__.py` (eight star-imports)
  — acceptable and conventional.
