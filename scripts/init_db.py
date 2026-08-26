"""
Creates every table (and the DEFAULT partition the messages table needs)
against DATABASE_URL. There are no migrations yet - this is the dev/manual-
testing equivalent of what tests/conftest.py does automatically per test.

Usage:
    python3 -m scripts.init_db
    python3 -m scripts.init_db --drop
"""
import asyncio
import sys
import os
from dotenv import load_dotenv

# 1. טוען את משתני הסביבה מקובץ ה-.env לפני שמייבאים מודולים שדורשים אותם
load_dotenv()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from database.base import Base
from database.connection import DATABASE_URL
# Registers every model on Base.metadata - importing database.connection alone
# doesn't import the model modules themselves.
from database.models import chat, message, participant, private_chat_pair, user  # noqa: F401


async def main(drop: bool) -> None:
    engine = create_async_engine(DATABASE_URL)

    async with engine.begin() as conn:
        if drop:
            await conn.run_sync(Base.metadata.drop_all)
            print("Dropped all tables.")
        else:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(
                "CREATE TABLE IF NOT EXISTS messages_default PARTITION OF messages DEFAULT"
            ))
            print("Created all tables (+ messages_default partition).")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(drop="--drop" in sys.argv))