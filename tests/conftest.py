import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from database.base import Base

# Registers the Message model (and its "messages" table) on Base.metadata,
# even for test files that never import it directly.
from database.models import message as _message  # noqa: F401

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