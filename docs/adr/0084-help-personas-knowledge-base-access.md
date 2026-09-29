# 0084 - Help personas get read-only knowledge-base access

Status: Accepted

## Context

ADR 0064 gave the two Help personas (`HELP_GENERAL`, `HELP_BUILDING`,
`modules/agents/builder_flow.py`) a deliberate zero-action posture: only
transfer tools, nothing that touches a real chat or saves config. Their
system prompts explicitly forbid inventing an answer - they must say "I
don't know" for anything not covered by the prompt text itself. In practice
that meant their factual knowledge was capped at whatever was hand-written
into `HELP_GENERAL_PROMPT`/`HELP_BUILDING_PROMPT`, with no way to draw on
longer reference material without bloating every single turn's system
prompt.

Separately, an owner can already seed their own agent's knowledge base
(`agent_knowledge_documents`/`agent_knowledge_chunks`, ADR 0046/0078) with
arbitrary reference text via `save_knowledge_from_text` or a file upload -
this is agent-scoped storage, hard-filtered by `agent_id` in every query, so
one agent's knowledge base is never visible to another agent. This mechanism
is the natural fit for giving the Help personas real content to answer from,
without reversing ADR 0064's ban on anything that *acts* on a real chat or
*writes* config.

## Decision

Both Help states gain three **read-only** knowledge-base tools, reusing the
existing execution-mode handlers verbatim (no new tool logic):
`search_knowledge_semantic`, `get_knowledge_index`, `fetch_chunk`
(`modules/agents/tools/execution.py`). Wired in `BUILDER_STATE_HANDLERS`
(`modules/agents/tools/builder_handoff.py`) and
`BUILDER_STATE_TOOL_SCHEMAS` (`modules/agents/tools/schemas.py`) for
`HELP_BUILDING`/`HELP_GENERAL` only - `SUPERVISOR`/`BUILDER` already had
these via the ADR 0062 execution-toolset union.

`HELP_GENERAL_PROMPT`/`HELP_BUILDING_PROMPT` now instruct the model to call
`search_knowledge_semantic` (falling back to `get_knowledge_index` +
`fetch_chunk`) before answering, and to answer only from what that lookup
returns - never reading saved content back verbatim, never mentioning that a
lookup happened. This lets an owner seed their own agent's knowledge base
with real product documentation (general Linka usage, or agent-building
specifics) and have their Help personas answer from it, entirely through the
existing per-agent knowledge base - no shared/global knowledge store, no
prompt changes needed per owner.

## What this deliberately does not do

- **No write access.** `save_knowledge_from_text` stays off both Help
  states' tool sets - they can read the knowledge base, never add to it.
- **No execution/config tools.** Everything else about ADR 0064's
  zero-action posture is unchanged: no `send_message`, no
  `update_agent_rules`, no chat-touching tool of any kind reachable from
  either Help state.
- **No cross-agent knowledge sharing.** Every knowledge-base query stays
  hard-scoped to `agent.id`, same as everywhere else it's used - one owner's
  seeded documentation is never visible to another owner's agent.
