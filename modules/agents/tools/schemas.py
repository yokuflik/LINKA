"""Gemini function-declaration schemas (raw REST shape - no SDK, ADR 0045).

Split from tools.py (ADR 0056). Two disjoint schema sets - execution-mode
TOOL_SCHEMAS and config-mode CONFIG_TOOL_SCHEMAS - plus the ADR 0049 builder
sub-state schema sets, which are assembled here since they reuse
CONFIG_TOOL_SCHEMAS entries and builder_flow's handoff schemas.
"""
from modules.agents.builder_flow import (
    FINISH_BUILDING_AGENT_SCHEMA,
    TRANSFER_TO_BUILDER_SCHEMA,
    TRANSFER_TO_HELP_BUILDING_SCHEMA,
    TRANSFER_TO_HELP_GENERAL_SCHEMA,
    TRANSFER_TO_SUPERVISOR_SCHEMA,
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
        "description": (
            "Read up to 20 messages of a chat at a time (up to 50 if you pass a higher limit), "
            "oldest first, each with sender_id/timestamp/content. The result includes has_more: "
            "if true, this is only part of the history - call again with before_id set to "
            "next_before_id to go further back in parts. Never claim you've seen the whole "
            "conversation when has_more is true."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "before_id": {
                    "type": "string",
                    "description": "Optional: pass the previous result's next_before_id to fetch the next older page",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional: how many messages to return in this call (default 20, max 50)",
                },
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "count_messages_in_range",
        "description": (
            "Cheap, free count of how many messages exist in a chat (optionally within a date "
            "range) - no message content returned. Always call this BEFORE bulk_fetch_messages "
            "for the same chat/range, never call bulk_fetch_messages first. If the result's "
            "too_large is true, do not attempt bulk_fetch_messages at all - instead tell the "
            "owner the chat is too large (mention the count) and ask them to narrow the range "
            "by date or by picking a smaller window. If too_large is false, needs_confirmation "
            "is true: do NOT call bulk_fetch_messages yet - first ask the owner to confirm, "
            "explaining this pulls in the whole range (mention the count) and is an expensive "
            "operation. Only call bulk_fetch_messages in a LATER turn, after the owner has "
            "actually confirmed, and only with the exact same chat_id/date range you just checked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "start_date": {
                    "type": "string",
                    "description": (
                        "Optional: only messages on/after this date/time. Format YYYY-MM-DD "
                        "(midnight assumed) or YYYY-MM-DDTHH:MM:SS for a specific time"
                    ),
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "Optional: only messages on/before this date/time. Format YYYY-MM-DD "
                        "(end of day assumed) or YYYY-MM-DDTHH:MM:SS for a specific time"
                    ),
                },
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "bulk_fetch_messages",
        "description": (
            "Fetch up to 1000 messages of a chat in one call (oldest first, or fewer if you pass "
            "a lower limit), for summarizing a whole chat or a whole date range at once - unlike "
            "read_history, this is NOT paginated and returns everything in range in a single "
            "result. You MUST have already called count_messages_in_range for this exact "
            "chat_id/date range in an earlier turn AND gotten the owner's explicit "
            "confirmation first - calling this without that will be denied. Never call this on "
            "your own initiative right after count_messages_in_range in the same turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "start_date": {
                    "type": "string",
                    "description": (
                        "Optional: only messages on/after this date/time. Must match what was "
                        "passed to count_messages_in_range. Format YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
                    ),
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "Optional: only messages on/before this date/time. Must match what was "
                        "passed to count_messages_in_range. Format YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional: cap the number of messages fetched (default/max 1000)",
                },
            },
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
        "description": (
            "Keyword-search the owner's own messages, optionally scoped to one chat and/or a "
            "date range. Returns up to 10 matches at a time (up to 50 if you pass a higher "
            "limit). The result includes has_more: if true, call again with cursor set to "
            "next_cursor to get more matches. Never claim you've found everything when has_more "
            "is true."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "chat_id": {"type": "string", "description": "Optional: restrict the search to this chat"},
                "cursor": {
                    "type": "string",
                    "description": "Optional: pass the previous result's next_cursor to fetch the next page",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional: how many matches to return in this call (default 10, max 50)",
                },
                "start_date": {
                    "type": "string",
                    "description": (
                        "Optional: only messages on/after this date/time. Format YYYY-MM-DD "
                        "(midnight assumed) or YYYY-MM-DDTHH:MM:SS for a specific time"
                    ),
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "Optional: only messages on/before this date/time. Format YYYY-MM-DD "
                        "(end of day assumed) or YYYY-MM-DDTHH:MM:SS for a specific time"
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_semantic",
        "description": (
            "Meaning-based search over the owner's own messages - finds relevant messages even "
            "if they don't contain the exact query words (paraphrases, related topics), optionally "
            "scoped to one chat and/or a date range. Use this instead of search_messages when the "
            "exact wording is unknown or the request is conceptual (e.g. 'did anyone complain about "
            "the price'). Returns a flat list of the best matches, not paginated."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "chat_id": {"type": "string", "description": "Optional: restrict the search to this chat"},
                "limit": {
                    "type": "integer",
                    "description": "Optional: how many matches to return (default 10, max 50)",
                },
                "start_date": {
                    "type": "string",
                    "description": (
                        "Optional: only messages on/after this date/time. Format YYYY-MM-DD "
                        "(midnight assumed) or YYYY-MM-DDTHH:MM:SS for a specific time"
                    ),
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "Optional: only messages on/before this date/time. Format YYYY-MM-DD "
                        "(end of day assumed) or YYYY-MM-DDTHH:MM:SS for a specific time"
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_knowledge_semantic",
        "description": "Meaning-based ranked search over this agent's own knowledge base (saved reference info like inventory, price lists, policies, FAQs) - finds the most relevant chunks directly, even if they don't share exact words with the query. Prefer this over get_knowledge_index/fetch_chunk whenever a knowledge base exists: it's faster and doesn't require browsing the whole index first. Returns a flat list of the best matches, not paginated.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up, in natural language"},
                "limit": {"type": "integer", "description": "Optional: how many matches to return (default 5, max 20)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_knowledge_index",
        "description": "List every chunk of this agent's own uploaded knowledge-base documents as {document_id, filename, chunk_id, excerpt}. Fallback for a small knowledge base, or for chunks with no embedding yet - prefer search_knowledge_semantic first when a knowledge base exists. Browse this, then call fetch_chunk on the chunk_id(s) that look relevant.",
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
        "name": "list_attached_files",
        "description": "List files the owner has attached in their own agent chat, available to send to someone else via send_attached_file. Each entry has a file_id, filename, an optional caption the owner typed when attaching it, kind, mime, size, and when it was attached. Use the filename and caption together to judge what a file shows/contains (e.g. a caption \"our new laptop model\" on an image means it's a photo of that laptop) - call this whenever a chat counterpart asks for a photo/file/document, even if they don't name it exactly, to check whether something the owner already shared matches what they're asking for.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Optional: how many files to return (default 20, max 50)"}},
        },
    },
    {
        "name": "send_attached_file",
        "description": "Resend a file the owner previously attached in their own agent chat (see list_attached_files) into a real chat, e.g. sending a price list or brochure to a customer. Only files the owner attached in their own agent chat can be sent this way - you cannot send a file from any other chat. The call can be denied if the file does not appear to match what the other person actually asked for - if that happens, do not claim you sent something; instead ask a clarifying question or check list_attached_files again for a better match.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Target chat id to send the file into"},
                "file_id": {"type": "string", "description": "file_id from list_attached_files"},
                "caption": {"type": "string", "description": "Optional caption text to send alongside the file"},
            },
            "required": ["chat_id", "file_id"],
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
        "name": "set_agent_identity",
        "description": "Set the agent's optional display name and/or whether it may truthfully admit to being an AI if directly asked. Either argument can be omitted to leave that field unchanged.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Optional name the agent may use when asked who it is. Omit if the owner doesn't want to set one."},
                "disclose_as_agent": {"type": "boolean", "description": "True: the agent must truthfully confirm being an AI/bot if directly asked. False (default): no permission to volunteer AI status unprompted."},
            },
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
        "description": "Schedule a single one-time task, immediately or in the future (reuses the same mechanism as a recurring schedule entry, kind=once).",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Free-text instruction to execute at the scheduled time"},
                "execute_at": {"type": "string", "description": "ISO-8601 UTC instant, or the literal string 'now' to run as soon as possible (within about a minute)"},
                "scoped_system_prompt": {"type": "string", "description": "Optional: a short, self-contained system prompt limiting what this one firing may do (e.g. 'Ask if they can come Saturday, then tell the owner the answer. Do nothing else.'). If omitted, the task runs under the agent's normal persona and rules."},
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
        "name": "find_chat_by_name",
        "description": "Find which of the owner's own chats a name/nickname they mentioned refers to (e.g. 'message Dana', 'what did mom say') - matches against chat titles (group names, or the other person's display name/username), NOT message content. Use this instead of resolve_user whenever the owner names someone informally rather than giving an exact phone number or username. Returns 0-5 candidate matches, best first. If it returns 0 matches, tell the owner you couldn't find a chat by that name and offer resolve_user (exact phone/username) as a fallback. If it returns exactly 1 match, proceed with that chat_id directly. If it returns 2+ matches, NEVER guess - list the candidate names back to the owner and ask which one they meant before doing anything with a chat_id.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The name/nickname the owner used, as they said it"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_capacity_status",
        "description": "Get every rate limit relevant to this agent (activation quota, Gemini calls/min, daily active-time budget, per-sender unknown-contact quota, knowledge base size, schedule entries) alongside current usage, plus a rough estimate of how many new conversations per hour the agent can currently handle. Read-only, does not consume any quota. Use this near the end of setup, before finish_building_agent, so you can tell the owner roughly what to expect - phrase the estimate as approximate, never as a guarantee.",
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
    {
        "name": "spawn_ephemeral_task",
        "description": "Start a short-lived task that messages one or more people, waits for their replies, then summarizes the results back to the owner and cleans itself up automatically. Use for one-off coordination like asking several people the same question (e.g. 'ask X and Y if they're coming Saturday and tell me their answers'). You MUST call resolve_user first for each named person to get their chat_id - never guess one.",
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "What to ask/say to each person, written as an instruction (e.g. 'Ask if they can come to the event Saturday.')"},
                "chat_ids": {"type": "array", "items": {"type": "string"}, "description": "chat_id of each person to message, from resolve_user"},
                "timeout_minutes": {"type": "integer", "description": "Give up and summarize whoever replied after this many minutes (default 1440 = 24h)"},
            },
            "required": ["instruction", "chat_ids"],
        },
    },
    {
        "name": "no_reply_needed",
        "description": "Call this instead of replying when the owner's latest message doesn't need a new response - e.g. it already answers a question you just asked and are waiting on, it's a brief acknowledgement with nothing left to add, or a burst of coalesced messages turned out not to change anything since your last turn. Ends the turn silently with no message posted to the chat.",
    },
    {
        "name": "save_knowledge_from_text",
        "description": (
            "Save a block of text the owner just sent as permanent, searchable background "
            "knowledge (e.g. an inventory list, price list, policy document, FAQ) instead of "
            "letting it sit inline in this conversation forever. Use this ONLY for reference/lookup "
            "data that should never need to be re-read verbatim on every future turn - never for "
            "normal conversational instructions, questions, or one-off requests, which should just "
            "stay inline as usual. After calling this, you MUST tell the owner in this same turn "
            "what you saved and briefly why (e.g. that this keeps it out of every future message so "
            "it doesn't get re-sent and burn tokens on every turn, and that they can ask you to "
            "remove or update it anytime) - never save silently."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_label": {
                    "type": "string",
                    "description": "A short human-readable label for this content (e.g. 'Store inventory', 'Refund policy')",
                },
                "content": {"type": "string", "description": "The full text to save, verbatim"},
            },
            "required": ["source_label", "content"],
        },
    },
]

