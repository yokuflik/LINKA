"""`_format_history_transcript` (modules/agents/invoke_turn_helpers.py) - a
pure function, no DB/Redis needed. Covers the attachment-awareness change:
a media message with no caption used to be silently dropped from the
transcript; it must now still appear, carrying only its filename.
"""
from types import SimpleNamespace

from modules.agents.invoke_turn_helpers import _format_history_transcript
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE


def _msg(content=None, type=1, media_name=None):
    return SimpleNamespace(content=content, type=type, media_name=media_name)


def test_captionless_media_message_is_not_dropped():
    history = [_msg(content=None, type=5, media_name="product-photo.jpg")]
    transcript = _format_history_transcript(history)
    assert transcript == "Customer: [attached file: product-photo.jpg]"


def test_captioned_media_message_includes_both_caption_and_filename():
    history = [_msg(content="here's the file", type=5, media_name="invoice.pdf")]
    transcript = _format_history_transcript(history)
    assert transcript == "Customer: here's the file [attached file: invoice.pdf]"


def test_plain_text_message_unaffected():
    history = [_msg(content="hello", type=1)]
    transcript = _format_history_transcript(history)
    assert transcript == "Customer: hello"


def test_media_message_with_neither_content_nor_name_still_dropped():
    history = [_msg(content=None, type=5, media_name=None)]
    assert _format_history_transcript(history) is None


def test_already_handled_marker_still_applies_to_attachment_line():
    # get_message_history returns newest-first; the agent's reply is the
    # newer message, so it comes first here.
    history = [
        _msg(content="here you go", type=AGENT_REPLY_MESSAGE_TYPE),
        _msg(content=None, type=5, media_name="photo.jpg"),
    ]
    transcript = _format_history_transcript(history)
    lines = transcript.split("\n")
    assert lines[0] == "Customer: [attached file: photo.jpg] [already handled]"
    assert lines[1] == "Agent: here you go"


def test_long_message_is_clipped_and_does_not_crowd_out_others():
    # Newest-first: a short newest message, then a huge older one, then a short one.
    history = [
        _msg(content="newest"),
        _msg(content="X" * 5000),
        _msg(content="oldest"),
    ]
    lines = _format_history_transcript(history).split("\n")
    assert lines[0] == "Customer: oldest"
    assert "truncated" in lines[1] and len(lines[1]) < 1200
    assert lines[2] == "Customer: newest"


def test_latest_customer_message_gets_higher_cap():
    history = [_msg(content="Y" * 2500)]
    transcript = _format_history_transcript(history)
    assert transcript == "Customer: " + "Y" * 2500


def test_total_budget_drops_whole_oldest_messages_only():
    history = [_msg(content=f"m{i}-" + "Z" * 900) for i in range(20)]
    transcript = _format_history_transcript(history)
    assert transcript.startswith("…(earlier messages truncated)…\n")
    body = transcript.split("\n")[1:]
    assert all(l.startswith("Customer: m") for l in body)
    assert body[-1].startswith("Customer: m0-")
