import os
import uuid

# Server coordinate for the test Postgres instance (usually spun up via Docker
# before running the tests). The suite never touches this database directly -
# see _ephemeral_database below and ADR 0032.
TEST_SERVER_URL = "postgresql+asyncpg://test_user:test_password@localhost:5433/test_db"

# Derive a throwaway per-run database name from the server coordinate, so a
# test run creates and drops its own database and never wipes the developer's
# seeded `test_db`. Computed here, before any project import, because
# infra.db.connection reads DATABASE_URL exactly once at import time.
_EPHEMERAL_DB_NAME = f"test_db_{uuid.uuid4().hex}"
_base, _, _ = TEST_SERVER_URL.rpartition("/")
TEST_DATABASE_URL = f"{_base}/{_EPHEMERAL_DB_NAME}"

# Must be set before infra.redis.client / infra.db.connection is imported by
# anything below (both read these once, at import time).
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from infra.db.base import Base

# Registers every model on Base.metadata regardless of which one the test
# file being run actually imports - create_all() needs the full set (e.g.
# Message's FK to chats.id fails to resolve if Chat was never imported by
# anything), and a test file that only exercises, say, crud_user has no
# reason to import Chat/Participant/Message itself.
from modules.chats.models import chat as _chat  # noqa: F401
from modules.chats.models import participant as _participant  # noqa: F401
from modules.messaging import models as _message  # noqa: F401
from modules.receipts import models as _message_receipt_log  # noqa: F401
from modules.users import models as _user  # noqa: F401
from modules.chats.models import private_chat_pair as _private_chat_pair  # noqa: F401
from modules.settings import models as _user_settings  # noqa: F401
from modules.auth import models as _reserved_username  # noqa: F401

@pytest.fixture(scope="session", autouse=True)
def _ephemeral_database():
    """
    Creates a throwaway Postgres database for this test session and drops it
    on teardown, so running the suite never disturbs the developer's seeded
    `test_db` (ADR 0032). Everything below - session_factory and, via the
    DATABASE_URL rewrite at the top of this module, infra.db.connection -
    points at this database.

    Uses a plain (sync) psycopg2-free path via asyncpg through a short-lived
    engine in AUTOCOMMIT mode against the server's default `postgres` db;
    CREATE/DROP DATABASE cannot run inside a transaction block.
    """
    import asyncio

    admin_url = f"{_base}/postgres"

    async def _run(sql: str):
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(sql))
        finally:
            await engine.dispose()

    asyncio.run(_run(f'CREATE DATABASE "{_EPHEMERAL_DB_NAME}"'))
    try:
        yield
    finally:
        asyncio.run(_run(f'DROP DATABASE IF EXISTS "{_EPHEMERAL_DB_NAME}" WITH (FORCE)'))


@pytest_asyncio.fixture(scope="function")
async def session_factory():
    # Echo=False keeps the console clean. Set to True to see the actual SQL generated.
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    # Create all tables in the test PostgreSQL database
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # "messages" is RANGE partitioned by created_at with no partitions
        # attached by create_all (SQLAlchemy only emits the parent DDL).
        # A DEFAULT partition catches every row so tests can insert freely
        # without pre-creating one partition per month.
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS messages_default PARTITION OF messages DEFAULT"
        ))
        # message_receipt_log is RANGE partitioned by occurred_at, same as
        # messages - a DEFAULT partition lets tests insert receipt rows freely.
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS message_receipt_log_default "
            "PARTITION OF message_receipt_log DEFAULT"
        ))

    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    # Exposed directly (instead of a single opened session) so concurrency
    # tests can open one independent AsyncSession/connection per coroutine -
    # a single AsyncSession is not safe to share across concurrent tasks.
    yield async_session

    # Drop all tables after the test finishes to keep the database clean
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function", autouse=True)
async def _reset_shared_singletons_after_every_test():
    """
    Tears down the pooled connections of every process-wide async singleton
    after each test, whether or not that test used it directly.

    pytest-asyncio gives each test function its own event loop, but
    services.redis_client.redis_client and database.connection.engine are
    both created once at import time and shared for the rest of the process.
    A test that touches either only *transitively* (e.g. a chat_service test
    going through message_service's Redis fan-out, or a websocket test going
    through database.connection.session_scope()) still opens connections
    bound to that test's loop; without this, the next test to touch either -
    even indirectly - would try to reuse a connection tied to a now-closed
    loop and crash with "Event loop is closed". autouse + no dependencies
    means this fixture is set up first and torn down last, so its cleanup
    always runs after redis_db's own teardown below.
    """
    yield
    from infra.redis.client import redis_client
    from infra.db.connection import engine as db_connection_engine

    await redis_client.connection_pool.disconnect()
    await db_connection_engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def redis_db():
    """
    Flushes the dedicated test Redis DB before and after each test that
    requests it - service tests don't share Postgres's per-test
    create_all/drop_all isolation, so this is what keeps presence/rate-limit/
    idempotency/OTP keys from leaking between tests.
    """
    from infra.redis.client import redis_client

    await redis_client.flushdb()
    yield redis_client
    await redis_client.flushdb()