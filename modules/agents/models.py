import json

from sqlalchemy import Column, BigInteger, Boolean, LargeBinary, String, Text, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.sql import func

from infra.db.base import Base

# Default shape of Agent.restrictions - a denylist over the messaging domain
# (no account-management tool exists at all, so there is nothing to deny
# there). Enforcement happens server-side in execute_tool_call, never via
# the system prompt alone - see docs/adr/0045.
DEFAULT_AGENT_RESTRICTIONS = {
    "can_send_messages": True,
    # New agents default to private-only sending; reading is unrestricted
    # (blocked_read_chat_ids empty). Changed 2026-09-23 per explicit user
    # go-ahead (CLAUDE.md Rule 10 - security-relevant default). New agents
    # only: no migration/backfill, existing rows keep whatever value they
    # already have (no-migrations convention, ADR-style additive default).
    "can_message_groups": False,
    "can_message_private": True,
    "can_message_new_private_contacts": True,
    "can_leave_groups": True,
    "blocked_read_chat_ids": [],
    "max_messages_per_day": None,
}

# Default shape of Agent.triggers - the Gatekeeper config (ADR 0045).
# on_specific_chats maps chat_id (as a JSON string key) -> {"keywords": [...]}.
# Empty keywords = wake on any message in that chat; non-empty = substring
# (case-insensitive) match required.
# on_unknown_sender (ADR 0046 decision 2): fires on the first-ever message in
# a private (non-group) chat, independent of on_specific_chats/on_time_window.
# on_schedule (ADR 0046 decision 3): a list of recurring/one-off entries that
# run a full agent turn at a given time (not a canned message - see
# modules/agents/schedule.py). Capped at AGENT_MAX_SCHEDULE_ENTRIES.
DEFAULT_AGENT_TRIGGERS = {
    "on_time_window": {"enabled": False, "start": "09:00", "end": "22:00"},
    "on_specific_chats": {},
    "on_unknown_sender": {"enabled": False},
    "on_schedule": [],
}

# Skills/personas catalog (ADR 0047 decision 3) - fixed, code-defined, no
# user-authored personas in v1. Full prompts live in modules/agents/personas.py
# (PERSONA_SYSTEM_PROMPTS), not here, to keep this module schema-only.
# "agent_builder" is never stored as active_skill - it's implicitly in force
# whenever the triggering chat is owner_agent_chat_id (ADR 0047 decision 4),
# regardless of what active_skill is set to.
DEFAULT_AGENT_ACTIVE_SKILL = "one_off_executor"

# Sub-state inside the config chat (ADR 0049) - dynamic runtime state written
# by the agent's own transfer_to_builder/transfer_to_help/finish_building_agent
# tools, meaningful only when chat_id == owner_agent_chat_id. See
# modules/agents/builder_flow.py for the full state machine.
DEFAULT_AGENT_BUILDER_STATE = "supervisor"


