"""Agent config API models (ADR 0045, frontend step 5)."""

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from api.schemas import IdStr


class AgentRestrictionsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    can_send_messages: bool
    can_message_groups: bool
    can_message_private: bool
    can_message_new_private_contacts: bool
    can_leave_groups: bool
    blocked_read_chat_ids: List[IdStr]
    max_messages_per_day: Optional[int]


class AgentRestrictionsIn(BaseModel):
    """Partial patch - only fields present are changed (PATCH semantics)."""

    can_send_messages: Optional[bool] = None
    can_message_groups: Optional[bool] = None
    can_message_private: Optional[bool] = None
    can_message_new_private_contacts: Optional[bool] = None
    can_leave_groups: Optional[bool] = None
    blocked_read_chat_ids: Optional[List[IdStr]] = None
    max_messages_per_day: Optional[int] = None


class AgentTimeWindowOut(BaseModel):
    enabled: bool
    start: str
    end: str


class AgentTimeWindowIn(BaseModel):
    enabled: bool
    start: str
    end: str


class AgentSpecificChatOut(BaseModel):
    keywords: List[str]


class AgentSpecificChatIn(BaseModel):
    keywords: List[str] = []


class AgentUnknownSenderOut(BaseModel):
    enabled: bool


class AgentUnknownSenderIn(BaseModel):
    enabled: bool


class AgentAnyMessageOut(BaseModel):
    enabled: bool


class AgentAnyMessageIn(BaseModel):
    enabled: bool


class AgentScheduleEntryOut(BaseModel):
    id: str
    kind: str  # "recurring" | "once"
    time: Optional[str] = None  # recurring: daily HH:MM UTC
    at: Optional[str] = None  # once: absolute UTC instant, ISO-8601
    instruction: str
    chat_id: Optional[IdStr] = None
    enabled: bool


class AgentScheduleEntryIn(BaseModel):
    id: str
    kind: str
    time: Optional[str] = None
    at: Optional[str] = None
    instruction: str
    chat_id: Optional[IdStr] = None
    enabled: bool = True


class AgentTriggersOut(BaseModel):
    on_time_window: AgentTimeWindowOut
    on_specific_chats: Dict[str, AgentSpecificChatOut]
    on_unknown_sender: AgentUnknownSenderOut
    on_any_message: AgentAnyMessageOut
    on_schedule: List[AgentScheduleEntryOut]


class AgentTriggersIn(BaseModel):
    """Partial patch - only fields present are changed (PATCH semantics).
    on_schedule, when present, replaces the whole list (same shallow-merge
    contract as the other trigger keys - the client sends the full list back
    after adding/removing/editing one entry)."""

    on_time_window: Optional[AgentTimeWindowIn] = None
    on_specific_chats: Optional[Dict[str, AgentSpecificChatIn]] = None
    on_unknown_sender: Optional[AgentUnknownSenderIn] = None
    on_any_message: Optional[AgentAnyMessageIn] = None
    on_schedule: Optional[List[AgentScheduleEntryIn]] = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    owner_agent_chat_id: IdStr
    system_prompt: str
    restrictions: AgentRestrictionsOut
    triggers: AgentTriggersOut
    is_enabled: bool
    # BYOK (ADR 0046 decision 5): never echoes the key itself, only whether
    # one is set. Computed from encrypted_gemini_api_key in the router.
    has_custom_key: bool = False
    # Skill/persona in force for execution-mode turns (ADR 0047 decision 3).
    # Read-only here - writing it goes through the set_agent_persona config
    # tool (ADR 0047 decision 6) or a future config-UI picker, not PATCH.
    active_skill: str
    # Chats the agent paused itself in via pause_and_escalate (ADR 0047
    # decision 5) - human-only resume via POST /agents/me/resume-chat/{id}.
    paused_chat_ids: List[IdStr] = []
    # Sub-state inside the config chat (ADR 0049): "supervisor" |
    # "builder_agent" | "help_agent". Read-only here - only the agent's own
    # transfer_to_builder/transfer_to_help/finish_building_agent tools change
    # it, never PATCH /agents/me.
    builder_state: str


class AgentKnowledgeUploadTicketIn(BaseModel):
    mime_type: str
    size_bytes: int


class AgentKnowledgeUploadTicketOut(BaseModel):
    storage_key: str
    upload_url: str
    required_headers: dict
    expires_in: int


class AgentKnowledgeCommitIn(BaseModel):
    filename: str
    storage_key: str
    mime_type: str
    # Required for application/pdf (client-side parsed/chunked, ADR 0046
    # decision 4); must be omitted for text/plain and text/markdown, which
    # the server fetches and chunks itself.
    chunks: Optional[List[str]] = None


class AgentKnowledgeDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    filename: str
    mime_type: str
    status: str


class AgentConfigPatchIn(BaseModel):
    """PATCH /agents/me body - every field optional, only sent keys change."""

    system_prompt: Optional[str] = None
    is_enabled: Optional[bool] = None
    restrictions: Optional[AgentRestrictionsIn] = None
    triggers: Optional[AgentTriggersIn] = None
    # BYOK (ADR 0046 decision 5): write-only, never echoed back. Absent =
    # leave the stored key untouched; "" or null clears it (falls back to
    # the shared settings.GEMINI_API_KEY); a non-empty string replaces it.
    gemini_api_key: Optional[str] = None
