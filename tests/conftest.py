import os

# Must be set before services.redis_client / database.connection is imported
# by anything below (both read these once, at import time).
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test_user:test_password@localhost:5433/test_db")

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from database.base import Base

# Registers every model on Base.metadata regardless of which one the test
# file being run actually imports - create_all() needs the full set (e.g.
# Message's FK to chats.id fails to resolve if Chat was never imported by
# anything), and a test file that only exercises, say, crud_user has no
# reason to import Chat/Participant/Message itself.
from database.models import chat as _chat  # noqa: F401
from database.models import participant as _participant  # noqa: F401
from database.models import message as _message  # noqa: F401
from database.models import message_receipt_log as _message_receipt_log  # noqa: F401
from database.models import user as _user  # noqa: F401
from database.models import private_chat_pair as _private_chat_pair  # noqa: F401
from database.models import user_settings as _user_settings  # noqa: F401

# Pointing to a local PostgreSQL instance dedicated ONLY for tests
# (Usually spun up via Docker before running the tests)
TEST_DATABASE_URL = "postgresql+asyncpg://test_user:test_password@localhost:5433/test_db"
#TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

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
    from services.redis_client import redis_client
    from database.connection import engine as db_connection_engine

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
    from services.redis_client import redis_client

    await redis_client.flushdb()
    yield redis_client
    await redis_client.flushdb()