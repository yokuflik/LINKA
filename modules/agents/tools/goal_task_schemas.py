"""Tool schemas for goal-driven conversational tasks (ADR 0099).

Leaf module (no imports from schemas.py) so schemas.py can pull the two
config-mode schemas into ONE_OFF_ACTION without an import cycle; the
goal-turn schema list itself is assembled in dispatch.py."""

START_GOAL_TASK_SCHEMA = {
    "name": "start_goal_task",
    "description": (
        "Start a goal-driven conversation with ONE person that continues over several messages "
        "until a goal is reached, then reports back to the owner and cleans itself up. Use when the "
        "owner wants something ACHIEVED through back-and-forth (e.g. 'buy X from this person with "
        "these settings', 'agree on a meeting time with Y', 'find out the price of Z'). For a "
        "single question to one or more people use spawn_ephemeral_task instead; for a single "
        "message use send_message. You MUST call resolve_user / find_chat_by_name first to get the "
        "chat_id - never guess one. Only one goal task per chat at a time. Do NOT set may_commit "
        "unless the owner explicitly said you may finalize/pay/book on their behalf."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "chat_id of the person to converse with, from resolve_user/find_chat_by_name"},
            "opening_message": {"type": "string", "description": "Your FIRST message to the person, written now in their language and the owner's voice, covering everything the owner specified. It is sent immediately. Always provide it."},
            "goal": {"type": "string", "description": "What to achieve, in the owner's words, including every detail they gave (item, quantity, settings, budget...)"},
            "done_when": {"type": "string", "description": "A concrete, checkable success condition (e.g. 'the seller confirms the item is available at or under 500 NIS and gives pickup details')"},
            "constraints": {"type": "string", "description": "Limits the agent must respect (max price, deadlines, things not to say or agree to)"},
            "may_commit": {"type": "boolean", "description": "true ONLY if the owner explicitly allowed finalizing/paying/booking. Default false: the task then stops at 'ready for owner confirmation'."},
            "max_turns": {"type": "integer", "description": "Max agent turns before the task is force-closed (capped server-side)"},
            "timeout_minutes": {"type": "integer", "description": "Give up after this many minutes (default 1440 = 24h, capped server-side)"},
        },
        "required": ["chat_id", "goal", "done_when"],
    },
}

CANCEL_GOAL_TASK_SCHEMA = {
    "name": "cancel_goal_task",
    "description": "Cancel an active goal task (from start_goal_task) because the owner no longer wants it. Identify it by the chat it targets.",
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "chat_id the goal task targets"},
        },
        "required": ["chat_id"],
    },
}

COMPLETE_TASK_SCHEMA = {
    "name": "complete_task",
    "description": (
        "End the task because it is finished. Call this as soon as DONE WHEN is met, the other "
        "side declined, or (when you may not commit) the terms are agreed and only the owner's "
        "confirmation is left. The summary is what your owner will be told - include exact terms."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["achieved", "ready_for_owner_confirmation", "declined_by_counterpart"]},
            "summary": {"type": "string", "description": "Exact result/terms agreed, or what the other side said"},
        },
        "required": ["outcome", "summary"],
    },
}

FAIL_TASK_SCHEMA = {
    "name": "fail_task",
    "description": "End the task because the goal cannot be reached (other side can't deliver, stopped responding usefully, or it is out of your scope/constraints). Tell the owner why in the summary.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": ["cannot_achieve", "counterpart_unresponsive", "out_of_scope"]},
            "summary": {"type": "string", "description": "What happened and what was tried"},
        },
        "required": ["reason", "summary"],
    },
}
