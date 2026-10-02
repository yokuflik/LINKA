"""Single source of truth for the chat formatting the PoC renders in message
bubbles (mirrors poc/composables/messageFormat.js - keep the two in sync).
Shared by the execution personas (personas.CHAT_STYLE_RULES) and the
config-mode prompts (builder_flow.STYLE_RULES).
"""

MESSAGE_FORMATTING_RULES = (
    "Message formatting supported in this chat (WhatsApp-style; anything else, "
    "e.g. markdown headers, **double asterisks**, [links](url), tables, is shown "
    "as literal characters, so never use it): *bold* with single asterisks, "
    "_italic_ with underscores, ~strikethrough~ with tildes, ```monospace``` "
    "with triple backticks (nothing inside is formatted), lines starting with "
    "\"- \" become a bullet list, lines starting with \"1. \" a numbered list. "
    "The markers must touch the text with no space inside (*like this*, not "
    "* like this *). Most messages need NO formatting at all - plain text is "
    "the default and what a real person would send. Use it only when it truly "
    "helps and a person would do the same: *bold* for one key word or detail "
    "worth catching (a price, a date, a name), a list only for 3+ distinct "
    "items such as options or steps, monospace only for something the reader "
    "must copy exactly (a code, an address, a command), italic/strikethrough "
    "rarely (e.g. striking out an old price). Never format a whole message, "
    "never stack several styles, never use it to sound structured or "
    "official, and never as a substitute for a normal conversational reply. "
)
