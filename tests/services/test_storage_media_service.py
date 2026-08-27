"""
Tests for services.storage.media_service against a real MinIO container
(docker compose `test_minio`), matching the suite's no-mocks convention.

Skips itself cleanly if MinIO isn't reachable, so it doesn't break a run
that only spun up Postgres/Redis.
"""
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from services.storage import media_service as media
from services.storage.errors import MediaNotFoundError, MediaValidationError

pytestmark = pytest.mark.asyncio


def _put(url: str, data: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    return urllib.request.urlopen(req, timeout=5).status


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _buckets_or_skip():
    try:
        await media.ensure_buckets()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"object storage not reachable: {exc}")


# --------------------------------------------------------------------------
# Validation (sync, no network)
# --------------------------------------------------------------------------
def test_unknown_kind_rejected():
    with pytest.raises(MediaValidationError):
        media.create_upload_ticket("hologram", "image/png", 10)


def test_disallowed_mime_rejected():
    with pytest.raises(MediaValidationError):
        media.create_upload_ticket("image", "application/x-msdownload", 10)


def test_oversize_declaration_rejected():
    with pytest.raises(MediaValidationError):
        media.create_upload_ticket("image", "image/png", 999 * 1024 * 1024)


def test_non_positive_size_rejected():
    with pytest.raises(MediaValidationError):
        media.create_upload_ticket("file", "application/pdf", 0)


def test_key_has_high_entropy_prefix_and_kind_segment():
    t = media.create_upload_ticket("image", "image/png", 10)
    prefix, kind, name = t.storage_key.split("/")
    assert len(prefix) == 2
    assert kind == "image"
    assert name.endswith(".png")


def test_avatar_kind_targets_avatars_bucket():
    from config import S3_BUCKET_AVATARS

    t = media.create_upload_ticket("avatar", "image/jpeg", 10)
    assert t.bucket == S3_BUCKET_AVATARS


# --------------------------------------------------------------------------
# Round trip (real MinIO)
# --------------------------------------------------------------------------
async def test_upload_then_head_then_download():
    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    t = media.create_upload_ticket("image", "image/png", len(body))

    assert _put(t.upload_url, body, t.required_headers) == 200

    meta = await media.object_metadata(t.storage_key)
    assert meta.size == len(body)
    assert meta.content_type == "image/png"

    fetched = urllib.request.urlopen(media.download_url(t.storage_key), timeout=5).read()
    assert fetched == body

    await media.delete_object(t.storage_key)
    assert await media.object_exists(t.storage_key) is False


async def test_pinned_content_length_is_enforced_by_storage():
    """A PUT that sends more bytes than the ticket authorised is rejected."""
    t = media.create_upload_ticket("file", "application/pdf", 5)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _put(
            t.upload_url,
            b"x" * 50,
            {"Content-Type": "application/pdf", "Content-Length": "50"},
        )
    assert exc.value.code == 403


async def test_object_metadata_missing_raises_not_found():
    with pytest.raises(MediaNotFoundError):
        await media.object_metadata("00/image/does-not-exist.png")


async def test_public_avatar_url_round_trips():
    body = b"\xff\xd8\xff" + b"0" * 20
    t = media.create_upload_ticket("avatar", "image/jpeg", len(body))
    assert _put(t.upload_url, body, t.required_headers) == 200

    url = media.public_avatar_url(t.storage_key)
    assert urllib.request.urlopen(url, timeout=5).status == 200

    await media.delete_object(t.storage_key, bucket=t.bucket)


@pytest.mark.parametrize("bad", [None, ""])
def test_public_avatar_url_none_for_missing_key(bad):
    assert media.public_avatar_url(bad) is None
