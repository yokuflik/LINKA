"""User / profile / settings / E2E-public-key API models (ADR 0030)."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, model_validator

from api.schemas import IdStr
from modules.media.media_service import public_avatar_url


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    phone_number: str
    username: Optional[str] = None
    # Optional free-form nickname (ADR 0024). Shown instead of username when set;
    # never unique, never searchable.
    display_name: Optional[str] = None
    about_text: Optional[str]
    profile_pic_url: Optional[str]
    # Inline avatar thumbnail data: URI (ADR 0016) - plain passthrough.
    profile_pic_preview: Optional[str] = None

    @model_validator(mode="after")
    def _resolve_avatar_url(self):
        # profile_pic_url is stored as a storage key; expose it as a public
        # URL. Values that are already absolute URLs (legacy / seed data)
        # pass through untouched.
        key = self.profile_pic_url
        if key and not key.startswith(("http://", "https://")):
            self.profile_pic_url = public_avatar_url(key)
        return self


class UserProfileUpdateIn(BaseModel):
    about_text: Optional[str] = None
    # ADR 0017: changing the handle. Format 400, taken/cooldown/grace_hold 409 -
    # each with a machine `reason` code. Untouched field => handle unchanged.
    username: Optional[str] = None
    # ADR 0024: optional nickname. A *sent* field (present in model_fields_set)
    # sets it - or, when "" / null, clears it; an absent field leaves it as is.
    # Sanitised server-side (control/bidi/zero-width stripped, NFC, length cap).
    display_name: Optional[str] = None
    # The avatar is set through the dedicated /users/me/avatar endpoints, not
    # here - a raw client-supplied URL/key can't be trusted or cleaned up.


class PublicKeyIn(BaseModel):
    """Upload the caller's E2E public key (ADR 0026). ``public_key`` is a public
    EC/P-256 JWK; the server validates shape only and rejects a private key."""

    public_key: dict
    algo: str = "ECDH-P256"


class PublicKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: IdStr
    public_key: dict
    algo: str
    fingerprint: str


class UserSettingsOut(BaseModel):
    # Fully-resolved settings (every group/key present, defaults filled in).
    # Kept as an open dict on purpose: new setting groups are added in
    # services/settings/schema.py with no change here or in the DB.
    settings: dict


class UserSettingsUpdateIn(BaseModel):
    # A partial patch: only the groups/keys being changed. Deep-merged onto
    # the stored blob and validated server-side against the settings schema.
    settings: dict


__all__ = [
    "UserOut",
    "UserProfileUpdateIn",
    "PublicKeyIn",
    "PublicKeyOut",
    "UserSettingsOut",
    "UserSettingsUpdateIn",
]
