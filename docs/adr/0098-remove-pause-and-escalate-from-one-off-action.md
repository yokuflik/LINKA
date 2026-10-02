# 0098 - Remove pause_and_escalate from ONE_OFF_ACTION

Status: Accepted

`pause_and_escalate` freezes the *triggering* chat (no `chat_id` argument).
ONE_OFF_ACTION runs only in the owner's own agent chat (ADR 0093), so the tool
would pause that chat itself - useless and harmful. It entered via the ADR 0062
execution-toolset union. Removed from ONE_OFF_ACTION's handlers, schemas and
prompt; it stays in execution mode (third-party chats), where it is meant to
run. `resume_paused_chat` stays (owner-initiated resume of a third-party chat).
