import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

# Example: postgresql+asyncpg://user:password@host:5432/dbname
DATABASE_URL = os.environ["DATABASE_URL"]

# At billion-row scale, Postgres itself (not the app) is almost always run
# behind PgBouncer in transaction-pooling mode, so a single Postgres server
# can serve far more app instances than its own max_connections would allow.
# Transaction pooling silently breaks asyncpg's server-side prepared statement
# cache (a connection can be handed to a different client between statements),
# so USE_PGBOUNCER must disable it and let SQLAlchemy hold no pool of its own -
# PgBouncer is already the pool.
USE_PGBOUNCER = os.environ.get("DB_USE_PGBOUNCER", "false").lower() == "true"

# Sized per app instance, not for the whole fleet - with N replicas behind
# PgBouncer, real capacity planning happens on PgBouncer's/Postgres's side.
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "20"))
MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "10"))
POOL_TIMEOUT = int(os.environ.get("DB_POOL_TIMEOUT", "30"))

# Recycle connections before Postgres/a load balancer/a cloud provider's NAT
# drops them silently from under us (a stale connection surfaces as a hang,
# not a clean error, if it isn't proactively recycled).
POOL_RECYCLE_SECONDS = int(os.environ.get("DB_POOL_RECYCLE_SECONDS", "1800"))

ECHO_SQL = os.environ.get("DB_ECHO", "false").lower() == "true"

_engine_kwargs = {
    "echo": ECHO_SQL,
    "pool_pre_ping": True,  # one cheap round-trip to catch a dead connection before it fails a real query
}

if USE_PGBOUNCER:
    _engine_kwargs["poolclass"] = NullPool
    _engine_kwargs["connect_args"] = {"statement_cache_size": 0}
else:
    _engine_kwargs.update(
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        pool_recycle=POOL_RECYCLE_SECONDS,
    )

engine: AsyncEngine = create_async_engine(DATABASE_URL, **_engine_kwargs)

# expire_on_commit=False: avoids a surprise re-SELECT the next time a
# committed object's attribute is touched - cheap on a small table, but a
# real cost multiplier at this scale.
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """
    FastAPI-style dependency: yields one request-scoped session, always
    rolling back and closing it, even if the endpoint raises.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """
    Same guarantee as get_db(), for call sites outside request handling
    (background workers, scripts, message consumers).
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_database_connection() -> bool:
    """
    Cheap readiness-probe query. Does not go through the app's pool sizing
    logic above - just confirms the database is reachable at all.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database connectivity check failed: {e}")
        return False


async def dispose_engine() -> None:
    """
    Closes every pooled connection. Call this once, on app shutdown -
    leaving connections open past process exit leaks them on the Postgres
    side until it times them out.
    """
    await engine.dispose()
