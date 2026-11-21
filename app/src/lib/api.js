// HTTP client for the Peregrine gateway (backend/peregrine/api.py).
// VITE_API lets a dev build point at a test gateway; the app itself always uses 47800.
export const BASE = import.meta.env.VITE_API || 'http://127.0.0.1:47800';

async function req(path, opts = {}) {
  const r = await fetch(BASE + path, { ...opts, headers: { 'Content-Type': 'application/json', ...opts.headers } });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const body = await r.json();
      msg = body.detail ?? body.error?.message ?? msg;
    } catch {}
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return r.status === 204 ? null : r.json();
}
const post = (path, body) => req(path, { method: 'POST', body: body && JSON.stringify(body) });
const q = encodeURIComponent;

export const api = {
  status: () => req('/manage/status'),
  stats: () => req('/manage/stats'),
  catalog: () => req('/manage/catalog'),
  library: () => req('/manage/library'),
  search: (text) => req('/manage/search?q=' + q(text)),
  variants: (repo) => req('/manage/variants?repo=' + q(repo)),
  inspect: (repo) => req('/manage/inspect?repo=' + q(repo)),
  download: (repo) => post('/manage/downloads', { repo }),
  cancelDownload: (id) => req('/manage/downloads/' + q(id), { method: 'DELETE' }),
  deleteModel: (id) => req('/manage/library/' + q(id), { method: 'DELETE' }),
  load: (model, context) => post('/manage/load', { model, context }),
  unload: () => post('/manage/unload'),
  about: (repo) => req('/manage/about?repo=' + q(repo)),
  avatar: (owner) => req('/manage/avatar?owner=' + q(owner)),
  hfAccount: () => req('/manage/hf/account'),
  hfLogin: (token) => req('/manage/hf/token', { method: 'PUT', body: JSON.stringify({ token }) }),
  hfLogout: () => req('/manage/hf/token', { method: 'DELETE' }),
  hermes: () => req('/manage/hermes'),
  hermesConnect: () => post('/manage/hermes/connect'),
  hermesDisconnect: () => post('/manage/hermes/disconnect'),
};

/** Streams a chat answer. Yields {content, reasoning} deltas, then {usage} if the server sends it. */
export async function* chatStream(model, messages, signal) {
  const r = await fetch(BASE + '/v1/chat/completions', {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, messages, stream: true, stream_options: { include_usage: true } }),
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error?.message ?? msg; } catch {}
    throw new Error(msg);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ') || line.startsWith('data: [DONE]')) continue;
      let obj;
      try { obj = JSON.parse(line.slice(6)); } catch { continue; }
      if (obj.usage) yield { usage: obj.usage };
      const d = obj.choices?.[0]?.delta;
      if (d && (d.content || d.reasoning_content || d.reasoning)) {
        yield { content: d.content || '', reasoning: d.reasoning_content || d.reasoning || '' };
      }
    }
  }
}

// Accent per model family, as on the Tokens board. Ink text sits on every one at 4.5:1 or better.
export const FAMILY = {
  nemotron: ['#76B900', 'NVIDIA Nemotron'], gemma: ['#5AC8FA', 'Google Gemma'], qwen: ['#9B6BFF', 'Qwen'],
  deepseek: ['#4D6BFE', 'DeepSeek'], llama: ['#FF6FB5', 'Meta Llama'], mistral: ['#FF8A1F', 'Mistral'],
  gptoss: ['#10A37F', 'OpenAI gpt-oss'], hermes: ['#FFC21A', 'Nous Hermes'],
};
export const BRAND = '#C6FF3D';

export function familyOf(repo = '') {
  const r = repo.toLowerCase();
  // Hermes first: DeepHermes is built on Llama but is a Nous model.
  for (const [key, words] of [
    ['hermes', ['hermes', 'nousresearch']], ['nemotron', ['nemotron']], ['gemma', ['gemma']], ['qwen', ['qwen']],
    ['deepseek', ['deepseek']], ['gptoss', ['gpt-oss']], ['mistral', ['mistral', 'devstral', 'magistral']], ['llama', ['llama']],
  ]) {
    if (words.some((w) => r.includes(w))) return key;
  }
  return null;
}
export const accentOf = (repo) => FAMILY[familyOf(repo)]?.[0] ?? BRAND;
export const familyName = (repo) => FAMILY[familyOf(repo)]?.[1] ?? 'Model';
/** The page accent follows the model in focus: the chat's model, the library's selection. */
export const setAccent = (repo) => document.documentElement.style.setProperty('--accent', accentOf(repo ?? ''));

export const gb = (bytes) => (bytes / 2 ** 30).toFixed(1) + ' GB'; // GiB, as Windows counts
export const kTokens = (n) => Math.round(n / 1024) + 'K';
