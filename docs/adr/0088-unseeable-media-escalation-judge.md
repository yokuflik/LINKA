# 0088 - Escalate unseeable media-only messages via the existing judge call

Status: Accepted

## Context

The agent has no image/video/audio/file content inspection (ADR 0083's "no
content inspection" boundary applies everywhere, not just attachments). When
the triggering message for a turn is media-only (no `content`, e.g. a photo,
voice note, or document with no caption), `judge.py::evaluate_message` already
short-circuits to an approved verdict ("no text content to evaluate") because
there is nothing for jev to classify against `state`. The main turn then
runs normally - the model is handed a `read_history` entry that names the
media exists but can never see what it actually shows, and typically replies
with a generic acknowledgement or asks the customer to describe it in words.

That is fine when the customer follows up in text afterwards (the model gets
a real turn to react to). It is a silent dead-end when the media message is
still the *last* thing in the chat - nobody ever explained what's in the
file, and the agent has no way to move the conversation forward on its own.
That is exactly the case that should reach the owner instead of leaving the
customer unanswered.

## Decision

Fold this into the **existing** message-judge jev call (`judge.py`) instead
of adding a second gate/model call - reuses the one already-running
classification instead of spending a dedicated Gemini/jev round-trip on it,
per the owner's explicit ask to let the judge decide rather than a hard-coded
rule (saves latency + tokens).

- `evaluate_message`'s empty-content short-circuit no longer applies when the
  message carries media (`Message.media_key is not None`). Instead of
  approving blind, it builds a **fifth** atomic Noul question,
  `needs_human_review`, fed only with metadata jev is allowed to see -
  `type` (image/video/audio/file), `media_mime`, `media_name` - never file
  bytes (same boundary as ADR 0086's attachment judge). The question asks jev
  to judge, from the filename/mime/type alone plus the domain description,
  whether this kind of attachment plausibly needs an actual human to look at
  it (e.g. an ID/receipt/document/screenshot that likely requires
  verification or a judgment call) rather than something the agent can
  reasonably just acknowledge and keep the conversation going on its own.
  Still fully permissive/fail-open in spirit: ambiguous or generic filenames
  default to NOT needing a human (`needs_human_review` only fires above
  `AGENT_MEDIA_ESCALATION_THRESHOLD`, own tunable, default 0.5 - independent
  of `JEV_ON_TOPIC_THRESHOLD`/`JEV_MALICIOUS_THRESHOLD`).
- The other three questions (`prompt_injection`/`info_extraction`/
  `code_execution`) are skipped for a media-only message - there is no text
  to carry an injection/extraction/code-execution attempt in, so asking them
  would just burn tokens on a guaranteed-empty answer. `on_topic` is also
  skipped (nothing to judge as on/off-topic) - the turn proceeds to the main
  model exactly as today unless `needs_human_review` fires.
- **The "unanswered" gate stays in Python, not the model.** Even when jev
  says `needs_human_review=true`, `evaluate_message` only escalates if the
  triggering message is still the chat's latest incoming message - checked
  via the existing `modules.messaging.crud.get_latest_incoming_message`
  (already used by ADR 0086's attachment judge) comparing its `id` against
  the triggering `message_id`. If the customer has since sent a follow-up
  (text or another attachment), the turn runs normally instead - the newer
  message is what the agent should be reacting to, and a stale "can't see
  this" escalation would confuse the owner. This check is deliberately not
  asked of jev - it is a simple DB fact, not a classification judgment, and
  keeping it in Python means jev's one call stays cheap and side-effect-free.
- On `needs_human_review=true` AND "still the last message": `invoke_worker.
  py`'s existing judge-rejection branch is extended - after posting the
  normal customer-facing redirect (jev-authored, same as any rejection -
  "let them know a person will follow up" tone, still same-language,
  non-technical, per the Frontend Display Rule's spirit applied to agent
  output), it calls the same shared `tools/common.py::escalate_chat` ADR 0074
  already uses, with `notice_prefix="\U0001f4ce"` (a paperclip glyph - a
  third, distinct visual signal from the model-initiated \U0001f91d handoff
  and the malicious-intent ⚠️ alert) and a reason built server-side
  ("received a photo/video/voice note/file it can't view and can't get more
  context from the customer") rather than free jev prose, since jev's
  `needs_human_review` answer carries no reason text (Noul questions are
  boolean-scored, not free-text).
- This is a **rejection-and-redirect path** like any other judge rejection -
  `is_approved=false`, the main model turn never runs, `AgentJudgeLog` gets a
  new `needs_human_review` boolean column (default `false`) alongside the
  existing `is_malicious`, `ALTER TABLE` safety-net line in
  `scripts/init_db.py`. Fail-open is unchanged: a jev call failure on a
  media-only message now falls open to **approved without escalating** (same
  posture as every other judge failure - a technical error must never itself
  trigger owner-facing noise).

### Config-mode / schedule-fired turns unaffected

Same scope restriction as the rest of `judge.py` - only execution-mode,
message-fired turns (`message_id is not None and not config_mode_turn`) ever
reach this. The owner's own agent chat and schedule-fired turns are
untouched.

## Consequences

- No new rate bucket, no new Gemini/jev call - rides the existing
  `agent_judge_calls` budget exactly as before; a media-only message now
  consumes one jev call instead of zero (previously fully skipped), but that
  is strictly cheaper than a hypothetical dedicated second gate.
- One new `AgentJudgeLog.needs_human_review` column (`ALTER TABLE` safety
  net, same pattern ADR 0074 used for `is_malicious`).
- One new setting: `AGENT_MEDIA_ESCALATION_THRESHOLD` (default `0.5`).
- A third escalation notice glyph (\U0001f4ce) alongside \U0001f91d
  (model-initiated) and ⚠️ (malicious-intent), so the owner can
  tell at a glance which kind of handoff this is from the chat itself.
