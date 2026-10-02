// Shared WhatsApp-style text formatting for message bubbles (used by both
// MessageList.js and AgentChatView.js, so it lives here instead of being
// duplicated per-component):
// - lines starting with "- " render as a <ul> list, "1. " / "1) " as an <ol> list
// - *word* / *a few words* renders as <strong>bold</strong>
// - _text_ italic, ~text~ strikethrough, ```text``` monospace (not formatted inside)
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
// _italic_ / ~strike~ need a non-word char (or line edge) before the opener and
// after the closer so snake_case_names and "a~b" are left alone.
const MESSAGE_ITALIC_RE = /(^|[^\w_])_(\S(?:[^_\n]*\S)?)_(?![\w_])/g;
const MESSAGE_STRIKE_RE = /(^|[^\w~])~(\S(?:[^~\n]*\S)?)~(?![\w~])/g;
const MESSAGE_CODE_RE = /```([\s\S]+?)```/g;
function applyInlineBold(escapedLine) {
  return escapedLine
    .replace(MESSAGE_BOLD_RE, '<strong>$1</strong>')
    .replace(MESSAGE_ITALIC_RE, '$1<em>$2</em>')
    .replace(MESSAGE_STRIKE_RE, '$1<del>$2</del>');
}

function formatMessageContent(text) {
  // Pull ```monospace``` spans out first (placeholder tokens) so nothing inside
  // them is interpreted, then splice them back after the line pass.
  const codeSpans = [];
  const escaped = escapeMessageHtml(text || '').replace(MESSAGE_CODE_RE, (_, code) => {
    codeSpans.push(code);
    return '\u0000' + (codeSpans.length - 1) + '\u0000';
  });
  const lines = escaped.split('\n');
  let html = '';
  let listTag = null; // 'ul' | 'ol' while inside a list
  for (const line of lines) {
    const bulletMatch = /^-\s+(.*)$/.exec(line);
    const numMatch = bulletMatch ? null : /^(\d{1,3})[.)]\s+(.*)$/.exec(line);
    const tag = bulletMatch ? 'ul' : numMatch ? 'ol' : null;
    if (tag) {
      if (listTag !== tag) {
        if (listTag) html += '</' + listTag + '>';
        html += tag === 'ul'
          ? '<ul class="list-disc ps-6 my-0.5">'
          : '<ol class="list-decimal ps-6 my-0.5" start="' + numMatch[1] + '">';
        listTag = tag;
      }
      html += '<li>' + applyInlineBold((bulletMatch || numMatch)[tag === 'ul' ? 1 : 2]) + '</li>';
    } else {
      if (listTag) { html += '</' + listTag + '>'; listTag = null; }
      html += (html && !/<\/[uo]l>$/.test(html) ? '<br>' : '') + applyInlineBold(line);
    }
  }
  if (listTag) html += '</' + listTag + '>';
  return html.replace(/\u0000(\d+)\u0000/g, (_, i) =>
    '<code class="font-mono text-[0.9em] bg-black/10 rounded px-1 whitespace-pre-wrap">' + codeSpans[+i] + '</code>');
}