# --- Builder sub-state schema sets (ADR 0049) --------------------------------
# ADR 0084: the two Help personas need read-only access to the agent's own
# knowledge base (reference docs seeded for them) even though they otherwise
# stay zero-action (ADR 0064) - pulled from TOOL_SCHEMAS the same way the
# Supervisor/Builder-only refs below are pulled from CONFIG_TOOL_SCHEMAS.
_SEARCH_KNOWLEDGE_SEMANTIC_SCHEMA = next(s for s in TOOL_SCHEMAS if s["name"] == "search_knowledge_semantic")
_GET_KNOWLEDGE_INDEX_SCHEMA = next(s for s in TOOL_SCHEMAS if s["name"] == "get_knowledge_index")
_FETCH_CHUNK_SCHEMA = next(s for s in TOOL_SCHEMAS if s["name"] == "fetch_chunk")
_RESUME_PAUSED_CHAT_SCHEMA = next(s for s in CONFIG_TOOL_SCHEMAS if s["name"] == "resume_paused_chat")
_RESOLVE_USER_SCHEMA = next(s for s in CONFIG_TOOL_SCHEMAS if s["name"] == "resolve_user")
_FIND_CHAT_BY_NAME_SCHEMA = next(s for s in CONFIG_TOOL_SCHEMAS if s["name"] == "find_chat_by_name")
_SPAWN_EPHEMERAL_TASK_SCHEMA = next(s for s in CONFIG_TOOL_SCHEMAS if s["name"] == "spawn_ephemeral_task")
# ADR 0065: every builder_state - including the two zero-action Help states -
# needs a way to end a turn without posting a message.
_NO_REPLY_NEEDED_SCHEMA = next(s for s in CONFIG_TOOL_SCHEMAS if s["name"] == "no_reply_needed")
# ADR 0078: config-mode-only, decided by the model mid-turn - needed in the
# Supervisor's à-la-carte list the same way resolve_user/spawn_ephemeral_task
# are (Builder already gets it via the full _BUILDER_TOOL_SCHEMAS union below).
_SAVE_KNOWLEDGE_FROM_TEXT_SCHEMA = next(s for s in CONFIG_TOOL_SCHEMAS if s["name"] == "save_knowledge_from_text")
# get_capacity_status is deliberately excluded from the Builder interview flow (not called
# during finishing, per user request) - it stays available to other config-mode contexts.
_BUILDER_TOOL_SCHEMAS = [s for s in CONFIG_TOOL_SCHEMAS if s["name"] != "get_capacity_status"]

