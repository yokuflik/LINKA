"""Attachment-relevance judge (ADR 0086) - a dedicated jev classification
gate for send_attached_file, separate from the message judge in judge.py.

Different proposition entirely from the message judge's on_topic/malicious
checks: "does this specific file plausibly satisfy what the other party just
asked for", evaluated from text only (the customer's own message + the
owner's caption + the filename - never the file's actual bytes, same "no
content inspection" boundary ADR 0083 already drew). One Noul question, one
jev call, own rate bucket (ATTACHMENT_JUDGE_CALLS_PER_MINUTE) - never shares
judge.py's agent_judge_calls bucket or JEV_ON_TOPIC_THRESHOLD.

Fail-open by design, same posture as judge.py: a classification failure must
never block a legitimate resend - this is a relevance safety net, not the
hard security boundary (ADR 0083's chat_id/sender_id re-verification in
_tool_send_attached_file stays the actual access-control boundary,
untouched by this module).
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ids.client import next_id
from infra.ratelimit.service import check_and_increment
from modules.agents.models import Agent, AgentAttachmentJudgeLog
from modules.agents.typesafe_client import TypeSafeError, classify, noul_value

logger = logging.getLogger(__name__)


class AttachmentVerdict:
    __slots__ = ("is_approved", "reason")

    def __init__(self, is_approved: bool, reason: str):
        self.is_approved = is_approved
        self.reason = reason


async def evaluate_attachment_match(
    session: AsyncSession,
    agent: Agent,
    chat_id: int,
    file_id: int,
    *,
    requester_message: str,
    caption: str,
    filename: str,
) -> AttachmentVerdict:
    """Runs the attachment-relevance gate for one send_attached_file call.
    Always returns an AttachmentVerdict - never raises; a technical failure
    is logged at ERROR and reported back as approved (fail-open). Every
    verdict (approved, rejected, or fail-open) is logged to
    AgentAttachmentJudgeLog for tuning."""
    if not requester_message.strip():
        # Nothing to match against (e.g. the other party's last message was
        # media-only, or this is the very first message in the chat) -
        # approve by default rather than rejecting a match the judge never
        # actually got to evaluate.
        verdict = AttachmentVerdict(True, "no requester text to evaluate against")
        await _log_verdict(session, agent.id, chat_id, file_id, verdict)
        return verdict

    if not await check_and_increment(
        agent.id,
        "attachment_judge_calls",
        settings.ATTACHMENT_JUDGE_CALLS_PER_MINUTE,
        settings.ATTACHMENT_JUDGE_CALLS_WINDOW_SECONDS,
    ):
        logger.warning("attachment_judge: agent %s over judge call budget, failing open", agent.id)
        verdict = AttachmentVerdict(True, "attachment judge rate limit exceeded - failed open")
        await _log_verdict(session, agent.id, chat_id, file_id, verdict)
        return verdict

    questions = {
        "matches_request": {
            "type": "noul",
            "instructions": {
                "question": (
                    "The described file plausibly satisfies what the person "
                    "is asking for in their message. Judge only the file's "
                    "own filename and the owner's description of it below - "
                    "not the file's actual contents, which are not "
                    "available. Be permissive: approve when the file is a "
                    "reasonable, plausible match for the request, not only "
                    "an exact one."
                ),
                "requester_message": requester_message,
                "file_caption": caption,
                "file_name": filename,
            },
            "criteria": {
                "true": "The caption/filename plausibly describes something the person asked for",
                "false": "The caption/filename clearly describes something unrelated to the request",
            },
        }
    }

    try:
        answers = await classify(state=requester_message, questions=questions)
        matches = noul_value(answers, "matches_request") >= settings.ATTACHMENT_JUDGE_MATCH_THRESHOLD
        reason = (
            "file plausibly matches the request"
            if matches
            else "file's caption/filename does not appear to match what was asked for"
        )
        verdict = AttachmentVerdict(matches, reason)
    except (TypeSafeError, KeyError, TypeError, ValueError) as exc:
        logger.error(
            "attachment_judge: jev classification failed for agent %s chat %s file %s, failing open: %s",
            agent.id, chat_id, file_id, exc,
        )
        verdict = AttachmentVerdict(True, f"judge call failed - failed open: {exc}")

    await _log_verdict(session, agent.id, chat_id, file_id, verdict)
    return verdict


async def _log_verdict(
    session: AsyncSession, agent_id: int, chat_id: int, file_id: int, verdict: AttachmentVerdict
) -> None:
    session.add(
        AgentAttachmentJudgeLog(
            id=await next_id(),
            agent_id=agent_id,
            chat_id=chat_id,
            file_id=file_id,
            is_approved=verdict.is_approved,
            reason=verdict.reason,
        )
    )
