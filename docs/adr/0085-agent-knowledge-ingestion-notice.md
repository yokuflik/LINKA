# 0085 - Agent posts a knowledge-ingestion summary/failure notice to the owner

Status: Accepted

## Context

Uploading a knowledge-base document (ADR 0046 decision 4/ADR 0078) is
currently silent from the agent's own point of view: a successful
`POST /agents/me/knowledge` just returns the new `AgentKnowledgeDocumentOut`
to the frontend, and a PDF that yields no extractable text (scanned/
image-only) throws a local JS `Error` in `useKnowledgeUpload.js`, surfaced
only as a `knowledgeError` toast in the settings UI. Nothing ever reaches the
owner-agent chat, so the owner has no record, inside the conversation they
actually have with their agent, of what the agent's knowledge base currently
contains or why a given upload didn't take.

## Decision

Both outcomes now produce a real turn in the owner-agent chat
(`agent.owner_agent_chat_id`), authored by the agent itself (Gemini,
config-mode/Supervisor persona) rather than a canned string - consistent
with every other owner-facing notice this codebase already generates through
a real turn (e.g. the `on_schedule` free-text-instruction pattern, ADR 0046
decision 3) as opposed to the handful of fixed-string notices used for
hard exhaustion/timeout paths (ADR 0057 budget notice, `_run_turn`'s timeout
message) - because there is meaningful, variable content here (filename,
mime type, chunk count, or an explanation of *why* a PDF didn't parse) that
benefits from the agent phrasing it in its own voice.

**Success path** (`modules/agents/router.py::commit_knowledge_document`):
after `knowledge_service.commit_knowledge_document` returns and the row is
committed, the router enqueues a new `kind="knowledge"` entry onto
`agent_invoke_stream` via a new `invoke_queue.enqueue_knowledge_event`,
carrying `agent_id`, `chat_id=agent.owner_agent_chat_id`, and a free-text
`instruction` built server-side: filename, mime type, `len(chunk_list)`, and
**a real content excerpt** - the first `AGENT_KNOWLEDGE_NOTICE_PREVIEW_CHUNKS`
(default 5) chunks joined and capped to
`AGENT_KNOWLEDGE_NOTICE_PREVIEW_MAX_CHARS` (default 4000) characters. Without
this, the model would only ever be told a document *was added*, never what
it says, and so could not produce a real "here's what I learned" summary -
only a templated-sounding acknowledgment. `commit_knowledge_document`'s
return type changed from `AgentKnowledgeDocument` to `tuple[
AgentKnowledgeDocument, list[str]]` (the just-built `chunk_list`) so the
router has the content in hand without a second DB read. No dedicated schema
field is added for the instruction itself - the string is fully formed at
enqueue time, mirroring how `schedule_instruction` already works.

**Failure path (PDF only)**: `useKnowledgeUpload.js`'s `extractPdfChunks`
still runs first and still throws locally on zero extracted chunks - no
change to the client-side parse itself. What changes is what happens with
that failure: instead of setting `knowledgeError` for a toast, the frontend
calls a new endpoint, `POST /agents/me/knowledge/report-failure`
(`{filename, mime_type, reason}`, `reason` a fixed enum -
`"no_extractable_text"` is the only value for now), which enqueues the same
`kind="knowledge"` stream entry with a failure-flavored instruction. The
`knowledgeError` toast/ref stays in the composable's return shape but is no
longer set on this path (nothing else currently writes to it, so it is
effectively dead until/unless a future non-PDF failure case needs it).

**Turn seeding**: a new `_build_knowledge_contents(instruction)` helper
(`invoke_turn_helpers.py`, alongside `_build_schedule_contents`) wraps the
instruction in the same "decide how to act" framing, e.g.:

```
Knowledge base update: {instruction}

Tell the owner what happened, in your own words.
```

**Dispatch**: `invoke_worker.py::process_entry` gains a third `kind`
branch (alongside `"message"`/`"schedule"`) that calls `_run_turn` with a
new `knowledge_instruction` parameter and `chat_id=agent.owner_agent_chat_id`
- since that `chat_id` always equals `owner_agent_chat_id` by construction,
`is_config_mode` (`modules/agents/tools/dispatch.py`) already classifies it
as config-mode with zero new dispatch logic: `_run_turn` seeds from
`_build_knowledge_contents` instead of `_build_schedule_contents`, runs the
existing Supervisor persona/tool schema set, and its final text reaches the
owner via the existing `_post_config_reply` path - not a new delivery
mechanism, not a new persona.

Per-(agent_id, chat_id) turn mutex/debounce (ADR 0063) and the daily
active-time budget re-check (`process_entry`'s existing top-of-function
gate) apply unchanged, since `chat_id` here is a real, non-null value (the
owner-agent chat) like every other config-mode turn.

## What this deliberately does not do

- **No new persona/system prompt.** Reuses whichever `builder_state` the
  owner's agent is currently in (Supervisor by default) - if the owner is
  mid-Builder-interview when an upload happens, the notice is voiced by the
  Builder persona, not a special "notifications" persona. Considered adding
  a distinct notification persona and rejected it: it would need its own
  tool schema carve-out for a message that never calls a tool at all, and
  the existing personas already know how to just report a fact in plain
  text.
- **No retry/backoff/queueing beyond what `agent_invoke_stream` already
  provides.** A knowledge-notice turn is best-effort like every other
  enqueue onto this stream - if it's dropped or the worker is down, the
  document itself is still safely committed (success path) or simply never
  uploaded (failure path); only the *notice* is best-effort.
- **No extension to text/markdown failure cases.** Those uploads have no
  pre-flight content check today (confirmed out of scope for this ADR) -
  only the existing PDF zero-chunk failure gets a notice. A future ADR can
  extend `report-failure`'s `reason` enum if a text/markdown failure mode
  needs the same treatment.
- **No change to the `knowledgeError` ref's existence** - kept for future
  non-PDF-upload error cases (network failure on the ticket/PUT/commit
  calls, which are not knowledge-content problems and don't belong in the
  agent's own voice), just no longer populated by the PDF-parse-failure
  path specifically.