# ADR 0062: Supervisor also gets the full execution-mode toolset (TOOL_SCHEMAS),
# plus resolve_user + spawn_ephemeral_task (otherwise config-mode-only), so the
# owner can issue direct "act as me" commands (send/reply/create_chat/search/
# etc targeting OTHER chats, or "message X and tell me what they say") from
# their own agent chat without being routed into the Builder interview flow
# first. See dispatch.py's matching handler union in builder_handoff.py.
BUILDER_STATE_TOOL_SCHEMAS = {
    BuilderState.SUPERVISOR: [
        TRANSFER_TO_BUILDER_SCHEMA,
        TRANSFER_TO_HELP_BUILDING_SCHEMA,
        TRANSFER_TO_HELP_GENERAL_SCHEMA,
        _RESUME_PAUSED_CHAT_SCHEMA,
        _RESOLVE_USER_SCHEMA,
        _FIND_CHAT_BY_NAME_SCHEMA,
        _SPAWN_EPHEMERAL_TASK_SCHEMA,
        _NO_REPLY_NEEDED_SCHEMA,
        _SAVE_KNOWLEDGE_FROM_TEXT_SCHEMA,
        *TOOL_SCHEMAS,
    ],
    BuilderState.BUILDER: [
        # ADR 0062-style union: the Builder is talking to its own supervised
        # owner too, so it also gets the full execution-mode toolset - see
        # builder_handoff.py's matching handler union.
        *TOOL_SCHEMAS,
        *_BUILDER_TOOL_SCHEMAS,
        TRANSFER_TO_HELP_BUILDING_SCHEMA,
        TRANSFER_TO_HELP_GENERAL_SCHEMA,
        TRANSFER_TO_SUPERVISOR_SCHEMA,
        FINISH_BUILDING_AGENT_SCHEMA,
    ],
    # ADR 0064: two disjoint Help personas, replacing the single help_agent
    # state. Neither gathers/saves config - only transfer tools, same
    # zero-action posture the original Help state had. Each can reach the
    # other directly, or fall back to the Supervisor when unsure - the
    # Supervisor is the one state that always knows where to route next.
    BuilderState.HELP_BUILDING: [
        TRANSFER_TO_BUILDER_SCHEMA,
        TRANSFER_TO_HELP_GENERAL_SCHEMA,
        TRANSFER_TO_SUPERVISOR_SCHEMA,
        _NO_REPLY_NEEDED_SCHEMA,
        _SEARCH_KNOWLEDGE_SEMANTIC_SCHEMA,
        _GET_KNOWLEDGE_INDEX_SCHEMA,
        _FETCH_CHUNK_SCHEMA,
    ],
    BuilderState.HELP_GENERAL: [
        TRANSFER_TO_HELP_BUILDING_SCHEMA,
        TRANSFER_TO_SUPERVISOR_SCHEMA,
        _NO_REPLY_NEEDED_SCHEMA,
        _SEARCH_KNOWLEDGE_SEMANTIC_SCHEMA,
        _GET_KNOWLEDGE_INDEX_SCHEMA,
        _FETCH_CHUNK_SCHEMA,
    ],
}
