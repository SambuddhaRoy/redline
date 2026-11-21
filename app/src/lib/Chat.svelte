<script>
  import { app, persist, newThread, currentThread } from './app.svelte.js';
  import { api, chatStream, familyName, gb, kTokens, setAccent } from './api.js';
  import { render } from './markdown.js';

  let models = $state([]); // GET /manage/library
  let input = $state('');
  let busy = $state(false);
  let note = $state('');
  let now = $state(Date.now());
  let ctrl = null;
  let scroller;

  const thread = $derived(currentThread());
  const engine = $derived(app.status?.engine);
  const selectedId = $derived(thread?.model ?? engine?.model ?? models[0]?.id ?? null);
  const model = $derived(models.find((m) => m.id === selectedId));
  const turns = $derived.by(() => {
    const out = [];
    (thread?.messages ?? []).forEach((m, i, all) => {
      if (m.role === 'user') out.push({ n: out.length + 1, user: m, answer: all[i + 1], index: i });
    });
    return out;
  });
  const lastStats = $derived(
    [...(thread?.messages ?? [])].reverse().find((m) => m.role === 'assistant' && m.stats?.tps)?.stats,
  );
  const loadedHere = $derived(engine?.model === selectedId);
  const warming = $derived(loadedHere && engine?.state === 'starting');
  const elapsed = $derived(engine ? Math.max(0, Math.round(now / 1000 - engine.since)) : 0);
  const ctx = $derived(loadedHere ? engine.context : model?.fit?.default_context);

  $effect(() => {
    if (app.online) api.library().then((m) => (models = m)).catch(() => {});
  });
  $effect(() => setAccent(model?.repo));
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });
  // A first message typed during setup goes out as soon as there's a model to send it to.
  $effect(() => {
    if (app.queued && selectedId) {
      const text = app.queued;
      app.queued = '';
      send(text);
    }
  });

  const pretty = (repo) => repo?.split('/').pop() ?? '';
  const mmss = (s) => Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  const size = (text) => (text.length <= 60 ? 'xl' : text.length <= 140 ? 'l' : 'm');

  function scrollDown() {
    requestAnimationFrame(() => scroller?.scrollTo({ top: scroller.scrollHeight }));
  }

  async function pickModel(id) {
    note = '';
    const t = thread ?? newThread(id);
    t.model = id;
    persist();
    if (engine?.model !== id) {
      try { await api.load(id); } catch (e) { note = e.message; }
    }
  }

  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    if (!selectedId) { note = 'Download a model first.'; return; }
    const t = thread ?? newThread(selectedId);
    t.model ??= selectedId;
    if (!t.messages.length) t.title = text.slice(0, 60);
    t.messages.push({ role: 'user', content: text });
    t.messages.push({ role: 'assistant', content: '', reasoning: '', stats: null, error: null });
    const msg = t.messages[t.messages.length - 1];
    const history = t.messages.slice(0, -1).map(({ role, content }) => ({ role, content }));
    input = '';
    note = '';
    busy = true;
    ctrl = new AbortController();
    scrollDown();
    const t0 = performance.now();
    let first = 0, last = 0, n = 0, usage = null;
    try {
      for await (const d of chatStream(t.model, history, ctrl.signal)) {
        if (d.usage) { usage = d.usage; continue; }
        last = performance.now();
        first ||= last;
        n++;
        msg.content += d.content;
        msg.reasoning += d.reasoning;
        scrollDown();
      }
    } catch (e) {
      if (e.name !== 'AbortError') msg.error = e.message;
    }
    const tokens = usage?.completion_tokens ?? n;
    // The gateway times the first token from when the model was ready, so a load on demand doesn't count.
    const served = first ? (await api.stats().catch(() => [])).at(-1) : null;
    msg.stats = first && {
      ttft: served?.ttft_ms ?? Math.round(first - t0),
      tps: tokens > 1 && last > first ? Math.round((tokens - 1) / ((last - first) / 1000)) : null,
      tokens,
      secs: ((last - t0) / 1000).toFixed(1),
    };
    t.updated = Date.now();
    busy = false;
    ctrl = null;
    persist();
  }

  function retry(turn) {
    const text = turn.user.content;
    thread.messages.splice(turn.index); // drop this turn and everything after it
    send(text);
  }

  function keydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }
</script>

