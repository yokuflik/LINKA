"""Resend an owner-attached file (ADR 0083, caption+judge gate ADR 0086) -
modules/agents/tools/execution.py's list_attached_files/send_attached_file.

Behavioral contract exercised here:

- list_attached_files queries scoped to (agent.owner_agent_chat_id,
  agent.owner_user_id) - never a model-supplied chat_id - and reports
  file_id/filename/kind/mime/size_bytes/uploaded_at per row.
- send_attached_file refuses (ToolDeniedError) when the file_id doesn't
  resolve to a message in the owner-agent chat, when it wasn't sent by the
  owner (e.g. sender_agent_id set / a different sender), when it has no
  media_key, has no/empty caption (ADR 0086 - grandfathered captionless
  attachments are unusable), or when it's soft-deleted/purged.
- send_attached_file applies the same restriction gate as send_message:
  can_send_messages, blocked_read_chat_ids, group/private.
- send_attached_file refuses when the attachment-relevance judge rejects the
  match (ADR 0086), and does NOT call it at all when denied earlier for any
  other reason.
- A successful call forwards the source message's media block (key, name,
  duration_seconds, blur_hash) and type into process_outgoing, targeting the
  given chat_id, with sender_agent_id=agent.id.
"""
from types import SimpleNamespace

import pytest

from modules.agents.tools import execution as execution_module
from modules.agents.tools.common import ToolDeniedError
from modules.agents.tools.execution import _tool_list_attached_files, _tool_send_attached_file

pytestmark = pytest.mark.asyncio

OWNER_AGENT_CHAT_ID = 111
OWNER_USER_ID = 42
TARGET_CHAT_ID = 777
FILE_ID = 999


def _agent(**restrictions) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        owner_user_id=OWNER_USER_ID,
        owner_agent_chat_id=OWNER_AGENT_CHAT_ID,
        restrictions=restrictions,
    )


class _FakeMessage:
    def __init__(
        self,
        id=FILE_ID,
        sender_id=OWNER_USER_ID,
        type=5,
        media_key="k1",
        media_name="pricelist.pdf",
        media_mime="application/pdf",
        media_size=1234,
        media_duration_seconds=None,
        media_blur_hash=None,
        deleted_at=None,
        purged_at=None,
        created_at=None,
        content="a caption describing the file",
    ):
        self.id = id
        self.sender_id = sender_id
        self.type = type
        self.media_key = media_key
        self.media_name = media_name
        self.media_mime = media_mime
        self.media_size = media_size
        self.media_duration_seconds = media_duration_seconds
        self.media_blur_hash = media_blur_hash
        self.deleted_at = deleted_at
        self.purged_at = purged_at
        self.created_at = created_at
        self.content = content


class _FakeSentMessage:
    def __init__(self, id=555):
        self.id = id


class _FakeIncomingMessage:
    def __init__(self, content="can you send me the price list?"):
        self.content = content


@pytest.fixture(autouse=True)
def _mock_collaborators(monkeypatch):
    state = {
        "files": [],
        "source": None,
        "is_participant": True,
        "is_group": False,
        "sent": [],
        "incoming": _FakeIncomingMessage(),
        "judge_approved": True,
        "judge_calls": [],
    }

    async def _fake_list_attached_files(session, chat_id, sender_id, limit=20):
        assert chat_id == OWNER_AGENT_CHAT_ID
        assert sender_id == OWNER_USER_ID
        return state["files"]

    async def _fake_get_message_by_id(session, chat_id, message_id):
        assert chat_id == OWNER_AGENT_CHAT_ID
        return state["source"]

    async def _fake_is_participant(session, chat_id, user_id):
        return state["is_participant"]

    async def _fake_chat_is_group(session, chat_id):
        return state["is_group"]

    async def _fake_consume_owner_send_budget(agent):
        pass

    async def _fake_process_outgoing(session, **kwargs):
        state["sent"].append(kwargs)
        return _FakeSentMessage()

    async def _fake_get_latest_incoming_message(session, chat_id, exclude_sender_id):
        assert chat_id == TARGET_CHAT_ID
        assert exclude_sender_id == OWNER_USER_ID
        return state["incoming"]

    async def _fake_evaluate_attachment_match(session, agent, chat_id, file_id, **kwargs):
        state["judge_calls"].append({"chat_id": chat_id, "file_id": file_id, **kwargs})
        return SimpleNamespace(is_approved=state["judge_approved"], reason="test reason")

    monkeypatch.setattr(execution_module, "list_attached_files", _fake_list_attached_files)
    monkeypatch.setattr(execution_module, "get_message_by_id", _fake_get_message_by_id)
    monkeypatch.setattr(execution_module, "is_participant", _fake_is_participant)
    monkeypatch.setattr(execution_module, "_chat_is_group", _fake_chat_is_group)
    monkeypatch.setattr(execution_module, "_consume_owner_send_budget", _fake_consume_owner_send_budget)
    monkeypatch.setattr(execution_module.message_service, "process_outgoing", _fake_process_outgoing)
    monkeypatch.setattr(execution_module, "get_latest_incoming_message", _fake_get_latest_incoming_message)
    monkeypatch.setattr(execution_module, "evaluate_attachment_match", _fake_evaluate_attachment_match)
    return state


