"""ADR 0058: the agent must draw from the SAME per-user WS send_message
sliding-window budget its owner's own client consumes - never a separate or
unlimited bucket, since every agent send is stamped with the owner's own
user_id (impersonation, not a distinct service account).

Uses real (short) windows + real sleeps rather than mocking asyncio.sleep -
_consume_owner_send_budget's retry loop has no exit condition beyond the
Redis window itself aging out, so a no-op mocked sleep (time never advances)
would spin it forever. Matches the pattern already used in
tests/infra/ratelimit/test_rate_limit_sliding_window.py.
"""
import asyncio
from types import SimpleNamespace

import pytest

from config import agent_settings, security_settings
from infra.ratelimit.service import check_sliding_window
from modules.agents.tools import common as agent_tools_common

pytestmark = pytest.mark.asyncio


def _agent(owner_user_id: int) -> SimpleNamespace:
    return SimpleNamespace(owner_user_id=owner_user_id)


async def test_passes_immediately_when_owner_has_headroom(redis_db, monkeypatch):
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_RATE_MAX", 5)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_RATE_WINDOW_SECONDS", 10)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_BURST_MAX", 40)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_BURST_WINDOW_SECONDS", 60)

    calls = []
    monkeypatch.setattr(agent_tools_common.asyncio, "sleep", _tracking_sleep(calls))

    await agent_tools_common._consume_owner_send_budget(_agent(owner_user_id=1))
    assert calls == []


def _tracking_sleep(calls):
    real_sleep = asyncio.sleep

    async def _sleep(seconds):
        calls.append(seconds)
        await real_sleep(seconds)

    return _sleep


async def test_consumes_the_same_bucket_the_owners_own_ws_client_uses(redis_db, monkeypatch):
    # Short, real window: the agent's retry must wait for it to age out for
    # real, proving it is bound by the same clock the owner's own WS client
    # would be bound by - not a mocked/frozen one.
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_RATE_MAX", 1)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_RATE_WINDOW_SECONDS", 1)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_BURST_MAX", 40)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_BURST_WINDOW_SECONDS", 60)
    monkeypatch.setattr(agent_settings, "AGENT_SEND_RATE_LIMIT_BACKOFF_MS", 200)
    monkeypatch.setattr(agent_settings, "AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS", 200)

    owner_id = 2
    agent = _agent(owner_user_id=owner_id)

    # Exhaust the primary window exactly as the Rust ws_gateway would for a
    # real WS send from the owner's own client - same key, same action name.
    assert await check_sliding_window(owner_id, "send_message", 1, 1) is True

    calls = []
    monkeypatch.setattr(agent_tools_common.asyncio, "sleep", _tracking_sleep(calls))

    # Must retry at least once (the owner's bucket is full) before the 1s
    # window ages out and it can finally proceed.
    await agent_tools_common._consume_owner_send_budget(agent)
    assert len(calls) >= 1


async def test_different_owners_have_independent_buckets(redis_db, monkeypatch):
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_RATE_MAX", 1)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_RATE_WINDOW_SECONDS", 5)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_BURST_MAX", 40)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_BURST_WINDOW_SECONDS", 60)

    await check_sliding_window(10, "send_message", 1, 5)

    calls = []
    monkeypatch.setattr(agent_tools_common.asyncio, "sleep", _tracking_sleep(calls))

    # Owner 11's bucket is untouched, so their agent must not wait at all,
    # even though owner 10's bucket (a different agent) is exhausted.
    await agent_tools_common._consume_owner_send_budget(_agent(owner_user_id=11))
    assert calls == []


async def test_backoff_doubles_on_repeated_rejection(redis_db, monkeypatch):
    # Long real window (well past this test's runtime) so every check stays
    # rejected regardless of wall-clock time - isolates the assertion to the
    # backoff sequence's shape rather than racing a real expiry.
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_RATE_MAX", 1)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_RATE_WINDOW_SECONDS", 60)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_BURST_MAX", 40)
    monkeypatch.setattr(security_settings, "WS_SEND_MESSAGE_BURST_WINDOW_SECONDS", 60)
    monkeypatch.setattr(agent_settings, "AGENT_SEND_RATE_LIMIT_BACKOFF_MS", 10)
    monkeypatch.setattr(agent_settings, "AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS", 1000)

    owner_id = 30
    agent = _agent(owner_user_id=owner_id)
    await check_sliding_window(owner_id, "send_message", 1, 60)

    sleep_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 3:
            raise RuntimeError("stop-after-3-backoffs")

    monkeypatch.setattr(agent_tools_common.asyncio, "sleep", _fake_sleep)

    with pytest.raises(RuntimeError, match="stop-after-3-backoffs"):
        await agent_tools_common._consume_owner_send_budget(agent)

    expected_first_s = 10 / 1000
    assert sleep_calls[0] == pytest.approx(expected_first_s)
    assert sleep_calls[1] == pytest.approx(expected_first_s * 2)
    assert sleep_calls[2] == pytest.approx(expected_first_s * 4)
