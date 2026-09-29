# 0086 - Mandatory attachment caption + pre-send relevance judge for `send_attached_file`

Status: Accepted

## Context

ADR 0083 gave the agent `list_attached_files`/`send_attached_file`: the owner
drops a file into their own owner-agent chat, the model later resends it to a
customer when it thinks the request matches. The only signal the model has to
decide "does this file match what the customer asked for" is `filename` +
`caption` (`Message.content` on the owner's attach message) - and `caption` is
**optional** today (`poc/components/AgentChatView.js`'s `submit()`: "Combined
send: caption (may be empty)"). A bare `IMG_20260928.jpg` with no caption
gives the model nothing to match against but a meaningless filename, which is
exactly the failure mode raised: the agent guessing wrong and sending a
customer a photo/document that has nothing to do with what they asked for.

ADR 0083 already closed the *security* boundary (a file can only be sourced
from the owner's own attach message, never another chat/customer/knowledge
base). It explicitly did not address *relevance* - whether the one matching
file the model picked is actually the right one for this specific request.

## Decision

Two independent, additive layers - the first makes a wrong match harder to
create, the second catches a wrong match that gets made anyway.

### 1. Caption becomes mandatory at attach time

A file attached in the owner-agent chat is not usable by `send_attached_file`
unless the owner described what it is at upload time. Concretely:

- **Frontend** (`poc/components/AgentChatView.js::submit()`): when
  `stagedAttachment` is set, `text.trim()` must be non-empty - the existing
  `if (!text && !this.stagedAttachment) return;` early-return is replaced with
  a rule that blocks the combined send (inline validation message, matching
  this project's Frontend Display Rule of no raw technical errors) rather than
  silently emitting `send-attachment` with an empty caption.
- **Backend backstop** (`modules/messaging/send.py::process_outgoing`): when
  `chat_id == ` the sender's own `Agent.owner_agent_chat_id` (looked up via
  the existing `modules.agents.crud` accessor keyed on `owner_user_id`) and
  `media` is present, `content` must be non-empty after stripping - raises the
  existing `modules.media.errors.MediaValidationError` otherwise (same
  exception class `_validate_media` already raises for a bad media payload,
  so the WS error path handles it with zero new plumbing). This is the actual
  enforcement point - the frontend check is convenience, not the boundary,
  same convention as every other "ask before backend changes" rule in this
  project: the check must hold even if the PoC is bypassed or a future
  client forgets it.
- Existing attachments already in the DB with a null/empty caption are
  **grandfathered as unusable**, not backfilled: `list_attached_files`
  (`modules/messaging/crud.py`) adds `Message.content IS NOT NULL AND
  Message.content != ''` to its existing filter, so an old captionless file
  simply stops appearing as a candidate - no migration, no retroactive
  prompt to the owner.

This does not touch any other media path (normal chat messages, group chats,
knowledge-base uploads) - the non-empty-caption rule is scoped to `chat_id ==
owner_agent_chat_id` only, since a caption is meaningless overhead everywhere
else.

### 2. Pre-send relevance judge on `send_attached_file`

Even with a caption, the model can still misjudge a match ("a picture of the
computer" against a caption "our office setup" could go either way). Add a
narrow, cheap second opinion **between the model's tool call and the actual
send** - a fully separate, dedicated gate, deliberately NOT a reuse of
`judge.py`'s message-content gate (explicit requirement: the message judge
must not be the one deciding attachment relevance - different proposition,
different call site, own budget):

- New standalone module `modules/agents/attachment_judge.py::
  evaluate_attachment_match(session, agent, chat_id, file_id, *,
  requester_message, caption, filename) -> AttachmentVerdict`. One `jev`
  classification call via the same `typesafe_client.classify` transport
  `judge.py` uses, but with its **own** rate bucket
  (`attachment_judge_calls:{agent_id}`, `ATTACHMENT_JUDGE_CALLS_PER_MINUTE`/
  `_WINDOW_SECONDS`) - never shares `judge.py`'s `agent_judge_calls` bucket.
  A single atomic `matches_request` Noul question (with `criteria` for the
  true/false boundary, per TypeSafe's own guidance): does the described file
  plausibly satisfy what the other party just asked for, given only their
  latest message text + the owner's caption + the filename - thresholded by
  its own `ATTACHMENT_JUDGE_MATCH_THRESHOLD` (default 0.5), independent of
  `judge.py`'s `JEV_ON_TOPIC_THRESHOLD`. No chat history, no tool schemas, no
  image bytes (ADR 0083's "no content inspection" boundary stays intact).
- The "requester message" is fetched via a new
  `modules.messaging.crud.get_latest_incoming_message(session, chat_id,
  exclude_sender_id)` - the most recent non-deleted message in the *target*
  chat (the one the file is being sent into) not sent by the owner. An empty
  result (media-only last message, or no prior message at all) auto-approves
  without ever calling `jev` - there's nothing to judge against.
- Wired into `_tool_send_attached_file`
  (`modules/agents/tools/execution.py`), right after the existing
  `source`/ownership/restriction checks (including the new caption check
  above - old captionless rows are excluded there too, same as
  `list_attached_files`) and before `process_outgoing` is called. A negative
  verdict raises `ToolDeniedError(f"attachment does not appear to match the
  request: {verdict.reason}")` - a normal tool-error return the model sees
  and must react to (ask a clarifying question, call `list_attached_files`
  again, or say it doesn't have a matching file), exactly like any other
  `ToolDeniedError` today; the tool's schema `description`
  (`modules/agents/tools/schemas.py`) was updated to tell the model this can
  happen and how to react.
- **Fail-open**, same posture as `judge.py`: a `TypeSafeError`/malformed
  response does not block the send - a classification outage must never make
  a legitimate resend silently stop working. Logged at `ERROR`, verdict
  defaults to approved. The rate limit itself being exceeded fails open the
  same way.
- One row per check written to a new, separate `AgentAttachmentJudgeLog`
  table (`modules/agents/models.py`) - not `AgentJudgeLog` - since this is an
  independent gate with its own question/call-site/budget, its audit trail
  stays independent too. Picked up automatically by
  `Base.metadata.create_all` (brand-new table, no `ALTER TABLE` needed, same
  as `AgentJudgeLog` when ADR 0053 first landed).

No new Gemini call is introduced anywhere in this ADR - `jev` is a
classifier, not a generator, and rejection here needs no customer-facing
redirect text (the customer never sees a rejected `send_attached_file` call;
the model just doesn't send the file and continues the conversation in its
own words, same as any other tool it decided not to call).

## What this deliberately does not do

- **No image/vision-based validation.** The actual bytes are never inspected,
  consistent with ADR 0083's existing "no content inspection" line - this
  stays a text-matching problem (customer request vs. owner's own
  description), not a computer-vision one. Revisit only if text-level
  mismatches keep happening in practice.
- **No owner confirmation gate before sending.** Unlike ADR 0072's bulk-fetch
  confirmation, a matched-and-approved attachment still sends immediately -
  requiring the owner to approve every single file send would defeat the
  point of an autonomous agent. The two layers above are meant to make a bad
  match rare, not to add a human in the loop for the common case.
- **No retroactive caption backfill** for already-attached files - see above.

## Consequences

- An owner attaching a file into their own agent chat now must type a short
  description every time - a small new friction, by design; this is the
  cheapest and highest-leverage fix since the model otherwise has nothing
  else to go on.
- `send_attached_file` gains one extra Redis-rate-limited classification call
  per invocation - same order of cost as the existing message judge, not a
  new class of expense, and on its own independent budget.
- New table `agent_attachment_judge_log` - picked up by `create_all`, no
  migration/`ALTER TABLE` needed.
