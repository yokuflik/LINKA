"""
Tool registry + execute_tool_call (ADR 0045 step 4, split into this package
by ADR 0056). This facade re-exports the public API so invoke_worker.py and
anything else keeps importing `from modules.agents.tools import ...`
unchanged - no behaviour change.

Modules:
- common          - ToolDeniedError, identity masking, quota check, call logging
- execution       - the 11 execution-mode tool handlers + EXECUTION_TOOL_HANDLERS
- config_mode     - the 8 config-mode tool handlers + CONFIG_TOOL_HANDLERS
- builder_handoff - finish_building_agent + BUILDER_STATE_HANDLERS (ADR 0049,
                     transfer_to_* handoff tools removed by ADR 0093 Phase 3)
- schemas         - TOOL_SCHEMAS, CONFIG_TOOL_SCHEMAS, BUILDER_STATE_TOOL_SCHEMAS
- dispatch        - is_config_mode, get_tool_schemas_for_chat, execute_tool_call
                     (the hard tool-mode gate, ADR 0047 decision 4)
"""
from modules.agents.tools.common import ToolDeniedError
from modules.agents.tools.dispatch import (
    execute_tool_call,
    get_tool_schemas_for_chat,
    is_config_mode,
)
from modules.agents.tools.schemas import CONFIG_TOOL_SCHEMAS, TOOL_SCHEMAS

__all__ = [
    "ToolDeniedError",
    "TOOL_SCHEMAS",
    "CONFIG_TOOL_SCHEMAS",
    "execute_tool_call",
    "get_tool_schemas_for_chat",
    "is_config_mode",
]
