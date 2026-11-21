// Model answers as rich text: GitHub-style markdown, LaTeX math (KaTeX), highlighted code with a copy
// button. Everything goes through DOMPurify last: model output must never become live markup in a
// webview that can call the native shell.
import { Marked } from 'marked';
import katex from 'katex';
import hljs from 'highlight.js/lib/common';
import powershell from 'highlight.js/lib/languages/powershell';
import DOMPurify from 'dompurify';

hljs.registerLanguage('powershell', powershell);

const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function tex(src, display) {
  try {
    return katex.renderToString(src, { displayMode: display, throwOnError: false, output: 'html' });
  } catch {
    return `<code>${esc(src)}</code>`;
  }
}

// $$…$$ and \[…\] blocks; $…$ and \(…\) inline. "$5 and $10" stays text: no space after the opening $,
// and no digit right after the closing one.
const math = {
  extensions: [
    {
      name: 'blockMath', level: 'block',
      start: (s) => { const i = s.search(/\$\$|\\\[/); return i < 0 ? undefined : i; },
      tokenizer(src) {
        const m = /^\$\$([\s\S]+?)\$\$|^\\\[([\s\S]+?)\\\]/.exec(src);
        if (m) return { type: 'blockMath', raw: m[0], text: (m[1] ?? m[2]).trim() };
      },
      renderer: (t) => `<div class="math">${tex(t.text, true)}</div>`,
    },
    {
      name: 'inlineMath', level: 'inline',
      start: (s) => { const i = s.search(/\$|\\\(/); return i < 0 ? undefined : i; },
      tokenizer(src) {
        const m = /^\$(?!\s)((?:\\.|[^\\$\n])+?)\$(?!\d)|^\\\(([\s\S]+?)\\\)/.exec(src);
        if (m) return { type: 'inlineMath', raw: m[0], text: m[1] ?? m[2] };
      },
      renderer: (t) => tex(t.text, false),
    },
  ],
};

const marked = new Marked({ gfm: true, breaks: true }, math, {
  renderer: {
    code({ text, lang }) {
      const l = (lang || '').trim().split(/\s/)[0].toLowerCase();
      const known = l && hljs.getLanguage(l);
      const body = known ? hljs.highlight(text, { language: l, ignoreIllegals: true }).value : hljs.highlightAuto(text).value;
      return `<figure class="code"><figcaption><span>${esc(known ? l : 'code')}</span>` +
        `<button type="button" class="copy" data-copy="">Copy</button></figcaption>` +
        `<pre><code class="hljs">${body}</code></pre></figure>`;
    },
  },
});

// KaTeX positions glyphs with inline style attributes, so those stay (they can't run code); style tags,
// forms and anything scriptable are removed.
export const render = (text) =>
  DOMPurify.sanitize(marked.parse(text), { FORBID_TAGS: ['style', 'form', 'input', 'textarea', 'select'] });
