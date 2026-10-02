"""Bulk chat summarization confirmation gate (ADR 0072) -
modules/agents/tools/execution.py's count_messages_in_range/bulk_fetch_messages.

Behavioral contract exercised here:

- count_messages_in_range never touches message content, only a count. When
  the count exceeds AGENT_BULK_FETCH_MAX_MESSAGES it reports too_large=true
  and clears any prior pending_confirmation - never stashes anything.
- When the count is within the cap, it stashes a pending_confirmation on the
  agent (tool/chat_id/start_at/end_at/count/created_at) and reports
  needs_confirmation=true - it never calls bulk_fetch_messages itself.
- bulk_fetch_messages refuses (ToolDeniedError) when there is no matching,
  unexpired pending_confirmation for the exact chat_id/date range - this is
  the hard, server-side gate, independent of whatever the model claims.
- bulk_fetch_messages refuses when pending_confirmation is for a different
  chat_id or a different date range than the call's actual arguments.
- bulk_fetch_messages refuses when the stashed confirmation has expired
  (older than AGENT_PENDING_CONFIRMATION_TTL_MINUTES).
- bulk_fetch_messages re-counts at call time and refuses if the true count
  now exceeds the cap, even with a matching pending_confirmation (closes the
  race where messages arrived between confirmation and fetch), and clears
  pending_confirmation when it does.
- A successful bulk_fetch_messages call clears pending_confirmation (so a
  second call without a fresh confirmation is refused) and returns messages
  oldest-first with masked sender identity (never raw sender_id/chat_id).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from modules.agents.tools import execution as execution_module
from modules.agents.tools.common import ToolDeniedError
from modules.agents.tools.execution import _tool_bulk_fetch_messages, _tool_count_messages_in_range

pytestmark = pytest.mark.asyncio

CHAT_ID = 777


def _agent(pending_confirmation=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        owner_user_id=42,
        restrictions={},
        pending_confirmation=pending_confirmation,
    )


class _FakeMessage:
    _next_id = 1

    def __init__(self, sender_id, content, created_at):
        self.id = _FakeMessage._next_id
        _FakeMessage._next_id += 1
        self.sender_id = sender_id
        self.content = content
        self.created_at = created_at


@pytest.fixture(autouse=True)
def _mock_collaborators(monkeypatch):
    """Isolates the gate logic from the DB/identity-masking - only the
    functions this ADR's logic actually decides on are faked."""
    state = {"count": 0, "messages": []}

    async def _fake_is_participant(session, chat_id, user_id):
        return True

    async def _fake_count(session, chat_id, user_id, start_at=None, end_at=None):
        return state["count"]

    async def _fake_get_messages(session, chat_id, user_id, start_at=None, end_at=None, limit=1000, after_id=None):
        return [m for m in state["messages"] if after_id is None or m.id > after_id]

    async def _fake_resolve_sender_labels(session, sender_ids):
        return {str(sid): {"name": f"user{sid}", "phone_number": "+100"} for sid in sender_ids if sid is not None}

    monkeypatch.setattr(execution_module, "is_participant", _fake_is_participant)
    monkeypatch.setattr(execution_module, "count_messages_in_range", _fake_count)
    monkeypatch.setattr(execution_module, "get_messages_in_range", _fake_get_messages)
    monkeypatch.setattr(execution_module, "_resolve_sender_labels", _fake_resolve_sender_labels)
    return state


@pytest.fixture
def fake_session():
    class _FakeSession:
        async def flush(self):
            pass

    return _FakeSession()


# --- count_messages_in_range --------------------------------------------------


async def test_count_over_cap_reports_too_large_and_does_not_stash(_mock_collaborators, fake_session):
    _mock_collaborators["count"] = 1500
    agent = _agent(pending_confirmation={"tool": "stale"})

    result = await _tool_count_messages_in_range(fake_session, agent, {"chat_id": str(CHAT_ID)})

    assert result["too_large"] is True
    assert result["count"] == 1500
    assert agent.pending_confirmation is None


async def test_count_within_cap_stashes_pending_confirmation(_mock_collaborators, fake_session):
    _mock_collaborators["count"] = 250
    agent = _agent()

    result = await _tool_count_messages_in_range(fake_session, agent, {"chat_id": str(CHAT_ID)})

    assert result["needs_confirmation"] is True
    assert result["too_large"] is False
    assert agent.pending_confirmation["tool"] == "bulk_fetch_messages"
    assert agent.pending_confirmation["chat_id"] == str(CHAT_ID)
    assert agent.pending_confirmation["count"] == 250


# --- bulk_fetch_messages: the hard gate --------------------------------------


async def test_bulk_fetch_denied_with_no_pending_confirmation(_mock_collaborators, fake_session):
    _mock_collaborators["count"] = 10
    agent = _agent(pending_confirmation=None)

    with pytest.raises(ToolDeniedError):
        await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})


async def test_bulk_fetch_denied_when_pending_is_for_a_different_chat(_mock_collaborators, fake_session):
    _mock_collaborators["count"] = 10
    agent = _agent(
        pending_confirmation={
            "tool": "bulk_fetch_messages",
            "chat_id": str(CHAT_ID + 1),
            "start_at": None,
            "end_at": None,
            "count": 10,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    with pytest.raises(ToolDeniedError):
        await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})


