"""Gemini function-declaration schemas (raw REST shape - no SDK, ADR 0045).

Split from tools.py (ADR 0056). Two disjoint schema sets - execution-mode
TOOL_SCHEMAS and config-mode CONFIG_TOOL_SCHEMAS - plus the ADR 0049 builder
sub-state schema sets, which are assembled here since they reuse
CONFIG_TOOL_SCHEMAS entries and builder_flow's handoff schemas.
"""
from modules.agents.builder_flow import (
    FINISH_BUILDING_AGENT_SCHEMA,
    TRANSFER_TO_BUILDER_SCHEMA,
    TRANSFER_TO_HELP_SCHEMA,
    BuilderState,
)

TOOL_SCHEMAS = [
    {
        "name": "send_message",
        "description": "Send a new text message into an existing chat the owner already participates in.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Target chat id"},
                "content": {"type": "string", "description": "Message text"},
            },
            "required": ["chat_id", "content"],
        },
    },
    {
        "name": "reply_message",
        "description": "Send a text message that replies to a specific earlier message in a chat.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "reply_to_message_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["chat_id", "reply_to_message_id", "content"],
        },
    },
    {
        "name": "create_chat",
        "description": "Open a brand-new 1:1 chat with a user the owner has not messaged before (or fetch the existing one).",
        "parameters": {
            "type": "object",
            "properties": {"target_user_id": {"type": "string"}},
            "required": ["target_user_id"],
        },
    },
    {
        "name": "leave_group",
        "description": "Leave a group chat on the owner's behalf. If the owner is the group's owner and other members remain, new_owner_id must name a successor.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "new_owner_id": {"type": "string", "description": "Required only if the owner is this group's owner and other members remain"},
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "read_history",
        "description": "Read the last 20 messages of a chat, oldest first, each with sender_id/timestamp/content.",
        "parameters": {
            "type": "object",
            "properties": {"chat_id": {"type": "string"}},
            "required": ["chat_id"],
        },
    },
    {
        "name": "update_own_triggers",
        "description": "Modify this agent's own wake-up trigger configuration (time window / per-chat keywords). Cannot touch restrictions.",
        "parameters": {
            "type": "object",
            "properties": {
                "triggers": {
                    "type": "object",
                    "description": "Partial or full triggers object: {on_time_window: {enabled, start, end}, on_specific_chats: {chat_id: {keywords: [...]}}, on_any_message: {enabled}}",
                }
            },
            "required": ["triggers"],
        },
    },
    {
        "name": "search_messages",
        "description": "Keyword-search the owner's own messages (optionally scoped to one chat) - e.g. to check who has been waiting for a reply.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "chat_id": {"type": "string", "description": "Optional: restrict the search to this chat"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_knowledge_index",
        "description": "List every chunk of this agent's own uploaded knowledge-base documents as {document_id, filename, chunk_id, excerpt}. Browse this first, then call fetch_chunk on the chunk_id(s) that look relevant.",
    },
    {
        "name": "fetch_chunk",
        "description": "Fetch the full text of one knowledge-base chunk by chunk_id (from get_knowledge_index).",
        "parameters": {
            "type": "object",
            "properties": {"chunk_id": {"type": "string"}},
            "required": ["chunk_id"],
        },
    },
    {
        "name": "pause_and_escalate",
        "description": "Freeze yourself for this specific chat and notify the human owner that you need their input. Use this when you're stuck, unsure, or asked to do something outside your restrictions - and ALWAYS when the other person explicitly asks to speak with a human/real person/representative/the owner, or is ready to close a deal and needs a human to finalize it. You will not be woken again in this chat until the owner resumes it. Before or immediately after calling this, also tell the other person in the chat (via send_message/reply_message) that you're connecting them with a real person now, in their own language.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "The actual notification message for the owner, written naturally in the same language you've been using with the owner (not a template or a short label) - explain what happened and why you're handing off, the way you'd tell them in a normal message."}},
        },
    },
]

