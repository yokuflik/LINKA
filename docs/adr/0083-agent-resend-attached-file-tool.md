# 0083 - Agent tool to list and resend an owner-attached file

Status: Accepted

## Context

The 2026-09-28 "Attached file" work (`ai_agent_frontend.md`) let the owner
send an arbitrary file into their own owner-agent chat as a normal media
message, and made the agent aware of its filename in the history transcript
(`[attached file: <name>]`) without ever fetching/analyzing its bytes - built
explicitly as groundwork for "a future, not-yet-built tool that could let the
agent reference/resend such a file to a customer." That tool did not exist
yet: the agent had no way to turn "the owner attached a price list" into
"send that price list to this customer."

## Decision

Two new execution-mode tools (also reachable from `builder_state ==
SUPERVISOR`/`BUILDER` via the existing ADR 0062 toolset union - no separate
wiring needed there):

**1. `list_attached_files`** (`modules/agents/tools/execution.py::
_tool_list_attached_files`) - lists files available to resend: media
messages where `chat_id == agent.owner_agent_chat_id`, `sender_id ==
agent.owner_user_id`, `media_key IS NOT NULL`, not soft-deleted/purged.
Returns `{file_id, filename, caption, kind, mime, size_bytes, uploaded_at}`
per file, newest first, capped like every other list tool
(`_clamp_tool_limit`, default/max via `AGENT_TOOL_RESULT_MAX_LIMIT`).
`caption` is that `Message.content` - whatever text the owner typed
alongside the attachment (nullable, no caption is the common case) - given
to the model specifically so it can match a vague customer ask ("a picture
of the computer") against a filename that alone means nothing
(`IMG_20260928.jpg`) using whatever description the owner attached it with.
New crud query `modules/messaging/crud.py::list_attached_files` (same shape
as `get_messages_in_range`, filtered instead of range-scoped).

**2026-09-28, extends the above: proactive use in real (customer-facing)
chats.** These tools were always registered in the plain `TOOL_SCHEMAS` list
(execution mode = "any chat except `owner_agent_chat_id`", `dispatch.py::
get_tool_schemas_for_chat`) - reachable by every persona
(`sales_agent`/`support_agent`/etc.) with no extra wiring. What was missing
was the model actually thinking to use them: `CHAT_STYLE_RULES`
(`modules/agents/personas.py`, shared by every execution persona) now has an
explicit rule - when the other person asks for a photo/file/document, call
`list_attached_files` before saying no, send immediately via
`send_attached_file` on a single clear match, ask which one on multiple
matches, and never claim to send something that isn't there.

**2. `send_attached_file(chat_id, file_id, caption?)`**
(`_tool_send_attached_file`) - resends one such file into a target chat.
`file_id` is looked up via `crud.get_message_by_id(session,
chat_id=agent.owner_agent_chat_id, message_id=file_id)` - **hard-scoped to
the owner-agent chat and re-verified `sender_id == agent.owner_user_id` at
call time**, not trusted from whatever `list_attached_files` returned earlier
in the turn. Then applies the exact same restriction/quota gate as
`send_message`/`reply_message` (`can_send_messages`, `blocked_read_chat_ids`,
group/private, `_check_daily_send_quota`, `_consume_owner_send_budget`), and
calls `message_service.process_outgoing(..., type=<source message's type>,
media={"key": ..., "name": ..., "duration_seconds": ..., "blur_hash": ...},
content=caption, sender_agent_id=agent.id)`. `_validate_media` (existing
path, unchanged) re-HEADs the object and bumps `media_blob.ref_count` again -
same dedup-safe mechanics a client-side forward (ADR 0020) already relies on.

## Security boundary

The one deliberate hard restriction: **a file can only be sourced from a
media message the owner personally sent into their own owner-agent chat.**
Concretely, not reachable as a resend source:
- Knowledge-base documents (`agent_knowledge_documents`/`chunks`, ADR 0046/
  0078) - a disjoint table/pipeline, never touched by this tool.
- Media messages from any *other* chat the agent can read via
  `read_history`/`search_messages` - `get_message_by_id`'s `chat_id`
  parameter is always `agent.owner_agent_chat_id`, a fixed value from the
  `Agent` row, never model-supplied, so there is no `chat_id` argument the
  model could pass to reach another chat's attachment.
- A file the agent itself already sent elsewhere (not just the owner) -
  `sender_id == agent.owner_user_id` re-check excludes anything with
  `sender_agent_id` set.

Without this, a customer-facing execution-mode tool could otherwise become a
generic "fetch any file from any chat you can read and hand it to a third
party" primitive - a real exfiltration path across the agent's own chat
boundaries, not just an inconvenience.

## What this deliberately does not do

- **No new quota.** Resending an already-uploaded file re-runs the existing
  storage-quota accounting (`add_storage_usage` inside `_validate_media`,
  ADR 0028) and the existing per-turn/day send limits - no separate cap.
- **No content inspection.** The agent still never fetches/reads the file's
  actual bytes (per the 2026-09-28 "model awareness without analysis"
  requirement) - it only ever passes the existing `media_key` through.
- **No listing/resend from `read_history`'s transcript directly.** The
  `[attached file: <name>]` transcript line stays informational only -
  `list_attached_files` is the one path that hands the model a real,
  re-verifiable `file_id` handle.
