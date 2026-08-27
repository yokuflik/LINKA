"""
Creates the object-storage buckets (media + avatars) against the configured
S3 / MinIO endpoint and sets the avatars bucket public-read.

The dev/manual-testing equivalent of scripts/init_db.py for object storage.
Safe to re-run - existing buckets and an already-set policy are a no-op.
The app also calls services.storage.media_service.ensure_buckets() on
startup, so running this by hand is only needed if you want the buckets
before the server boots (e.g. to upload fixtures).

Usage:
    python3 -m scripts.init_storage
"""
import asyncio

from dotenv import load_dotenv

# Load .env before importing modules that read config at import time.
load_dotenv()

from config import (  # noqa: E402
    S3_BUCKET_AVATARS,
    S3_BUCKET_MEDIA,
    S3_ENDPOINT_URL,
)
from services.storage.media_service import ensure_buckets  # noqa: E402


async def main() -> None:
    print(f"Ensuring buckets on {S3_ENDPOINT_URL or 'aws-s3'} ...")
    await ensure_buckets()
    print(f"  ready: {S3_BUCKET_MEDIA} (private)")
    print(f"  ready: {S3_BUCKET_AVATARS} (public-read)")


if __name__ == "__main__":
    asyncio.run(main())
