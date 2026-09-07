import os

# --- Usernames (ADR 0017) ---
# Unique, lowercase-canonical handle on every user. A plain unique btree index
# on users.username is the uniqueness authority; values are normalised to
# lowercase before every write. Format is validated as untrusted input and each
# rejection carries a machine `reason` code so the frontend can show a precise
# hint (too_short / too_long / bad_chars / must_start_letter / reserved / taken
# / grace_hold / cooldown).
USERNAME_MIN_LEN = int(os.environ.get("USERNAME_MIN_LEN", "3"))
USERNAME_MAX_LEN = int(os.environ.get("USERNAME_MAX_LEN", "32"))
# 3-32 chars, starts with a lowercase letter, then lowercase letters / digits /
# underscore. Kept here (not hard-coded in the validator) so the bounds and the
# regex can be tuned from one place.
USERNAME_REGEX = os.environ.get(
    "USERNAME_REGEX", r"^[a-z][a-z0-9_]{%d,%d}$" % (USERNAME_MIN_LEN - 1, USERNAME_MAX_LEN - 1)
)
# Handles nobody may register (impersonation / routing collisions).
USERNAME_RESERVED = set(
    x.strip().lower()
    for x in os.environ.get(
        "USERNAME_RESERVED",
        "admin,support,linka,me,null,system,help,info,root,about,search,settings",
    ).split(",")
    if x.strip()
)
# A user-initiated username change is refused (`cooldown`) until this many days
# after the last change. The initial auto-assignment at signup does NOT start
# this clock (users.username_changed_at stays NULL), so the first chosen
# username is free.
USERNAME_CHANGE_COOLDOWN_DAYS = int(os.environ.get("USERNAME_CHANGE_COOLDOWN_DAYS", "14"))
# When a username is released (its owner changed it), it is held in
# reserved_usernames for this many days: nobody else may take it (`grace_hold`),
# the original owner may reclaim it. Anti-impersonation.
USERNAME_RESERVED_GRACE_DAYS = int(os.environ.get("USERNAME_RESERVED_GRACE_DAYS", "14"))
# Advisory availability endpoint (GET /users/username-available): tight per-user
# sliding window so it can't be used to enumerate the users table.
USERNAME_CHECK_RATE_MAX = int(os.environ.get("USERNAME_CHECK_RATE_MAX", "20"))
USERNAME_CHECK_RATE_WINDOW_SECONDS = int(os.environ.get("USERNAME_CHECK_RATE_WINDOW_SECONDS", "60"))
# Exact-match user search (endpoint deferred, ADR 0017). Dedicated bucket for
# when it lands - exact match only, never a prefix/LIKE scan.
USERNAME_SEARCH_RATE_MAX = int(os.environ.get("USERNAME_SEARCH_RATE_MAX", "15"))
USERNAME_SEARCH_RATE_WINDOW_SECONDS = int(os.environ.get("USERNAME_SEARCH_RATE_WINDOW_SECONDS", "60"))
# How many candidate handles generate_free_username tries before widening the
# random digit suffix. Each attempt is one indexed existence check.
USERNAME_GENERATE_ATTEMPTS = int(os.environ.get("USERNAME_GENERATE_ATTEMPTS", "6"))

__all__ = [
    "USERNAME_MIN_LEN",
    "USERNAME_MAX_LEN",
    "USERNAME_REGEX",
    "USERNAME_RESERVED",
    "USERNAME_CHANGE_COOLDOWN_DAYS",
    "USERNAME_RESERVED_GRACE_DAYS",
    "USERNAME_CHECK_RATE_MAX",
    "USERNAME_CHECK_RATE_WINDOW_SECONDS",
    "USERNAME_SEARCH_RATE_MAX",
    "USERNAME_SEARCH_RATE_WINDOW_SECONDS",
    "USERNAME_GENERATE_ATTEMPTS",
]
