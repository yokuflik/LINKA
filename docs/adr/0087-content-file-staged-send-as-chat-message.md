# 0087 - Content file (knowledge doc) staged like a normal attachment, sent as a real chat message

Status: Accepted

## Context

The owner-agent chat's `[+]` menu has two entries with very different UX:

- **"Attached file"** (`stage-attachment`) - picks a file, shows a preview
  strip above the composer with an `x` to cancel, lets the owner type a
  caption, and only uploads/sends on pressing Send. It becomes a normal type
  5 file message in the chat (ADR 0083), resendable to customers via
  `list_attached_files`/`send_attached_file`.
- **"Content file (txt/pdf)"** (`pick-knowledge-file`) - uploads immediately
  on file selection, with no staging/preview/cancel/caption step, straight
  into the knowledge-base pipeline (`useKnowledgeUpload.js::
  uploadKnowledgeFile` -> `POST /agents/me/knowledge`, ADR 0046 decision 4 /
  ADR 0078). No message is ever posted to the chat by the owner for this
  path - the only visible trace is the agent's own after-the-fact ingestion
  notice (ADR 0085).

This asymmetry reads as a bug from the owner's side (reported as "the PDF
just disappears, no box with an x, nothing in the chat") even though it was
working as designed - and it removes the owner's ability to cancel or add
context before a knowledge document is committed.

## Decision

Unify the front-end UX: **"Content file" now stages exactly like "Attached
file"** (same `stagedAttachment` preview strip, same cancel-`x`, same
caption-before-send). On Send, the file is:

1. Uploaded and sent as a normal type 5 chat message via the existing
   `sendMediaMessage` path (same as any other attachment) - so it appears in
   the chat immediately, like a regular file the owner sent themself, and
   stays reachable by `list_attached_files`/`send_attached_file` (ADR 0083)
   with no change to that tool or its security boundary.
2. **Also** committed to the knowledge base via the existing, separate
   `agent_knowledge` bucket pipeline (`create_knowledge_upload_ticket` /
   `POST /agents/me/knowledge`) - **not** the same `storage_key` as the chat
   message. `create_knowledge_upload_ticket` puts into the private
   `agent_knowledge` bucket and text/markdown's server-side chunking
   (`_fetch_text_from_s3`) hardcodes that bucket, so a messaging-media key
   (general chat bucket, ADR 0010 dedup) is not interchangeable with it - two
   PUTs of the same bytes, one per bucket, same as today's two independent
   flows, just both fired from one Send action instead of one firing on pick.
   PDF chunks are still computed client-side (`pdf.js`) exactly as today;
   txt/md are still server-chunked from the `agent_knowledge`-bucket copy.

The staged preview keeps one small addition: a toggle/label distinguishing
"send as regular file" vs "send + add to knowledge base" is **not** added -
the menu entry itself (`[+] -> Content file` vs `Attached file`) remains the
only way to pick intent, exactly as today. Staging just makes both paths
share the same cancel/caption UX; it does not merge the two menu entries into
one.

## What changes

- `poc/composables/useKnowledgeUpload.js`: `uploadKnowledgeFile` no longer
  fires on file-pick. File-pick now calls `ctx.stageAttachment(file,
  'knowledge')` (new `forceKind`-style marker, or a parallel
  `stageKnowledgeAttachment` - implementation detail for the build step) so
  it lands in the same `stagedAttachment` ref `AgentChatView.js` already
  renders.
- `poc/composables/useAgentConfig.js::sendAgentAttachment` (the owner-agent
  chat's actual send path - `useMediaUpload.js::sendStagedAttachment` is the
  main-chat composer's parallel, unaffected path): after the staged file's
  `send_message` fires, if the staged item is flagged `isKnowledge`, also
  runs the existing `uploadKnowledgeFile` upload-ticket + PUT + commit
  sequence against the **same local `File` object** (already held in memory,
  so no re-fetch needed) - two independent uploads of the same bytes, one per
  bucket, both awaited before `sendAgentAttachment` resolves.
- `modules/agents/router.py` / `modules/agents/knowledge_service.py`: **no
  backend change required.** Both endpoints are called exactly as they are
  today, just both triggered from the Send button instead of one firing on
  file-pick.
- ADR 0083's security boundary is unaffected: `send_attached_file` still only
  ever sources from `Message` rows in the owner-agent chat: a
  knowledge-sourced file is now *also* such a row (it wasn't before), which
  only adds a legitimate resend option, not a new exfiltration path.

## Sequencing detail

PDF text extraction (`pdf.js`, fully client-side) runs **at stage-time**
(on file-pick, same as today), not at send-time: the staged file is already
in memory, so extraction can start immediately and a scanned/no-text PDF can
still be caught and reported (ADR 0085's `report-failure` path) before the
owner even reaches the Send button - `stageAgentAttachment` gains an
async pre-check for `isKnowledge` files specifically (image/video/plain
attachments skip it entirely, no behavior change there). If extraction
fails, the file is never staged - same "no preview, no send" outcome the
instant-upload path has today, just surfaced at pick-time via a toast
instead of at commit-time via the agent's own chat notice.

## What this deliberately does not do

- No new `Message`/`AgentKnowledgeDocument` schema field marking "this
  message is also a knowledge doc" - the link is discoverable (same
  `storage_key`/`media_key`) but not stored as an explicit FK; not needed by
  any current read path.
- No change to `list_knowledge_documents`, `search_knowledge_semantic`,
  `get_knowledge_index`, or any other knowledge-read tool.
- No change to the per-agent knowledge quota (`check_knowledge_quota`) or
  storage quota (`add_storage_usage`) - both already fire exactly once per
  underlying upload today and continue to.