@pytest.fixture
def fake_session():
    class _FakeSession:
        pass

    return _FakeSession()


# --- list_attached_files ------------------------------------------------------


async def test_list_attached_files_reports_expected_shape(_mock_collaborators, fake_session):
    import datetime

    _mock_collaborators["files"] = [
        _FakeMessage(
            created_at=datetime.datetime(2026, 9, 28, tzinfo=datetime.timezone.utc),
            content="our current price list",
        )
    ]
    agent = _agent()

    result = await _tool_list_attached_files(fake_session, agent, {})

    assert result["files"] == [
        {
            "file_id": str(FILE_ID),
            "filename": "pricelist.pdf",
            "caption": "our current price list",
            "kind": "file",
            "mime": "application/pdf",
            "size_bytes": 1234,
            "uploaded_at": "2026-09-28T00:00:00+00:00",
        }
    ]


# --- send_attached_file: the security gate ------------------------------------


async def test_send_attached_file_denied_when_source_not_found(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = None
    agent = _agent()

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )


async def test_send_attached_file_denied_when_sender_is_not_the_owner(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = _FakeMessage(sender_id=OWNER_USER_ID + 1)
    agent = _agent()

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )


async def test_send_attached_file_denied_when_caption_missing(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = _FakeMessage(content=None)
    agent = _agent()

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )
    assert _mock_collaborators["judge_calls"] == []


async def test_send_attached_file_denied_when_caption_blank(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = _FakeMessage(content="   ")
    agent = _agent()

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )


async def test_send_attached_file_denied_when_purged(_mock_collaborators, fake_session):
    import datetime

    _mock_collaborators["source"] = _FakeMessage(purged_at=datetime.datetime.now(datetime.timezone.utc))
    agent = _agent()

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )


async def test_send_attached_file_denied_when_can_send_messages_disabled(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = _FakeMessage()
    agent = _agent(can_send_messages=False)

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )


async def test_send_attached_file_denied_when_target_chat_blocked(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = _FakeMessage()
    agent = _agent(blocked_read_chat_ids=[TARGET_CHAT_ID])

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )


async def test_send_attached_file_denied_when_group_messaging_disabled(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = _FakeMessage()
    _mock_collaborators["is_group"] = True
    agent = _agent(can_message_groups=False)

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )


async def test_send_attached_file_denies_earlier_restriction_without_calling_judge(
    _mock_collaborators, fake_session
):
    _mock_collaborators["source"] = _FakeMessage()
    agent = _agent(can_send_messages=False)

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )
    assert _mock_collaborators["judge_calls"] == []


# --- send_attached_file: attachment-relevance judge (ADR 0086) ----------------


async def test_send_attached_file_denied_when_judge_rejects_match(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = _FakeMessage(content="a photo of our office")
    _mock_collaborators["judge_approved"] = False
    agent = _agent()

    with pytest.raises(ToolDeniedError):
        await _tool_send_attached_file(
            fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
        )
    assert _mock_collaborators["sent"] == []
    [call] = _mock_collaborators["judge_calls"]
    assert call["chat_id"] == TARGET_CHAT_ID
    assert call["file_id"] == FILE_ID
    assert call["caption"] == "a photo of our office"
    assert call["requester_message"] == "can you send me the price list?"


async def test_send_attached_file_judge_gets_empty_requester_text_when_no_incoming_message(
    _mock_collaborators, fake_session
):
    _mock_collaborators["source"] = _FakeMessage()
    _mock_collaborators["incoming"] = None
    agent = _agent()

    await _tool_send_attached_file(
        fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID)}
    )
    [call] = _mock_collaborators["judge_calls"]
    assert call["requester_message"] == ""


# --- send_attached_file: success path -----------------------------------------


async def test_send_attached_file_forwards_media_block_and_returns_message_id(_mock_collaborators, fake_session):
    _mock_collaborators["source"] = _FakeMessage(
        media_key="blob-key", media_name="brochure.pdf", media_duration_seconds=None, media_blur_hash="abc"
    )
    agent = _agent()

    result = await _tool_send_attached_file(
        fake_session, agent, {"chat_id": str(TARGET_CHAT_ID), "file_id": str(FILE_ID), "caption": "Here you go"}
    )

    assert result == {"message_id": "555"}
    [sent] = _mock_collaborators["sent"]
    assert sent["chat_id"] == TARGET_CHAT_ID
    assert sent["sender_id"] == OWNER_USER_ID
    assert sent["sender_agent_id"] == agent.id
    assert sent["type"] == 5
    assert sent["content"] == "Here you go"
    assert sent["media"] == {
        "key": "blob-key",
        "name": "brochure.pdf",
        "duration_seconds": None,
        "blur_hash": "abc",
    }
