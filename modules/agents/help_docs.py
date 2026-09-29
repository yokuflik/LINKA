"""Static reference material for the Help personas (ADR 0092).

Read once at import time from docs/agent_knowledge/ and inlined directly into
HELP_GENERAL_PROMPT/HELP_BUILDING_PROMPT (builder_flow.py) - not chunked,
embedded, or stored per agent_id. Unlike the per-owner knowledge base
(ADR 0046/0078), this content is identical for every agent, never touched by
POST /agents/me/reset, and updated by editing these files and restarting the
app - no seeding script, no re-embedding.
"""
from pathlib import Path

_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs" / "agent_knowledge"

GENERAL_HELP_DOC = (_DOCS_DIR / "linka_general_help.md").read_text(encoding="utf-8")
AGENT_BUILDING_HELP_DOC = (_DOCS_DIR / "linka_agent_building_help.md").read_text(encoding="utf-8")
