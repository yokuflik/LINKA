"""
Object-storage layer for Linka.

Everything that talks to S3 / MinIO lives here, kept out of the business-logic
service modules. The app server never streams file bytes: it only mints
short-lived presigned URLs (clients upload/download directly against storage)
and records the resulting object keys as message / avatar metadata.

Modules:
  - client:        the boto3 (sync, signing-only) and aioboto3 (async, network)
                   client factories, plus a small key generator.
  - media_service: the public API - upload tickets, download URLs, object
                   existence / deletion. This is what routers and services call.
  - errors:        typed exceptions, mapped to HTTP status codes in main.py.
"""
