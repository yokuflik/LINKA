"""
Typed exceptions for the object-storage layer.

Raised by media_service, caught and turned into HTTP status codes centrally
in main.py (same pattern as auth_service / chat_service errors).
"""


class StorageError(Exception):
    """Base class for every error originating in services.storage."""


class MediaValidationError(StorageError):
    """
    The upload request is invalid on its face - unknown kind, disallowed
    MIME type, or a declared size over the per-kind ceiling. Maps to HTTP 400.
    Raised before any presigned URL is minted.
    """


class MediaNotFoundError(StorageError):
    """
    A referenced object does not exist in storage - e.g. a message send
    references a key that was never uploaded, or a download is requested for
    a key that has been removed. Maps to HTTP 404.
    """


class StorageUnavailableError(StorageError):
    """
    Storage could not be reached / a bucket operation failed unexpectedly.
    Maps to HTTP 503. Distinct from MediaNotFoundError, which is a normal
    "not there" answer rather than an outage.
    """
