// Shared WhatsApp-style text formatting for message bubbles (used by both
// MessageList.js and AgentChatView.js, so it lives here instead of being
// duplicated per-component):
// - lines starting with "- " render as a real <ul><li> list
// - *word* / *a few words* renders as <strong>bold</strong>
// Escapes HTML first so this can't be used to inject markup - only the
// formatting syntax itself is interpreted.
function escapeMessageHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// Matches *...* with no whitespace touching the asterisks and no line break
// inside, so a lone "*" is left alone instead of accidentally swallowing the
// rest of the message.
const MESSAGE_BOLD_RE = /\*(\S(?:[^*\n]*\S)?)\*/g;
function applyInlineBold(escapedLine) {
  return escapedLine.replace(MESSAGE_BOLD_RE, '<strong>$1</strong>');
}

function formatMessageContent(text) {
  const escaped = escapeMessageHtml(text || '');
  const lines = escaped.split('\n');
  let html = '';
  let inList = false;
  for (const line of lines) {
    const bulletMatch = /^-\s+(.*)$/.exec(line);
    if (bulletMatch) {
      if (!inList) { html += '<ul class="list-disc pl-6 my-0.5">'; inList = true; }
      html += '<li>' + applyInlineBold(bulletMatch[1]) + '</li>';
    } else {
      if (inList) { html += '</ul>'; inList = false; }
      html += (html && !html.endsWith('>') ? '<br>' : '') + applyInlineBold(line);
    }
  }
  if (inList) html += '</ul>';
  return html;
}
