"""
Content-addressed media blob index (ADR 0010).

One row per distinct uploaded file, keyed by the sha256 of its bytes. Lets a
forwarded / re-sent file be stored in object storage exactly once: the client
hashes the file, and if the hash is already known the upload is skipped
entirely. Many `messages` rows can point at the same `storage_key`.

Not partitioned - orders of magnitude smaller than `messages` (one row per
unique file, not per message).
"""

from sqlalchemy import Column, BigInteger, Text, DateTime
from sqlalchemy.sql import func

from database.base import Base


class MediaBlob(Base):
    __tablename__ = "media_blob"

    # Lowercase hex sha256 of the raw file bytes (64 chars).
    sha256 = Column(Text, primary_key=True)

    # Object key in the private media bucket. Deterministic from the hash
    # (see services.storage.client.build_media_blob_key), so two racing
    # uploads of identical bytes target the same key - idempotent.
    storage_key = Column(Text, nullable=False)
    bucket = Column(Text, nullable=False)

    # Upload kind of the first uploader (image/video/audio/file). A later
    # sender using a different message_type is still re-checked against the
    # stored object at send time.
    kind = Column(Text, nullable=False)

    # Authoritative type/size from the storage HEAD (filled on first confirmed
    # use). mime/size are the client's declared values until then.
    mime = Column(Text, nullable=False)
    size = Column(BigInteger, nullable=False)

    # Messages currently pointing at this blob. Incremented on send; not yet
    # decremented (no lifecycle deletion - ADR 0010 / storage known-gap).
    ref_count = Column(BigInteger, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # NULL until the object is confirmed present in storage (HEAD at send time
    # or an explicit confirm). A row with uploaded_at IS NULL means a ticket
    # was minted but the bytes never landed - don't dedup against it.
    uploaded_at = Column(DateTime(timezone=True), nullable=True)