async def test_bulk_fetch_denied_when_pending_confirmation_expired(_mock_collaborators, fake_session):
    _mock_collaborators["count"] = 10
    stale_created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    agent = _agent(
        pending_confirmation={
            "tool": "bulk_fetch_messages",
            "chat_id": str(CHAT_ID),
            "start_at": None,
            "end_at": None,
            "count": 10,
            "created_at": stale_created_at.isoformat(),
        }
    )

    with pytest.raises(ToolDeniedError):
        await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})


async def test_bulk_fetch_denied_when_count_grew_past_cap_since_confirmation(_mock_collaborators, fake_session):
    agent = _agent(
        pending_confirmation={
            "tool": "bulk_fetch_messages",
            "chat_id": str(CHAT_ID),
            "start_at": None,
            "end_at": None,
            "count": 100,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _mock_collaborators["count"] = 5000  # grew past the cap in the meantime

    with pytest.raises(ToolDeniedError):
        await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})

    assert agent.pending_confirmation is None


async def test_bulk_fetch_succeeds_with_matching_confirmation_and_clears_it(_mock_collaborators, fake_session):
    now = datetime.now(timezone.utc)
    agent = _agent(
        pending_confirmation={
            "tool": "bulk_fetch_messages",
            "chat_id": str(CHAT_ID),
            "start_at": None,
            "end_at": None,
            "count": 2,
            "created_at": now.isoformat(),
        }
    )
    _mock_collaborators["count"] = 2
    _mock_collaborators["messages"] = [
        _FakeMessage(sender_id=1, content="first", created_at=now - timedelta(minutes=1)),
        _FakeMessage(sender_id=2, content="second", created_at=now),
    ]

    result = await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})

    assert result["count"] == 2
    assert [m["content"] for m in result["messages"]] == ["first", "second"]
    assert agent.pending_confirmation is None


async def test_bulk_fetch_denied_a_second_time_without_a_fresh_confirmation(_mock_collaborators, fake_session):
    now = datetime.now(timezone.utc)
    agent = _agent(
        pending_confirmation={
            "tool": "bulk_fetch_messages",
            "chat_id": str(CHAT_ID),
            "start_at": None,
            "end_at": None,
            "count": 1,
            "created_at": now.isoformat(),
        }
    )
    _mock_collaborators["count"] = 1
    _mock_collaborators["messages"] = [_FakeMessage(sender_id=1, content="only", created_at=now)]

    await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})
    assert agent.pending_confirmation is None

    with pytest.raises(ToolDeniedError):
        await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})


def _confirmed_agent(now, count):
    return _agent(
        pending_confirmation={
            "tool": "bulk_fetch_messages",
            "chat_id": str(CHAT_ID),
            "start_at": None,
            "end_at": None,
            "count": count,
            "created_at": now.isoformat(),
        }
    )


async def test_bulk_fetch_truncates_long_messages(_mock_collaborators, fake_session, monkeypatch):
    monkeypatch.setattr("config.agent_settings.AGENT_BULK_FETCH_MESSAGE_MAX_CHARS", 10)
    now = datetime.now(timezone.utc)
    agent = _confirmed_agent(now, 1)
    _mock_collaborators["count"] = 1
    _mock_collaborators["messages"] = [_FakeMessage(1, "x" * 25, now)]

    result = await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})

    assert result["messages"][0]["content"] == "x" * 10 + " …[truncated 15 chars]"
    assert result["has_more"] is False


async def test_bulk_fetch_pages_over_char_budget_and_keeps_confirmation(_mock_collaborators, fake_session, monkeypatch):
    monkeypatch.setattr("config.agent_settings.AGENT_BULK_FETCH_MAX_CHARS", 10)
    now = datetime.now(timezone.utc)
    agent = _confirmed_agent(now, 3)
    _mock_collaborators["count"] = 3
    msgs = [_FakeMessage(1, "aaaaaa", now), _FakeMessage(1, "bbbbbb", now), _FakeMessage(1, "cc", now)]
    _mock_collaborators["messages"] = msgs

    page1 = await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID)})
    assert [m["content"] for m in page1["messages"]] == ["aaaaaa"]
    assert page1["has_more"] is True
    assert page1["next_after_message_id"] == str(msgs[0].id)
    assert agent.pending_confirmation is not None  # no re-confirmation for the next page

    page2 = await _tool_bulk_fetch_messages(
        fake_session, agent, {"chat_id": str(CHAT_ID), "after_message_id": page1["next_after_message_id"]}
    )
    assert [m["content"] for m in page2["messages"]] == ["bbbbbb", "cc"]
    assert page2["has_more"] is False
    assert agent.pending_confirmation is None


async def test_bulk_fetch_rejects_bad_after_message_id(_mock_collaborators, fake_session):
    now = datetime.now(timezone.utc)
    agent = _confirmed_agent(now, 1)
    _mock_collaborators["count"] = 1

    with pytest.raises(ToolDeniedError):
        await _tool_bulk_fetch_messages(fake_session, agent, {"chat_id": str(CHAT_ID), "after_message_id": "abc"})
