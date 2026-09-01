from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from scripts.manage_partitions import (
    TableSpec,
    cold_cutoff,
    cold_eligible,
    ensure_partitions,
    migrate_default,
    partition_suffix,
    partitions_between,
    period_start,
    run_cold,
    wanted_partitions,
)

# --- pure boundary math ---------------------------------------------------

def test_period_start_week_aligns_to_monday_utc():
    # 2026-02-11 is a Wednesday; its ISO week starts Monday 2026-02-09.
    wed = datetime(2026, 2, 11, 15, 30, tzinfo=timezone.utc)
    assert period_start("week", wed) == datetime(2026, 2, 9, tzinfo=timezone.utc)


def test_period_start_week_on_monday_is_identity():
    mon = datetime(2026, 2, 9, 0, 0, tzinfo=timezone.utc)
    assert period_start("week", mon) == mon


def test_period_start_day_truncates_to_midnight_utc():
    t = datetime(2026, 8, 29, 23, 59, 59, tzinfo=timezone.utc)
    assert period_start("day", t) == datetime(2026, 8, 29, tzinfo=timezone.utc)


def test_partition_suffix_formats():
    assert partition_suffix("week", datetime(2026, 2, 9, tzinfo=timezone.utc)) == "y2026w07"
    assert partition_suffix("day", datetime(2026, 8, 29, tzinfo=timezone.utc)) == "y2026m08d29"


def test_wanted_partitions_count_and_bounds():
    spec = TableSpec("messages", "week", precreate=6)
    now = datetime(2026, 2, 11, tzinfo=timezone.utc)
    wanted = wanted_partitions(spec, now)
    # current week + 6 ahead, inclusive
    assert len(wanted) == 7
    first_name, first_lo, first_hi = wanted[0]
    assert first_name == "messages_y2026w07"
    assert first_lo == datetime(2026, 2, 9, tzinfo=timezone.utc)
    assert first_hi == datetime(2026, 2, 16, tzinfo=timezone.utc)
    # contiguous, no gaps
    for (_, _, hi), (_, lo, _) in zip(wanted, wanted[1:]):
        assert hi == lo


def test_partitions_between_covers_closed_span():
    spec = TableSpec("message_receipt_log", "day", precreate=10)
    lo = datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc)
    hi = datetime(2026, 8, 29, 23, 0, tzinfo=timezone.utc)
    parts = partitions_between(spec, lo, hi)
    assert [p[0] for p in parts] == [
        "message_receipt_log_y2026m08d27",
        "message_receipt_log_y2026m08d28",
        "message_receipt_log_y2026m08d29",
    ]


def test_wanted_partitions_daily_receipt_log():
    spec = TableSpec("message_receipt_log", "day", precreate=10)
    now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    wanted = wanted_partitions(spec, now)
    assert len(wanted) == 11
    assert wanted[0][0] == "message_receipt_log_y2026m08d29"
    assert wanted[-1][0] == "message_receipt_log_y2026m09d08"


def test_cold_cutoff_shifts_calendar_months():
    assert cold_cutoff(datetime(2026, 8, 29, tzinfo=timezone.utc), 12) == datetime(2025, 8, 29, tzinfo=timezone.utc)
    # crossing the year boundary
    assert cold_cutoff(datetime(2026, 3, 10, tzinfo=timezone.utc), 5) == datetime(2025, 10, 10, tzinfo=timezone.utc)
    # day clamped to the shorter target month
    assert cold_cutoff(datetime(2026, 3, 31, tzinfo=timezone.utc), 1) == datetime(2026, 2, 28, tzinfo=timezone.utc)


def test_cold_eligible_filters_by_upper_bound():
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    parts = [
        ("messages_old", "FOR VALUES FROM ('2024-12-30 00:00:00+00') TO ('2025-01-06 00:00:00+00')"),
        ("messages_edge", "FOR VALUES FROM ('2025-12-25 00:00:00+00') TO ('2026-01-01 00:00:00+00')"),  # ends exactly at cutoff
        ("messages_new", "FOR VALUES FROM ('2026-01-05 00:00:00+00') TO ('2026-01-12 00:00:00+00')"),
        ("messages_default", "DEFAULT"),
    ]
    assert cold_eligible(parts, cutoff) == ["messages_old", "messages_edge"]


# --- against real Postgres ----------------------------------------------

async def _attached_partitions(conn, parent: str) -> set[str]:
    rows = (await conn.execute(text(
        """
        SELECT c.relname FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = :parent
        """
    ), {"parent": parent})).all()
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_ensure_creates_dated_partitions(session_factory):
    engine = session_factory.kw["bind"]
    now = datetime(2026, 2, 11, tzinfo=timezone.utc)
    async with engine.begin() as conn:
        created = await ensure_partitions(conn, now=now)
        msg_parts = await _attached_partitions(conn, "messages")
        rl_parts = await _attached_partitions(conn, "message_receipt_log")

    assert "messages_y2026w07" in msg_parts
    assert "messages_y2026w13" in msg_parts  # 6 weeks ahead
    assert "messages_default" in msg_parts  # safety net untouched
    assert "message_receipt_log_y2026m02d11" in rl_parts
    assert set(created) <= (msg_parts | rl_parts)


