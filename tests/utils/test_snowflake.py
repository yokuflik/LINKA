import threading

import pytest

import utils.snowflake as sf
from utils.snowflake import SnowflakeGenerator


def test_ids_are_unique_and_monotonic_single_threaded():
    gen = SnowflakeGenerator(machine_id=1)
    ids = [gen.next_id() for _ in range(1000)]

    assert len(set(ids)) == 1000
    assert ids == sorted(ids)


def test_ids_are_unique_under_concurrent_threads():
    # Simulates many request-handling threads generating ids at once - the
    # exact scenario the generator's internal lock exists to protect against.
    gen = SnowflakeGenerator(machine_id=2)
    ids_per_thread = 500
    thread_count = 20
    results: list[int] = []
    results_lock = threading.Lock()

    def worker():
        local_ids = [gen.next_id() for _ in range(ids_per_thread)]
        with results_lock:
            results.extend(local_ids)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == ids_per_thread * thread_count
    assert len(set(results)) == len(results)


def test_different_machine_ids_never_collide_even_on_the_same_millisecond(monkeypatch):
    # Two app instances issuing ids in the very same millisecond (the normal
    # case under real load) must still never collide.
    fixed_ms = 1_800_000_000_000
    monkeypatch.setattr(SnowflakeGenerator, "_current_millis", staticmethod(lambda: fixed_ms))

    gen_a = SnowflakeGenerator(machine_id=3)
    gen_b = SnowflakeGenerator(machine_id=4)

    ids_a = {gen_a.next_id() for _ in range(5)}
    ids_b = {gen_b.next_id() for _ in range(5)}

    assert ids_a.isdisjoint(ids_b)


def test_sequence_rolls_over_to_the_next_millisecond_without_colliding(monkeypatch):
    # Shrinks the sequence space to 4 slots (0-3) so a burst of 5 ids on one
    # machine within a single millisecond is guaranteed to exhaust it and
    # force a rollover - the real 4096-slot version behaves identically,
    # just harder to actually trigger in a fast test.
    monkeypatch.setattr(sf, "_MAX_SEQUENCE", 3)

    # 4 calls to fill sequence slots 0,1,2,3 at ms=100; the 5th call's first
    # read (ts=100) triggers the wrap, and its follow-up read inside the
    # rollover wait advances to ms=101.
    clock_reads = iter([100, 100, 100, 100, 100, 101])
    monkeypatch.setattr(SnowflakeGenerator, "_current_millis", staticmethod(lambda: next(clock_reads)))

    gen = SnowflakeGenerator(machine_id=5)
    ids = [gen.next_id() for _ in range(5)]

    assert len(set(ids)) == 5


def test_clock_moving_backwards_raises_instead_of_risking_a_duplicate(monkeypatch):
    # An NTP correction (or a bad host clock) stepping time backwards must
    # never silently reuse a sequence space from the future.
    clock_reads = iter([1_800_000_000_000, 1_700_000_000_000])
    monkeypatch.setattr(SnowflakeGenerator, "_current_millis", staticmethod(lambda: next(clock_reads)))

    gen = SnowflakeGenerator(machine_id=6)
    gen.next_id()

    with pytest.raises(RuntimeError):
        gen.next_id()


@pytest.mark.parametrize("bad_machine_id", [-1, 1024, 99999])
def test_out_of_range_machine_id_rejected(bad_machine_id):
    with pytest.raises(ValueError):
        SnowflakeGenerator(machine_id=bad_machine_id)
