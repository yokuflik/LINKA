"""Exception types raised across the chat / group-membership flows."""


class PermissionDeniedError(Exception):
    pass


class TooManyMembersError(Exception):
    pass


class UserNotFoundError(Exception):
    pass


class OwnershipTransferRequiredError(Exception):
    """
    Raised when the owner tries to leave a group that still has other
    members without naming who inherits ownership - a group can never be
    left ownerless while people remain in it.
    """
    pass
