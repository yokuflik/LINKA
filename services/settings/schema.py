"""
Declarative spec for per-user settings + a validator for partial patches.

Adding a new setting is done HERE and nowhere else in the schema layer:
add its default to DEFAULT_USER_SETTINGS and, if it is not a free-form
value, add its allowed values to _ENUMS keyed by its dotted path. No
migration, no new column - the value lands in user_settings.settings JSONB.

Design: settings are grouped one level deep ("privacy", and later
"notifications", "security", ...). The validator walks the patch against
DEFAULT_USER_SETTINGS, so any group/key not present in the defaults is
rejected - a client cannot smuggle arbitrary JSON into the blob.
"""
from copy import deepcopy
from typing import Any

from services.settings.errors import SettingsValidationError

# Visibility options for "who may see this about me".
PRIVACY_VISIBILITY = ("everyone", "contacts", "nobody")

# The full, canonical shape. A user's stored blob is a sparse subset of
# this; reads merge the stored blob over a deepcopy of this.
DEFAULT_USER_SETTINGS: dict[str, Any] = {
    "privacy": {
        # Who may see the "last seen" timestamp.
        "last_seen": "everyone",
        # Who may see the live online / connected indicator.
        "online": "everyone",
    },
}

# Dotted path -> allowed scalar values. A leaf in DEFAULT_USER_SETTINGS
# that is not listed here is treated as free-form (type-checked only
# against the default's Python type).
_ENUMS: dict[str, tuple] = {
    "privacy.last_seen": PRIVACY_VISIBILITY,
    "privacy.online": PRIVACY_VISIBILITY,
}


def default_settings() -> dict[str, Any]:
    """A fresh, mutable copy of the canonical defaults."""
    return deepcopy(DEFAULT_USER_SETTINGS)


def merge_with_defaults(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay a sparse stored blob on top of the canonical defaults."""
    merged = default_settings()
    if stored:
        _deep_merge(merged, stored)
    return merged


def validate_patch(patch: Any) -> dict[str, Any]:
    """
    Validate a partial settings patch against the canonical shape.

    Returns the patch unchanged (as a dict) on success; raises
    SettingsValidationError on any unknown group/key or bad value.
    """
    if not isinstance(patch, dict):
        raise SettingsValidationError("settings patch must be an object")
    _validate_node(patch, DEFAULT_USER_SETTINGS, path="")
    return patch


def apply_patch(stored: dict[str, Any] | None, patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a validated patch onto the sparse stored blob."""
    result = deepcopy(stored) if stored else {}
    _deep_merge(result, patch)
    return result


# --- internals ---------------------------------------------------------

def _deep_merge(dst: dict, src: dict) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value


def _validate_node(node: Any, spec: Any, path: str) -> None:
    if isinstance(spec, dict):
        if not isinstance(node, dict):
            raise SettingsValidationError(f"'{path or 'settings'}' must be an object")
        for key, value in node.items():
            if key not in spec:
                where = f"{path}.{key}" if path else key
                raise SettingsValidationError(f"unknown setting '{where}'")
            child_path = f"{path}.{key}" if path else key
            _validate_node(value, spec[key], child_path)
        return

    # Leaf value.
    allowed = _ENUMS.get(path)
    if allowed is not None:
        if node not in allowed:
            raise SettingsValidationError(
                f"'{path}' must be one of {', '.join(allowed)}"
            )
        return

    # Free-form leaf: enforce the default's type (bool/str/int/...).
    if not isinstance(node, type(spec)) or isinstance(node, bool) != isinstance(spec, bool):
        raise SettingsValidationError(f"'{path}' has the wrong type")