class Agent(Base):
    """
    One autonomous AI agent per Linka user (service account, Gemini tool
    calling - ADR 0045). Acts as the owner's user_id when performing tool
    calls; gated by owner-configured wake triggers and hard, server-enforced
    restrictions.
    """

    __tablename__ = "agents"

    id = Column(BigInteger, primary_key=True, index=True)

    # One agent per user.
    owner_user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Permanent 1:1 chat between the owner and this agent, auto-created at
    # Agent creation time (not lazily) - used for system notifications
    # (e.g. daily time-budget exhaustion) and direct owner<->agent chat.
    owner_agent_chat_id = Column(
        BigInteger,
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Soft constraint only - injected as the Gemini system_instruction.
    # Explicitly NOT a security boundary; restrictions below are.
    system_prompt = Column(Text, nullable=False, server_default=text("''"))

    # Hard, server-enforced denylist. See DEFAULT_AGENT_RESTRICTIONS.
    restrictions = Column(
        JSONB,
        nullable=False,
        server_default=text(f"'{json.dumps(DEFAULT_AGENT_RESTRICTIONS)}'::jsonb"),
    )

    # Wake-up ("Gatekeeper") configuration. See DEFAULT_AGENT_TRIGGERS.
    triggers = Column(
        JSONB,
        nullable=False,
        server_default=text(f"'{json.dumps(DEFAULT_AGENT_TRIGGERS)}'::jsonb"),
    )

    # Global kill switch, checked at trigger-evaluation time AND again at
    # worker-dequeue time (defense in depth for the enqueue-to-dequeue gap).
    # Default flipped True -> False in ADR 0047 decision 2: every agent
    # starts fully dormant until the owner explicitly turns it on - no
    # trigger, including the config-chat ones, fires while disabled. New
    # rows only, no migration/backfill (no-migrations convention).
    is_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # BYOK (ADR 0046 decision 5): owner's own Gemini API key, Fernet-
    # encrypted at rest (modules/agents/crypto.py). NULL means "use the
    # shared settings.GEMINI_API_KEY" - never echoed back by the API, see
    # AgentOut.has_custom_key.
    encrypted_gemini_api_key = Column(LargeBinary, nullable=True)

    # Skill/persona in force for execution-mode turns (ADR 0047 decision 3) -
    # one of PERSONA_SYSTEM_PROMPTS' keys (modules/agents/personas.py). Fixed
    # config on the row, set only via the set_agent_persona config tool or
    # the config UI - never selected per-turn by the model itself.
    active_skill = Column(
        String(32),
        nullable=False,
        default=DEFAULT_AGENT_ACTIVE_SKILL,
        server_default=text(f"'{DEFAULT_AGENT_ACTIVE_SKILL}'"),
    )

    # Dynamic runtime state written by the agent itself via pause_and_escalate
    # (ADR 0047 decision 5) - NOT part of restrictions (owner-authored hard
    # constraints) or triggers (wake conditions). A chat_id in this list is
    # skipped entirely at trigger-evaluation time until a human clears it via
    # POST /agents/me/resume-chat/{chat_id} - un-pausing is deliberately not a
    # tool the agent can call on itself.
    paused_chat_ids = Column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )

    # Sub-state inside the config chat (ADR 0049) - one of "supervisor" |
    # "builder_agent" | "help_agent". Only meaningful when the triggering
    # chat_id is owner_agent_chat_id; execution-mode turns never read/write
    # this. Written only by the agent's own handoff/finish tools, never by
    # PATCH /agents/me (see AgentOut.builder_state - read-only).
    builder_state = Column(
        String(32),
        nullable=False,
        default=DEFAULT_AGENT_BUILDER_STATE,
        server_default=text(f"'{DEFAULT_AGENT_BUILDER_STATE}'"),
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AgentKnowledgeDocument(Base):
    """
    One uploaded knowledge-base source file (ADR 0046 decision 4). Text/
    Markdown is chunked server-side after the client uploads the raw file to
    S3; PDFs are parsed+chunked entirely client-side (pdf.js) and only the
    resulting chunk array is POSTed - the app host never runs a PDF parser.
    """

    __tablename__ = "agent_knowledge_documents"

    id = Column(BigInteger, primary_key=True, index=True)

    agent_id = Column(
        BigInteger,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    filename = Column(String(255), nullable=False)
    s3_key = Column(Text, nullable=False)
    mime_type = Column(String(128), nullable=False)

    # "processing" | "ready" | "failed"
    status = Column(String(16), nullable=False, server_default=text("'processing'"))

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AgentKnowledgeChunk(Base):
    """
    One chunk of a knowledge document's text, searchable via Postgres FTS
    (no pgvector on this table - ADR 0042's IVFFlat stays scoped to message
    search; a per-agent knowledge corpus is expected to be small enough that
    keyword FTS is sufficient and cheaper).
    """

    __tablename__ = "agent_knowledge_chunks"

    id = Column(BigInteger, primary_key=True, index=True)

    # Denormalized from document_id so the search index can filter on it
    # directly (mirrors ADR 0040's chat_id on messages).
    agent_id = Column(
        BigInteger,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id = Column(
        BigInteger,
        ForeignKey("agent_knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    chunk_index = Column(BigInteger, nullable=False)
    content = Column(Text, nullable=False)

    # Maintained by a DB trigger (modules/agents/knowledge_ddl.py), same
    # mechanism as messages.content_tsv (ADR 0040) - not a SQLAlchemy
    # Computed/TSVectorType so a plain column here matches the ALTER-based
    # deployed-DB safety net in scripts/init_db.py.
    content_tsv = Column(TSVECTOR, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AgentToolCallLog(Base):
    """
    Audit log of every tool call an agent attempted, allowed or not.
    Unpartitioned to start; promote to a time-partitioned table later if
    volume warrants it (same pattern as ADR 0005).
    """

    __tablename__ = "agent_tool_call_log"

    id = Column(BigInteger, primary_key=True, index=True)

    agent_id = Column(
        BigInteger,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    tool_name = Column(String(64), nullable=False)

    arguments = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    allowed = Column(Boolean, nullable=False)

    # Populated only when allowed=False - which restriction blocked it.
    denial_reason = Column(String(128), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
