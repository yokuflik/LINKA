"""Application configuration.

Split into focused sub-modules (ADR 0019); this package is a thin facade that
re-exports every setting, so `from config import X` and `import config` /
`config.X` keep working exactly as before. Each sub-module declares `__all__`,
so nothing but the settings themselves leaks into this namespace.

To re-evaluate an env-var default in a test, reload the sub-module that owns
the setting (e.g. `importlib.reload(config.app_settings)`), not this package -
reloading a package returns the cached sub-modules.
"""

from .app_settings import *  # noqa: F401,F403
from .auth_settings import *  # noqa: F401,F403
from .redis_settings import *  # noqa: F401,F403
from .security_settings import *  # noqa: F401,F403
from .username_settings import *  # noqa: F401,F403
from .storage_settings import *  # noqa: F401,F403
from .messaging_settings import *  # noqa: F401,F403
from .partition_settings import *  # noqa: F401,F403

from . import (  # noqa: F401
    app_settings,
    auth_settings,
    redis_settings,
    security_settings,
    username_settings,
    storage_settings,
    messaging_settings,
    partition_settings,
)

__all__ = [
    *app_settings.__all__,
    *auth_settings.__all__,
    *redis_settings.__all__,
    *security_settings.__all__,
    *username_settings.__all__,
    *storage_settings.__all__,
    *messaging_settings.__all__,
    *partition_settings.__all__,
]
