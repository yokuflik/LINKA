"""`config.settings` - a single read-only accessor over every config value.

ADR 0029. Purely additive: `from config import X` and `import config; config.X`
keep working. New code (REST `modules/`) should prefer:

    from config import settings
    settings.MAX_MESSAGE_CONTENT_LENGTH

Every name is resolved **live** off the sub-module that owns it, so a test that
does `monkeypatch.setattr(config.app_settings, "X", ...)` is reflected here too.
"""

from . import (
    app_settings,
    auth_settings,
    redis_settings,
    security_settings,
    username_settings,
    storage_settings,
    messaging_settings,
    partition_settings,
    search_settings,
)

_MODULES = (
    app_settings,
    auth_settings,
    redis_settings,
    security_settings,
    username_settings,
    storage_settings,
    messaging_settings,
    partition_settings,
    search_settings,
)

# name -> owning sub-module, built once from each sub-module's explicit __all__.
_INDEX = {name: mod for mod in _MODULES for name in mod.__all__}


class _Settings:
    """Flat, read-only view over the config sub-modules."""

    __slots__ = ()

    def __getattr__(self, name):
        try:
            return getattr(_INDEX[name], name)
        except KeyError:
            raise AttributeError(f"unknown setting {name!r}") from None

    def __setattr__(self, name, value):
        raise AttributeError(
            "config.settings is read-only; patch the owning config sub-module instead"
        )

    def __dir__(self):
        return sorted(_INDEX)


settings = _Settings()

__all__ = ["settings"]
