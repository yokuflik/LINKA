# ADR 0056 — Split `modules/agents/tools.py` into a `tools/` domain package

Status: Accepted
Date: 2026-09-25

## Context

`modules/agents/tools.py` had grown to ~980 lines spanning several unrelated
concerns: shared helpers (identity masking, quota check, call logging),
execution-mode tool handlers, config-mode tool handlers, builder sub-state
handoff tools, the hard tool-mode gate + dispatch (`execute_tool_call`), and
two large blocks of Gemini function-declaration schemas.

CLAUDE.md Rule 9 flags files over ~300 lines. Rule 7 requires an ADR before a
significant structural change. This is the same shape of problem
`chat_service.py` had before ADR 0013 split it into `modules/chats/`.

## Decision

Split by responsibility into a new `modules/agents/tools/` package, with
`__init__.py` as a thin facade re-exporting the public API. The single real
importer (`modules/agents/invoke_worker.py`, `from modules.agents.tools
import execute_tool_call, get_tool_schemas_for_chat, is_config_mode`) needs
no change - `modules.agents.tools` still resolves the same names.

### Modules

| Module | Public surface |
|---|---|
| `common.py` | `ToolDeniedError`, `_log_call`, `_new_client_message_id`, `_resolve_sender_labels`, `_check_daily_send_quota`, `_chat_is_group`, `_describe_escalation_counterpart` |
| `execution.py` | The 10 execution-mode tool handlers (`_tool_send_message` … `_tool_pause_and_escalate`) + `_EXECUTION_TOOL_HANDLERS` + `_CHAT_SCOPED_TOOL_NAMES` |
| `config_mode.py` | The 8 config-mode tool handlers (`_tool_set_agent_persona` … `_tool_resume_paused_chat`) + `_CONFIG_TOOL_HANDLERS` |
| `builder_handoff.py` | ADR 0049 handoff tools (`_tool_transfer_to_builder`, `_tool_transfer_to_help`, `_tool_finish_building_agent`) + `_BUILDER_STATE_HANDLERS` |
| `schemas.py` | `TOOL_SCHEMAS`, `CONFIG_TOOL_SCHEMAS`, `_BUILDER_STATE_TOOL_SCHEMAS` |
| `dispatch.py` | `is_config_mode`, `get_tool_schemas_for_chat`, `execute_tool_call` (the hard tool-mode gate, ADR 0047 decision 4) |
| `__init__.py` | Facade - re-exports everything the modules above and outside importers need |

No behaviour change, no schema/DB change. Pure `git mv` + import rewrite,
same pattern as ADR 0013/0022.
