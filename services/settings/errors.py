class SettingsError(Exception):
    """Base for the settings domain."""


class SettingsValidationError(SettingsError):
    """A settings patch referenced an unknown key or an invalid value."""