@pytest.mark.asyncio
async def test_ensure_is_idempotent(session_factory):
    engine = session_factory.kw["bind"]
    now = datetime(2026, 2, 11, tzinfo=timezone.utc)
    async with engine.begin() as conn:
        await ensure_partitions(conn, now=now)
        first = await _attached_partitions(conn, "messages")
        await ensure_partitions(conn, now=now)  # must not raise
        second = await _attached_partitions(conn, "messages")
    assert first == second


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(session_factory):
    engine = session_factory.kw["bind"]
    now = datetime(2026, 2, 11, tzinfo=timezone.utc)
    async with engine.begin() as conn:
        before = await _attached_partitions(conn, "messages")
        would = await ensure_partitions(conn, now=now, dry_run=True)
        after = await _attached_partitions(conn, "messages")
    assert before == after
    assert "messages_y2026w07" in would


@pytest.mark.asyncio
async def test_dated_partition_receives_rows_in_range(session_factory, db_session):
    engine = session_factory.kw["bind"]
    now = datetime(2026, 2, 11, tzinfo=timezone.utc)
    async with engine.begin() as conn:
        await ensure_partitions(conn, now=now)

    # A receipt row whose occurred_at falls in the dated day partition must
    # land there, not in DEFAULT.
    await db_session.execute(text(
        "INSERT INTO message_receipt_log (id, chat_id, user_id, kind, up_to_message_id, occurred_at) "
        "VALUES (1, 1, 1, 3, 1, '2026-02-11 09:00:00+00')"
    ))
    await db_session.commit()

    async with engine.begin() as conn:
        in_dated = (await conn.execute(text(
            "SELECT count(*) FROM ONLY message_receipt_log_y2026m02d11"
        ))).scalar_one()
        in_default = (await conn.execute(text(
            "SELECT count(*) FROM ONLY message_receipt_log_default"
        ))).scalar_one()
    assert in_dated == 1
    assert in_default == 0


@pytest.mark.asyncio
async def test_migrate_default_drains_into_dated_partitions(session_factory, db_session):
    engine = session_factory.kw["bind"]
    # Seed the DEFAULT partition with receipt rows across two recent days.
    day1 = "2026-08-27 10:00:00+00"
    day2 = "2026-08-28 22:00:00+00"
    rows = ", ".join(
        f"({i}, 1, 1, 3, 1, '{day1 if i % 2 else day2}')" for i in range(1, 21)
    )
    await db_session.execute(text(
        "INSERT INTO message_receipt_log (id, chat_id, user_id, kind, up_to_message_id, occurred_at) "
        f"VALUES {rows}"
    ))
    await db_session.commit()

    moved = await migrate_default(engine, batch_size=7)
    assert moved["message_receipt_log"] == 20
    assert moved["messages"] == 0  # its DEFAULT was empty

    async with engine.begin() as conn:
        in_default = (await conn.execute(text(
            "SELECT count(*) FROM ONLY message_receipt_log_default"
        ))).scalar_one()
        in_d27 = (await conn.execute(text(
            "SELECT count(*) FROM ONLY message_receipt_log_y2026m08d27"
        ))).scalar_one()
        in_d28 = (await conn.execute(text(
            "SELECT count(*) FROM ONLY message_receipt_log_y2026m08d28"
        ))).scalar_one()
        # DEFAULT must still be attached (a fresh in-range row lands nowhere-dated).
        bound = (await conn.execute(text(
            """
            SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'message_receipt_log' AND c.relname = 'message_receipt_log_default'
            """
        ))).scalar_one_or_none()

    assert in_default == 0
    assert in_d27 == 10
    assert in_d28 == 10
    assert bound is not None and "DEFAULT" in bound


@pytest.mark.asyncio
async def test_migrate_default_noop_on_empty(session_factory):
    engine = session_factory.kw["bind"]
    moved = await migrate_default(engine)
    assert moved == {"messages": 0, "message_receipt_log": 0}


async def _reloptions(conn, rel: str):
    return (await conn.execute(text(
        "SELECT reloptions FROM pg_class WHERE relname = :n"
    ), {"n": rel})).scalar_one_or_none()


@pytest.mark.asyncio
async def test_cold_freezes_only_old_messages_partitions(session_factory):
    engine = session_factory.kw["bind"]
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)  # cutoff = 2025-08-29
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE messages_y2024w10 PARTITION OF messages "
            "FOR VALUES FROM ('2024-03-04 00:00:00+00') TO ('2024-03-11 00:00:00+00')"
        ))
        await conn.execute(text(
            "CREATE TABLE messages_y2026w30 PARTITION OF messages "
            "FOR VALUES FROM ('2026-07-20 00:00:00+00') TO ('2026-07-27 00:00:00+00')"
        ))

    # dry-run touches nothing
    would = await run_cold(engine, now=now, dry_run=True)
    assert would == ["messages_y2024w10"]
    async with engine.begin() as conn:
        assert await _reloptions(conn, "messages_y2024w10") is None

    frozen = await run_cold(engine, now=now)
    assert frozen == ["messages_y2024w10"]
    async with engine.begin() as conn:
        assert "autovacuum_enabled=false" in (await _reloptions(conn, "messages_y2024w10") or [])
        assert await _reloptions(conn, "messages_y2026w30") is None  # recent, left alone

    # idempotent: already-frozen partition is skipped
    assert await run_cold(engine, now=now) == []
