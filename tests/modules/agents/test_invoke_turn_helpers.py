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
