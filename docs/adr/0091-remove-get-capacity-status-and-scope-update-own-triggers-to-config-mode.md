# 0091 - Remove `get_capacity_status`; scope `update_own_triggers` to config-mode only

Status: Accepted

## Context

Two unrelated cleanups to the agent tool registry, both raised by the user
directly:

1. **`get_capacity_status`** (ADR 0057) was found to be dead: defined in
   `CONFIG_TOOL_SCHEMAS`/`CONFIG_TOOL_HANDLERS`
   (`modules/agents/tools/config_mode.py`,
   `modules/agents/tools/schemas.py`), explicitly filtered *out* of the
   Builder's own schema list (`_BUILDER_TOOL_SCHEMAS`), and never added to
   Supervisor's à-la-carte schema list either
   (`BUILDER_STATE_TOOL_SCHEMAS[BuilderState.SUPERVISOR]`). No
   `builder_state` actually exposed it to the model - confirmed by grepping
   every `BUILDER_STATE_TOOL_SCHEMAS`/`BUILDER_STATE_HANDLERS` entry. The
   user confirmed the tool is old and unwanted; not a candidate for
   re-wiring in.

2. **`update_own_triggers`** (ADR 0045, execution-mode) let any of the four
   execution personas (`sales_agent`/`support_agent`/`summarizer`/
   `one_off_executor`) - which run inside chats with real third parties, not
   the owner - rewrite the agent's own wake-up trigger configuration
   (`Agent.triggers`) mid-turn. Because the triggering chat text is
   attacker-controlled (a customer, an unknown sender), this was a
   prompt-injection surface: a crafted message could attempt to talk the
   agent into loosening its own future wake conditions (e.g. adding itself
   to `on_specific_chats`, widening `on_time_window`) with no owner
   involvement. Flagged during a tool-registry review; the user chose to
   remove it from execution-mode rather than accept the risk.

## Decision

- **Delete `get_capacity_status` entirely**: the handler
  (`_tool_get_capacity_status`, `modules/agents/tools/config_mode.py`), its
  schema (`schemas.py`), its `CONFIG_TOOL_HANDLERS`/`CONFIG_TOOL_SCHEMAS`
  entries, and the now-dead `get_capacity_status`-filtering comment/list
  comprehension building `_BUILDER_TOOL_SCHEMAS` (simplified to
  `_BUILDER_TOOL_SCHEMAS = CONFIG_TOOL_SCHEMAS`, since the filter was a
  no-op once the tool doesn't exist). Also removes the now-unused
  `peek_fixed_window`/`peek_sliding_window`/`count_knowledge_chunks`/
  `count_knowledge_documents` imports from `config_mode.py` (no other
  caller in that file). `infra/ratelimit/service.py::peek_fixed_window`
  itself is left in place (generic infra, not agent-specific dead code).
- **Scope `update_own_triggers` to config-mode only**: removed from
  `TOOL_SCHEMAS`/`EXECUTION_TOOL_HANDLERS`
  (`modules/agents/tools/execution.py`, `schemas.py`) - no longer reachable
  from any of the four execution personas. The schema is split into a
  standalone `_UPDATE_OWN_TRIGGERS_SCHEMA` constant and added explicitly to
  `BUILDER_STATE_TOOL_SCHEMAS[SUPERVISOR]` and `[BUILDER]`; the handler
  (`_tool_update_own_triggers`, unchanged body, still in `execution.py`) is
  imported into `builder_handoff.py` and added explicitly to
  `BUILDER_STATE_HANDLERS[SUPERVISOR]` and `[BUILDER]`. Both Help states
  never had it. The tool's behavior/handler body is unchanged - only which
  contexts can reach it. This mirrors how `resolve_user`/
  `spawn_ephemeral_task`/`save_knowledge_from_text` are already
  config-mode-only tools added explicitly to Supervisor/Builder rather than
  living in the shared execution toolset.

## Consequences

- Supervisor and Builder (talking only to the agent's own owner, per ADR
  0062's toolset union) can still adjust the agent's own triggers
  on request - this is the only context it was actually used from in
  practice (an owner asking their agent to change its own wake-up rules).
- No execution persona can be steered by a third party into modifying its
  own trigger config anymore - closes that prompt-injection surface.
- `get_capacity_status` is gone with no replacement; a future "show me my
  agent's capacity" feature would need to be rebuilt (not re-enabled) and
  would need to actually be wired into a builder_state's schema list this
  time, unlike before.
- Test fallout: `tests/modules/agents/tools/test_dispatch.py` and
  `tests/modules/agents/tools/test_builder_flow.py` updated to match -
  `get_capacity_status` dropped from parametrized/expected-tool-set lists;
  `update_own_triggers` added to Supervisor/Builder's expected handler sets
  and removed from `test_builder_flow.py`'s `execution_only_tools` check
  (which also had a pre-existing bug: `save_knowledge_from_text` was
  mislabeled there as execution-only when it is config-mode-only - fixed
  in the same pass). 281 tests in `tests/modules/agents/` pass.
