<script>
  import { fly, scale } from 'svelte/transition';
  import { backOut, elasticOut } from 'svelte/easing';
  import { Tween } from 'svelte/motion';
  import { app, persist, newThread, currentThread } from './app.svelte.js';
  import { api, chatStream, familyName, gb, kTokens, setAccent } from './api.js';
  import { render } from './markdown.js';
  import { openExternal } from './shell.js';
  import Thinking from './Thinking.svelte';

  const CONTEXTS = [4096, 8192, 16384, 32768, 65536, 131072, 262144];

  let models = $state([]); // GET /manage/library
  let input = $state('');
  let busy = $state(false);
  let note = $state('');
  let stick = $state(true); // follow new tokens only while the reader is at the bottom
  let ctrl = null;
  let scroller;
  let composer;

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
  const ctx = $derived(loadedHere ? engine.context : model?.fit?.default_context);
  const ctxOptions = $derived.by(() => {
    const fit = model?.fit;
    if (!fit) return [];
    const cap = fit.model_max ?? fit.max_context;
    const list = CONTEXTS.filter((c) => c <= cap);
    if (ctx && !list.includes(ctx)) list.push(ctx);
    return list.sort((a, b) => a - b).map((c) => ({ value: c, fits: c <= fit.max_context || c === ctx }));
  });

  // The big speed number springs to each new value.
  const speed = new Tween(0, { duration: 900, easing: backOut });
  $effect(() => {
    speed.target = lastStats?.tps ?? 0;
  });

  $effect(() => {
    if (app.online) api.library().then((m) => (models = m)).catch(() => {});
  });
  $effect(() => setAccent(model?.repo));
  // A first message typed during setup goes out as soon as there's a model to send it to.
  $effect(() => {
    if (app.queued && selectedId) {
      const text = app.queued;
      app.queued = '';
      send(text);
    }
  });

  const pretty = (repo) => repo?.split('/').pop() ?? '';
  const size = (text) => (text.length <= 60 ? 'xl' : text.length <= 140 ? 'l' : 'm');

  function toBottom(smooth = false) {
    requestAnimationFrame(() => scroller?.scrollTo({ top: scroller.scrollHeight, behavior: smooth ? 'smooth' : 'instant' }));
  }
  // Only scrolling up means "I'm reading back"; our own smooth scroll down mid-animation isn't far enough
  // from the bottom yet and must not turn following off.
  let lastTop = 0;
  function onScroll() {
    const top = scroller.scrollTop;
    if (scroller.scrollHeight - top - scroller.clientHeight < 80) stick = true;
    else if (top < lastTop - 2) stick = false;
    lastTop = top;
  }

  async function load(id, context) {
    note = '';
    const t = thread ?? newThread(id);
    t.model = id;
    persist();
    if (engine?.model !== id || (context && context !== engine?.context)) {
      try { await api.load(id, context); } catch (e) { note = e.message; }
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
    t.messages.push({ role: 'assistant', content: '', reasoning: '', stats: null, error: null, started: Date.now() });
    const msg = t.messages[t.messages.length - 1];
    const history = t.messages.slice(0, -1).map(({ role, content }) => ({ role, content }));
    input = '';
    note = '';
    busy = true;
    stick = true;
    ctrl = new AbortController();
    toBottom(true);
    const t0 = performance.now();
    let first = 0, last = 0, n = 0, usage = null;
    try {
      for await (const d of chatStream(t.model, history, ctrl.signal)) {
        if (d.usage) { usage = d.usage; continue; }
        last = performance.now();
        first ||= last;
        n++;
        if (d.content && !msg.content && msg.reasoning) msg.thoughtMs = Date.now() - msg.started;
        msg.content += d.content;
        msg.reasoning += d.reasoning;
        if (stick) toBottom();
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

  // Clicks inside rendered answers: copy buttons on code, and links open in the browser, not the app.
  function proseClick(e) {
    const copy = e.target.closest('[data-copy]');
    if (copy) {
      navigator.clipboard.writeText(copy.closest('figure').querySelector('pre').innerText);
      copy.textContent = 'Copied';
      setTimeout(() => (copy.textContent = 'Copy'), 1400);
      return;
    }
    const a = e.target.closest('a[href]');
    if (a) {
      e.preventDefault();
      openExternal(a.href);
    }
  }

  function keydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }
  function globalKey(e) {
    if (app.screen !== 'chat') return;
    if (e.key === 'Escape' && busy) ctrl?.abort();
    else if (e.ctrlKey && e.key.toLowerCase() === 'n') { e.preventDefault(); newThread(selectedId); composer?.focus(); }
    else if (e.key === '/' && document.activeElement?.tagName !== 'TEXTAREA' && document.activeElement?.tagName !== 'INPUT') {
      e.preventDefault();
      composer?.focus();
    }
  }
</script>

<svelte:window onkeydown={globalKey} />

<div class="chat">
  <main bind:this={scroller} onscroll={onScroll}>
    <div class="layout">
      <section class="turns" aria-label="Conversation" onclick={proseClick} role="presentation">
        {#if !turns.length}
          <div in:fly={{ y: 24, duration: 700, easing: backOut }}>
            <h1 class="display empty">Ask <span class="highlight">anything.</span><br />It's all on your GPU.</h1>
            <div class="starters">
              {#each ['Explain this error message', 'Write a PowerShell script that', 'Review my Python function'] as s, i}
                <button class="pill quiet" in:scale={{ start: 0.6, delay: 150 + i * 70, duration: 600, easing: elasticOut }}
                  onclick={() => { input = s + ' '; composer?.focus(); }}>{s}</button>
              {/each}
            </div>
          </div>
        {/if}
        {#each turns as turn (turn.index)}
          {@const a = turn.answer}
          {@const live = busy && turn === turns.at(-1)}
          <article class="turn" in:fly={{ y: 32, duration: 650, easing: backOut }}>
            <span class="n num muted">{String(turn.n).padStart(2, '0')}</span>
            <h2 class="display prompt {size(turn.user.content)}">{turn.user.content}</h2>
            <div class="answer">
              {#if live && !a?.content && !a?.error}
                {#if warming}
                  <Thinking label="Warming up the engine" since={engine.since * 1000} warming />
                {:else}
                  <Thinking label={a?.reasoning ? 'Thinking' : 'Starting'} since={a?.started ?? Date.now()} peek={a?.reasoning ?? ''} />
                {/if}
              {:else if a?.reasoning}
                <details class="thought">
                  <summary class="muted">Thought{a.thoughtMs ? ` for ${Math.max(1, Math.round(a.thoughtMs / 1000))}s` : ''}</summary>
                  <p>{a.reasoning}</p>
                </details>
              {/if}
              {#if a?.content}
                <div class="prose">{@html render(a.content)}</div>
                {#if live}<span class="caret" aria-hidden="true"></span>{/if}
              {/if}
              {#if a?.error}<p class="error" in:scale={{ start: 0.9, duration: 400, easing: backOut }}>{a.error}</p>{/if}
              {#if a?.stats || a?.error}
                <div class="actions" in:fly={{ y: 8, duration: 400, easing: backOut }}>
                  {#if a?.stats}
                    <span class="num muted stamp">{a.stats.tps ?? '—'} tok/s · {a.stats.ttft} ms first token · {a.stats.tokens} tokens</span>
                  {/if}
                  <button class="pill" onclick={() => navigator.clipboard.writeText(a.content)}>Copy</button>
                  <button class="pill" disabled={busy} onclick={() => retry(turn)}>Retry</button>
                </div>
              {/if}
            </div>
          </article>
        {/each}
      </section>

      <aside aria-label="Speed">
        {#if models.length}
          {#if warming}
            <Thinking label="Warming up" since={engine.since * 1000} warming peek={engine.log_tail?.at(-1) ?? ''} />
          {:else if loadedHere && engine?.state === 'error'}
            <p class="error">The model stopped. {engine.error?.split('\n')[0]}</p>
            <button class="pill" onclick={() => load(selectedId)}>Load again</button>
          {:else if !loadedHere}
            <p class="status muted">Not loaded. Sending a message loads it, or <button class="link" onclick={() => load(selectedId)}>load now</button>.</p>
          {/if}
          {#if lastStats}
            <div class="display speed num" aria-label="{lastStats.tps} tokens a second">{Math.round(speed.current)}</div>
            <span class="highlight speed-label">tokens a second</span>
            <p class="num muted detail">{lastStats.ttft} ms to first token<br />{lastStats.tokens} tokens · {lastStats.secs} s</p>
          {:else}
            <span class="highlight speed-label">Speed shows after an answer</span>
          {/if}
        {:else}
          <p class="status">No models yet.</p>
          <button class="pill primary" onclick={() => (app.screen = 'models')}>Get a model</button>
        {/if}
        {#if note}<p class="error">{note}</p>{/if}
      </aside>
    </div>
    {#if !stick && turns.length}
      <button class="jump pill primary" onclick={() => { stick = true; toBottom(true); }} transition:scale={{ start: 0.7, duration: 350, easing: backOut }}>Jump to latest</button>
    {/if}
  </main>

  <form class="composer" onsubmit={(e) => { e.preventDefault(); send(input); }}>
    <label class="sr-only" for="ask">Message</label>
    <textarea id="ask" bind:this={composer} rows={Math.min(6, input.split('\n').length)} bind:value={input} onkeydown={keydown}
      placeholder="Ask anything. It's all on your GPU."></textarea>
    {#if busy}
      <button type="button" class="go stop" onclick={() => ctrl?.abort()} in:scale={{ start: 0.7, duration: 400, easing: backOut }}>Stop</button>
    {:else}
      <button type="submit" class="go display" disabled={!input.trim() || !models.length} in:scale={{ start: 0.7, duration: 400, easing: backOut }}>Go</button>
    {/if}
  </form>

  {#if model}
    <footer class="bar-strip">
      <label class="ctl">
        <span class="sr-only">Model</span>
        <span class="swatch" aria-hidden="true"></span>
        <select value={selectedId} onchange={(e) => load(e.currentTarget.value)} disabled={busy || warming}>
          {#each models as m (m.id)}<option value={m.id}>{pretty(m.repo)}</option>{/each}
        </select>
      </label>
      <label class="ctl">
        <span class="sr-only">Context</span>
        <select value={ctx} onchange={(e) => load(selectedId, Number(e.currentTarget.value))} disabled={busy || warming}>
          {#each ctxOptions as o (o.value)}
            <option value={o.value} disabled={!o.fits}>{kTokens(o.value)} context{o.fits ? '' : ' (won’t fit)'}</option>
          {:else}
            <option value={undefined}>Not enough free VRAM</option>
          {/each}
        </select>
      </label>
      <span>{familyName(model.repo)}</span>
      <span>{model.quant}</span>
      <span class="num">Weights {gb(model.weights_bytes)}</span>
      <span>TensorRT-LLM 1.3</span>
      <span class="tail">Local · nothing leaves this PC</span>
    </footer>
  {/if}
</div>

<style>
  .chat { height: 100%; display: grid; grid-template-rows: minmax(0, 1fr) auto auto; }
  main { overflow-y: auto; position: relative; scrollbar-gutter: stable; }
  .layout {
    max-width: 1120px; margin: 0 auto; padding: var(--sp-7) var(--sp-6) var(--sp-5);
    display: flex; flex-wrap: wrap; gap: var(--sp-7); align-items: flex-start;
  }
  .turns { flex: 999 1 560px; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-7); }
  .empty { margin: 0; font-size: 64px; line-height: 0.98; }
  .starters { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-top: var(--sp-5); }
  .turn { display: grid; grid-template-columns: 24px minmax(0, 1fr); column-gap: var(--sp-4); row-gap: var(--sp-5); }
  .n { font-size: 13px; font-weight: 700; padding-top: var(--sp-3); }
  .prompt { margin: 0; line-height: 1; white-space: pre-wrap; overflow-wrap: anywhere; }
  .prompt.xl { font-size: 64px; }
  .prompt.l { font-size: 40px; line-height: 1.05; }
  .prompt.m { font-size: 22px; line-height: 1.35; font-weight: 700; letter-spacing: -0.01em; }
  .answer { grid-column: 2; display: flex; flex-direction: column; gap: var(--sp-4); max-width: 700px; min-width: 0; }
  .thought summary { cursor: pointer; font-size: 14px; }
  .thought p { white-space: pre-wrap; font-size: 14px; margin: var(--sp-2) 0 0; color: var(--muted); }
  .caret {
    display: block; width: 10px; height: 18px; margin-top: calc(-1 * var(--sp-3)); border-radius: 2px;
    background: var(--accent); animation: blink 0.9s steps(2) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  .actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
  .stamp { font-size: 12px; margin-right: auto; }
  .error { color: var(--ink); background: var(--surface-2); padding: var(--sp-3) var(--sp-4); border-radius: var(--r-sm); margin: 0; font-size: 14px; }

  aside { flex: 1 1 240px; min-width: 0; max-width: 100%; display: flex; flex-direction: column; gap: var(--sp-4); align-items: flex-start; position: sticky; top: 0; }
  .status { margin: 0; font-size: 14px; }
  .link { border: none; background: none; padding: 0; text-decoration: underline; cursor: pointer; font-size: inherit; }
  .speed { font-size: 128px; line-height: 0.85; letter-spacing: -0.06em; padding-top: var(--sp-4); }
  .speed-label { font-size: 15px; font-weight: 600; padding: var(--sp-1) var(--sp-3); }
  .detail { margin: 0; font-size: 13px; line-height: 1.8; }
  .jump { position: sticky; bottom: var(--sp-4); margin: 0 auto var(--sp-4); display: flex; box-shadow: 0 8px 24px rgb(0 0 0 / 0.18); }

  .composer {
    width: 100%; max-width: 1120px; margin: 0 auto; padding: var(--sp-3) var(--sp-6) var(--sp-4);
    display: flex; align-items: flex-end; gap: var(--sp-2);
  }
  .composer textarea {
    flex: 1; min-width: 0; resize: none; min-height: var(--control-lg); max-height: 200px;
    padding: var(--sp-4) var(--sp-5); border: 1.5px solid var(--line); border-radius: 28px;
    background: var(--surface); font-size: 18px; line-height: 1.35; outline: none;
    transition: box-shadow 0.3s var(--ease);
  }
  .composer textarea:focus { box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 45%, transparent); }
  .go {
    height: var(--control-lg); padding: 0 var(--sp-6); border-radius: var(--r-pill); border: none; cursor: pointer;
    background: var(--accent); color: var(--on-accent); font-size: 18px; font-weight: 800;
    transition: transform 0.45s var(--spring), opacity 0.2s;
  }
  .go:hover:not(:disabled) { transform: translateY(-2px) scale(1.03); }
  .go:active:not(:disabled) { transform: scale(0.9); transition-duration: 0.08s; }
  .go:disabled { opacity: 0.45; cursor: default; }
  .go.stop { background: var(--ink); color: var(--bg); font-family: var(--text); font-weight: 700; font-size: 15px; }

  .bar-strip {
    background: var(--strip-bg); color: var(--strip-ink); padding: var(--sp-2) var(--sp-6); display: flex;
    flex-wrap: wrap; align-items: center; gap: var(--sp-2) var(--sp-5); font-size: 12px; font-weight: 600;
    letter-spacing: 0.06em; text-transform: uppercase;
  }
  .ctl {
    display: flex; align-items: center; gap: var(--sp-2); height: 36px; padding: 0 var(--sp-2) 0 var(--sp-3);
    border-radius: var(--r-pill); background: rgb(255 255 255 / 0.08); transition: background-color 0.2s;
  }
  .ctl:hover { background: rgb(255 255 255 / 0.14); }
  .ctl select {
    border: none; background: transparent; color: inherit; font: inherit; letter-spacing: inherit; text-transform: inherit;
    cursor: pointer; height: 100%; max-width: 280px;
  }
  .ctl select option { background: var(--surface); color: var(--ink); text-transform: none; }
  .swatch { width: 10px; height: 10px; flex: none; border-radius: var(--r-pill); background: var(--accent); }
  .tail { margin-left: auto; }
</style>