<div class="chat">
  <main bind:this={scroller}>
    <div class="layout">
      <section class="turns" aria-label="Conversation">
        {#if !turns.length}
          <h1 class="display empty">Ask <span class="highlight">anything.</span><br />It's all on your GPU.</h1>
          <div class="starters">
            {#each ['Explain this error message', 'Write a PowerShell script that', 'Review my Python function'] as s}
              <button class="pill quiet" onclick={() => (input = s + ' ')}>{s}</button>
            {/each}
          </div>
        {/if}
        {#each turns as turn (turn.index)}
          <article class="turn">
            <span class="n mono muted">{String(turn.n).padStart(2, '0')}</span>
            <h2 class="display prompt {size(turn.user.content)}">{turn.user.content}</h2>
            <div class="answer">
              {#if turn.answer?.reasoning}
                <details class="muted"><summary>Thinking</summary><p class="thought">{turn.answer.reasoning}</p></details>
              {/if}
              {#if turn.answer?.content}
                <div class="prose">{@html render(turn.answer.content)}</div>
              {:else if busy && turn === turns.at(-1) && !turn.answer?.error}
                <p class="muted">{warming ? 'Waiting for the model to warm up…' : 'Thinking…'}</p>
              {/if}
              {#if turn.answer?.error}<p class="error">{turn.answer.error}</p>{/if}
              {#if turn.answer?.stats || turn.answer?.error}
                <div class="actions">
                  {#if turn.answer?.stats}
                    <span class="mono muted stamp">{turn.answer.stats.tps ?? '—'} tok/s · {turn.answer.stats.ttft} ms first token · {turn.answer.stats.tokens} tokens</span>
                  {/if}
                  <button class="pill" onclick={() => navigator.clipboard.writeText(turn.answer.content)}>Copy</button>
                  <button class="pill" disabled={busy} onclick={() => retry(turn)}>Retry</button>
                </div>
              {/if}
            </div>
          </article>
        {/each}
      </section>

      <aside aria-label="Model and speed">
        {#if models.length}
          <label class="label muted" for="model">Model</label>
          <div class="picker">
            <span class="swatch" aria-hidden="true"></span>
            <select id="model" value={selectedId} onchange={(e) => pickModel(e.currentTarget.value)} disabled={busy}>
              {#each models as m (m.id)}<option value={m.id}>{pretty(m.repo)}</option>{/each}
            </select>
          </div>
          {#if warming}
            <p class="status">Warming up · <b class="mono">{mmss(elapsed)}</b></p>
            <p class="mono muted log">{engine.log_tail?.at(-1) ?? ''}</p>
          {:else if loadedHere && engine?.state === 'error'}
            <p class="error">The model stopped. {engine.error?.split('\n')[0]}</p>
            <button class="pill" onclick={() => pickModel(selectedId)}>Load again</button>
          {:else if !loadedHere}
            <p class="status muted">Not loaded. Sending a message loads it, or <button class="link" onclick={() => pickModel(selectedId)}>load now</button>.</p>
          {/if}
          {#if lastStats}<div class="display speed">{lastStats.tps}</div>{/if}
          <span class="highlight speed-label">{lastStats ? 'tokens a second' : 'Speed shows after an answer'}</span>
          {#if lastStats}<p class="mono muted detail">{lastStats.ttft} ms to first token<br />{lastStats.tokens} tokens · {lastStats.secs} s</p>{/if}
        {:else}
          <p class="status">No models yet.</p>
          <button class="pill primary" onclick={() => (app.screen = 'models')}>Get a model</button>
        {/if}
        {#if note}<p class="error">{note}</p>{/if}
      </aside>
    </div>
  </main>

  <form class="composer" onsubmit={(e) => { e.preventDefault(); send(input); }}>
    <label class="sr-only" for="ask">Message</label>
    <textarea id="ask" rows={Math.min(6, input.split('\n').length)} bind:value={input} onkeydown={keydown}
      placeholder="Ask anything. It's all on your GPU."></textarea>
    {#if busy}
      <button type="button" class="go stop" onclick={() => ctrl?.abort()}>Stop</button>
    {:else}
      <button type="submit" class="go display" disabled={!input.trim() || !models.length}>Go</button>
    {/if}
  </form>

  {#if model}
    <footer class="mono">
      <span class="accent">{pretty(model.repo)}</span>
      <span>{familyName(model.repo)}</span>
      <span>{model.quant}</span>
      {#if ctx}<span>{kTokens(ctx)} context</span>{/if}
      <span>Weights {gb(model.weights_bytes)}</span>
      <span>TensorRT-LLM 1.3</span>
      <span>Local · nothing leaves this PC</span>
    </footer>
  {/if}
</div>

<style>
  .chat { flex: 1; min-height: 0; display: flex; flex-direction: column; height: calc(100vh - 76px); }
  main { flex: 1; overflow-y: auto; }
  .layout {
    max-width: 1120px; margin: 0 auto; padding: var(--sp-7) var(--sp-6) var(--sp-5);
    display: flex; flex-wrap: wrap; gap: var(--sp-7); align-items: flex-start;
  }
  .turns { flex: 999 1 560px; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-7); }
  .empty { margin: 0; font-size: 64px; line-height: 0.98; }
  .starters { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-top: calc(-1 * var(--sp-5)); }
  .turn { display: grid; grid-template-columns: 24px minmax(0, 1fr); column-gap: var(--sp-4); row-gap: var(--sp-5); }
  .n { font-size: 13px; font-weight: 700; padding-top: var(--sp-3); }
  .prompt { margin: 0; line-height: 1; white-space: pre-wrap; overflow-wrap: anywhere; }
  .prompt.xl { font-size: 64px; }
  .prompt.l { font-size: 40px; line-height: 1.05; }
  .prompt.m { font-size: 22px; line-height: 1.35; font-weight: 700; letter-spacing: -0.01em; }
  .answer { grid-column: 2; display: flex; flex-direction: column; gap: var(--sp-4); max-width: 680px; }
  details summary { cursor: pointer; font-size: 14px; }
  .thought { white-space: pre-wrap; font-size: 14px; margin: var(--sp-2) 0 0; }
  .actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
  .stamp { font-size: 12px; margin-right: auto; }
  .error { color: var(--ink); background: var(--surface-2); padding: var(--sp-3) var(--sp-4); border-radius: var(--r-sm); margin: 0; font-size: 14px; }

  aside { flex: 1 1 240px; min-width: 0; max-width: 100%; display: flex; flex-direction: column; gap: var(--sp-4); align-items: flex-start; position: sticky; top: 0; }
  .label { font-size: 13px; font-weight: 600; margin-bottom: calc(-1 * var(--sp-3)); }
  .picker {
    display: flex; align-items: center; gap: var(--sp-2); height: var(--control); padding: 0 var(--sp-2) 0 var(--sp-4);
    border-radius: var(--r-pill); background: var(--surface-2); max-width: 100%;
  }
  .picker select { height: 100%; border: none; background: transparent; font-size: 14px; font-weight: 600; cursor: pointer; min-width: 0; }
  .swatch { width: 12px; height: 12px; flex: none; border-radius: var(--r-pill); background: var(--accent); }
  .status { margin: 0; font-size: 14px; }
  .log { margin: 0; font-size: 11px; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .link { border: none; background: none; padding: 0; text-decoration: underline; cursor: pointer; font-size: inherit; }
  .speed { font-size: 128px; line-height: 0.85; letter-spacing: -0.06em; padding-top: var(--sp-4); }
  .speed-label { font-size: 15px; font-weight: 600; padding: var(--sp-1) var(--sp-3); }
  .detail { margin: 0; font-size: 13px; line-height: 1.8; }

  .composer {
    width: 100%; max-width: 1120px; margin: 0 auto var(--sp-5); padding: 0 var(--sp-6);
    display: flex; align-items: flex-end; gap: var(--sp-2);
  }
  .composer textarea {
    flex: 1; min-width: 0; resize: none; min-height: var(--control-lg); max-height: 200px;
    padding: var(--sp-4) var(--sp-5); border: 1.5px solid var(--line); border-radius: 28px;
    background: var(--surface); font-size: 18px; line-height: 1.35; outline: none;
  }
  .go {
    height: var(--control-lg); padding: 0 var(--sp-6); border-radius: var(--r-pill); border: none; cursor: pointer;
    background: var(--accent); color: var(--on-accent); font-size: 18px; font-weight: 800;
  }
  .go:disabled { opacity: 0.45; cursor: default; }
  .go.stop { background: var(--ink); color: var(--bg); font-family: var(--text); font-weight: 700; font-size: 15px; }

  footer {
    background: var(--strip-bg); color: var(--strip-ink); padding: var(--sp-3) var(--sp-6); display: flex;
    flex-wrap: wrap; gap: var(--sp-6); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase;
  }
  footer .accent { color: var(--accent); }
</style>
