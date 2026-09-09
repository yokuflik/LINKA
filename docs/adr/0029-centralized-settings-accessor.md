# ADR 0029 — Centralized `settings` accessor object over the `config/` package

**Status:** Accepted
**Date:** 2026-09-09
**Amends:** ADR 0019 (config package split — kept, this sits on top of it)

## Context

ADR 0019 split `config.py` into a `config/` package of eight domain sub-modules
behind a `from .<sub> import *` facade. That fixed the 500-line god-file, but
left two AI-hostile properties:

- **Name-by-name imports.** Every consumer enumerates each setting it needs in a
  multi-line `from config import (A, B, C, ...)` block — `realtime/ws_router.py`
  lists 27, several REST routers list 4–10. Adding or renaming a knob is churn
  across every importer.
- **No discoverable surface.** The facade is a star-glob, so "what settings
  exist?" is only answerable by opening all eight sub-modules and reading their
  `__all__` lists.

## Decision

Add a single read-only accessor object, `config.settings`, that exposes every
setting as a flat attribute:

```python
from config import settings
settings.MAX_MESSAGE_CONTENT_LENGTH
settings.WS_FRAME_RATE_MAX
```

- **Purely additive.** The existing `from config import X` and
  `import config; config.X` styles keep working unchanged. `realtime/` (being
  rewritten in Rust), `scripts/`, and `infra/` are left on the old style for
  now; REST `modules/` migrate to `settings`.
- **No new dependency.** Pydantic v2 moved `BaseSettings` into the separate
  `pydantic-settings` package, which is not installed. A flat 139-field
  settings class would also just be a new 350-line god-file. Instead the
  accessor is a ~30-line object that resolves each name **live** off the
  sub-module that owns it (built once into a `name -> module` index from the
  sub-modules' `__all__`). Env-var parsing, computed defaults, and the
  rationale comments all stay where they are in the sub-modules.
- **Monkeypatch-friendly.** Because the accessor does `getattr(owning_module,
  name)` at access time, `monkeypatch.setattr(config.app_settings, "X", ...)`
  is reflected through `settings.X`. Tests that patch a *consumer's* local
  binding (`monkeypatch.setattr(auth_service, "OTP_REQUEST_RATE_LIMIT_MAX",
  ...)`) are unaffected only while that consumer still does `from config import
  X`; those seven modules (`modules/auth/service.py`, `modules/auth/router.py`,
  `modules/messaging/router.py`, `modules/messaging/scheduled_service.py`, and
  the `modules/chats/service.py` / `modules/messaging/service.py` facades) are
  therefore **not migrated in this change** — they move to injected settings
  together with the test-DI refactor (deferred; see
  `.claude_docs/env_handoff.md`).

## Consequences

- One import line per file; every setting discoverable in `config/_accessor.py`
  + the eight `__all__` lists it aggregates.
- Two access styles coexist during the migration. The end state is `settings.X`
  everywhere except the Rust-bound realtime layer; full removal of the flat
  re-export facade is a later, separate change.
- `config.settings` is not a Pydantic model — no validation/coercion layer is
  added. The sub-modules' `os.environ.get(...)` parsing remains the single
  source of truth.
- **Update 2026-09-09 (ADR 0033 landed):** the seven deferred modules moved to
  injected `policy=` / `limits=` (ADR 0033) or to `settings.X`, so the flat
  re-export facade in `config/__init__.py` was trimmed from
  `from .<sub> import *` to an explicit ~64-name allow-list — exactly the names
  still consumed by `realtime/`, `scripts/`, and `infra/`. `modules/` and the
  test suite are now 100% on `settings.X` (tests that patched a config value
  patch the owning `config.<sub>` module). Full facade removal still waits on
  the `realtime/` Rust rewrite.
- `importlib.reload(config.app_settings)` in `tests/test_config.py` still works
  (those tests assert against the reloaded module directly, not via
  `settings`); the accessor's cached module reference would not see the reload,
  which is acceptable and documented here.
