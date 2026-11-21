// Small markdown renderer for model answers. It escapes ALL html first and only then adds tags, so model
// output can never inject markup into the webview (which can call the native shell).
// ponytail: fences, headings, lists, paragraphs, `code`, **bold**, *italic*. Tables and links stay plain text;
// swap in a real parser plus a sanitizer if answers need them.
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function inline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
}

export function render(text) {
  const out = [];
  const parts = esc(text).split(/^```[^\n]*\n?/m); // odd parts are code; an unclosed fence (mid-stream) still renders
  parts.forEach((part, i) => {
    if (i % 2) {
      out.push('<pre><code>' + part.replace(/\n$/, '') + '</code></pre>');
      return;
    }
    for (const block of part.split(/\n{2,}/)) {
      const lines = block.split('\n').filter((l) => l.trim());
      if (!lines.length) continue;
      const h = lines[0].match(/^(#{1,3})\s+(.*)/);
      if (h && lines.length === 1) {
        out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
      } else if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
        out.push('<ul>' + lines.map((l) => '<li>' + inline(l.replace(/^\s*[-*]\s+/, '')) + '</li>').join('') + '</ul>');
      } else if (lines.every((l) => /^\s*\d+[.)]\s+/.test(l))) {
        out.push('<ol>' + lines.map((l) => '<li>' + inline(l.replace(/^\s*\d+[.)]\s+/, '')) + '</li>').join('') + '</ol>');
      } else {
        out.push('<p>' + lines.map(inline).join('<br>') + '</p>');
      }
    }
  });
  return out.join('');
}