# Config-mode tool schemas (ADR 0047 decision 6) - reachable only when
# is_config_mode(agent, chat_id) is True (the hard gate, dispatch.py).
CONFIG_TOOL_SCHEMAS = [
    {
        "name": "set_agent_persona",
        "description": "Set which skill/persona the agent runs as in execution-mode chats (sales_agent, support_agent, summarizer, or one_off_executor).",
        "parameters": {
            "type": "object",
            "properties": {"skill": {"type": "string", "description": "One of: sales_agent, support_agent, summarizer, one_off_executor"}},
            "required": ["skill"],
        },
    },
    {
        "name": "update_agent_rules",
        "description": "Replace the agent's free-text soft rules (system prompt), layered on top of its persona's behavioral template.",
        "parameters": {
            "type": "object",
            "properties": {"rules": {"type": "string"}},
            "required": ["rules"],
        },
    },
    {
        "name": "set_trigger",
        "description": "Modify the agent's wake-up trigger configuration (time window / per-chat keywords / unknown-sender / any-message / schedule entries).",
        "parameters": {
            "type": "object",
            "properties": {
                "triggers": {
                    "type": "object",
                    "description": "Partial triggers object: {on_time_window: {...}, on_specific_chats: {...}, on_unknown_sender: {...}, on_any_message: {enabled}, on_schedule: [...]}",
                }
            },
            "required": ["triggers"],
        },
    },
    {
        "name": "get_agent_status",
        "description": "Report the agent's current configuration: enabled state, active skill, restrictions, triggers, and any chats currently paused awaiting the owner's input.",
    },
    {
        "name": "estimate_api_usage",
        "description": "Give the owner a rough, informational cost estimate for a described action (e.g. sending messages, a daily schedule, knowledge lookups). Does not affect any real rate limit.",
        "parameters": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    },
    {
        "name": "schedule_one_off_task",
        "description": "Schedule a single one-time future task (reuses the same mechanism as a recurring schedule entry, kind=once).",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Free-text instruction to execute at the scheduled time"},
                "execute_at": {"type": "string", "description": "ISO-8601 UTC instant"},
                "chat_id": {"type": "string", "description": "Optional: chat to join history from when the task fires"},
            },
            "required": ["task", "execute_at"],
        },
    },
    {
        "name": "resolve_user",
        "description": "Look up a real person by their exact phone_number or username (never a display name/nickname) and return their verified chat_id. You MUST call this and get found=true before setting a trigger or scheduling anything targeted at a specific person the owner named by phone number or username - never assume the person exists or fabricate a chat_id. Provide exactly one of phone_number or username.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Exact phone number, if that's what the owner gave"},
                "username": {"type": "string", "description": "Exact username (not a display name/nickname), if that's what the owner gave"},
            },
        },
    },
    {
        "name": "resume_paused_chat",
        "description": "Un-pause the agent for one specific chat, identified by that person's exact phone_number or username, so it starts responding there again. Use this when the owner asks to bring the agent back for a specific customer/chat it had paused/escalated (e.g. after pause_and_escalate). Provide exactly one of phone_number or username - never guess a chat_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Exact phone number of the person whose chat should be resumed"},
                "username": {"type": "string", "description": "Exact username of the person whose chat should be resumed"},
            },
        },
    },
]

# --- Builder sub-state schema sets (ADR 0049) --------------------------------
_RESUME_PAUSED_CHAT_SCHEMA = next(s for s in CONFIG_TOOL_SCHEMAS if s["name"] == "resume_paused_chat")

BUILDER_STATE_TOOL_SCHEMAS = {
    BuilderState.SUPERVISOR: [TRANSFER_TO_BUILDER_SCHEMA, TRANSFER_TO_HELP_SCHEMA, _RESUME_PAUSED_CHAT_SCHEMA],
    BuilderState.BUILDER: [*CONFIG_TOOL_SCHEMAS, TRANSFER_TO_HELP_SCHEMA, FINISH_BUILDING_AGENT_SCHEMA],
    BuilderState.HELP: [TRANSFER_TO_BUILDER_SCHEMA],
}
