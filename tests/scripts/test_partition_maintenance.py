import pytest

from scripts import partition_maintenance as pm


class _FakeConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        return None


class _FakeEngine:
    def begin(self):
        return _FakeConn()

    async def dispose(self):
        return None


def test_bad_invocation_returns_usage_code():
    assert pm.main([]) == 2
    assert pm.main(["bogus"]) == 2
    assert pm.main(["ensure", "cold"]) == 2  # two positionals


@pytest.mark.parametrize("job,expected", [
    ("ensure", ("ensure", True)),
    ("report", ("report", None)),
    ("cold", ("cold", True)),
    ("prune-receipts", ("prune", True)),
])
def test_each_keyword_dispatches_to_its_action(monkeypatch, job, expected):
    calls = []

    async def fake_ensure(conn, dry_run=False):
        calls.append(("ensure", dry_run))

    async def fake_report(conn):
        calls.append(("report", None))

    async def fake_cold(engine, dry_run=False):
        calls.append(("cold", dry_run))

    async def fake_prune(dry_run):
        calls.append(("prune", dry_run))

    monkeypatch.setattr(pm.manage_partitions, "ensure_partitions", fake_ensure)
    monkeypatch.setattr(pm.manage_partitions, "report", fake_report)
    monkeypatch.setattr(pm.manage_partitions, "run_cold", fake_cold)
    monkeypatch.setattr(pm.prune_receipt_log, "main", fake_prune)
    monkeypatch.setattr(pm, "create_async_engine", lambda url: _FakeEngine())

    assert pm.main([job, "--dry-run"]) == 0
    assert calls == [expected]


def test_failure_in_a_job_maps_to_exit_1(monkeypatch):
    async def boom(conn, dry_run=False):
        raise RuntimeError("db down")

    monkeypatch.setattr(pm.manage_partitions, "ensure_partitions", boom)
    monkeypatch.setattr(pm, "create_async_engine", lambda url: _FakeEngine())

    assert pm.main(["ensure"]) == 1
