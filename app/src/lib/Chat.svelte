<script>
  import { fly, fade } from 'svelte/transition';
  import { backOut, cubicOut } from 'svelte/easing';
  import { Tween } from 'svelte/motion';
  import { app, persist, newThread, currentThread } from './app.svelte.js';
  import { api, chatStream, familyName, gb, kTokens, setAccent } from './api.js';
  import { render } from './markdown.js';
  import { openExternal } from './shell.js';
  import { systemPrompt } from './skills.js';
  import Thinking from './Thinking.svelte';

  const CONTEXTS = [4096, 8192, 16384, 32768, 65536, 131072, 262144];
  const FIRST_LOAD_S = 111; // measured cold load on an RTX 5070 Ti; drives the dock's progress line

  let models = $state([]); // GET /manage/library
  let input = $state('');
  let busy = $state(false);
  let note = $state('');
  let stick = $state(true); // follow new tokens only while the reader is at the bottom
  let now = $state(Date.now());
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
  const warmFrac = $derived(warming ? Math.min(0.95, (now / 1000 - engine.since) / FIRST_LOAD_S) : 0);
  const greeting = $derived.by(() => {
    const d = new Date(now);
    const h = d.getHours();
    const part = h < 5 ? 'night' : h < 12 ? 'morning' : h < 17 ? 'afternoon' : h < 22 ? 'evening' : 'night';
    return d.toLocaleDateString('en', { weekday: 'long' }).toLowerCase() + ' ' + part;
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
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 500);
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
      const messages = [{ role: 'system', content: systemPrompt(history) }, ...history];
      for await (const d of chatStream(t.model, messages, ctrl.signal)) {
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
      copy.textContent = 'copied';
      setTimeout(() => (copy.textContent = 'copy'), 1400);
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
    else if (e.key === '/' && !['TEXTAREA', 'INPUT', 'SELECT'].includes(document.activeElement?.tagName)) {
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
          <div class="empty" in:fly={{ y: 24, duration: 800, easing: backOut }}>
            <span class="eyebrow">{greeting}</span>
            <h1 class="display">ask <span class="highlight">anything.</span></h1>
            <p class="lede">It runs on your GPU, at full TensorRT speed, and nothing leaves this PC.</p>
            <div class="starters">
              {#each ['explain this error message', 'write a powershell script that', 'review my python function'] as s, i}
                <button class="pill" in:fly={{ y: 12, delay: 250 + i * 80, duration: 600, easing: backOut }}
                  onclick={() => { input = s.charAt(0).toUpperCase() + s.slice(1) + ' '; composer?.focus(); }}>{s}</button>
              {/each}
            </div>
          </div>
        {/if}
        {#each turns as turn (turn.index)}
          {@const a = turn.answer}
          {@const live = busy && turn === turns.at(-1)}
          <article class="turn" in:fly={{ y: 32, duration: 650, easing: backOut }}>
            <span class="eyebrow num">{String(turn.n).padStart(2, '0')}</span>
            <h2 class="prompt {size(turn.user.content)}">{turn.user.content}</h2>
            <div class="answer">
              {#if live && !a?.content && !a?.error}
                {#if warming}
                  <Thinking label="warming up the engine" since={engine.since * 1000} warming />
                {:else}
                  <Thinking label={a?.reasoning ? 'thinking' : 'starting'} since={a?.started ?? Date.now()} peek={a?.reasoning ?? ''} />
                {/if}
              {:else if a?.reasoning}
                <details class="thought">
                  <summary class="eyebrow">thought{a.thoughtMs ? ` for ${Math.max(1, Math.round(a.thoughtMs / 1000))}s` : ''}</summary>
                  <p>{a.reasoning}</p>
                </details>
              {/if}
              {#if a?.content}
                <div class="prose">{@html render(a.content)}</div>
                {#if live}<span class="caret" aria-hidden="true"></span>{/if}
              {/if}
              {#if a?.error}<p class="error" in:fade>{a.error}</p>{/if}
              {#if a?.stats || a?.error}
                <div class="actions" in:fade={{ duration: 400 }}>
                  {#if a?.stats}
                    <span class="eyebrow num stamp">{a.stats.tps ?? '—'} tok/s · {a.stats.ttft} ms first token · {a.stats.tokens} tokens</span>
                  {/if}
                  <button class="pill quiet" onclick={() => navigator.clipboard.writeText(a.content)}>copy</button>
                  <button class="pill quiet" disabled={busy} onclick={() => retry(turn)}>retry</button>
                </div>
              {/if}
            </div>
          </article>
        {/each}
      </section>

      <aside aria-label="Speed">
        {#if lastStats}
          <span class="eyebrow">last answer</span>
          <div class="display speed num" aria-label="{lastStats.tps} tokens a second">{Math.round(speed.current)}</div>
          <span class="speed-label">tokens a second</span>
          <p class="eyebrow num detail">{lastStats.ttft} ms to first token<br />{lastStats.tokens} tokens in {lastStats.secs} s</p>
        {:else if models.length}
          <span class="eyebrow">speed shows after an answer</span>
        {:else}
          <span class="eyebrow">no models yet</span>
          <button class="pill primary" onclick={() => (app.screen = 'models')}>get a model</button>
        {/if}
        {#if note}<p class="error">{note}</p>{/if}
      </aside>
    </div>
    {#if !stick && turns.length}
      <button class="jump" onclick={() => { stick = true; toBottom(true); }} transition:fly={{ y: 16, duration: 350, easing: backOut }}>jump to latest ↓</button>
    {/if}
  </main>

  <form class="composer" onsubmit={(e) => { e.preventDefault(); send(input); }}>
    <label class="sr-only" for="ask">Message</label>
    <textarea id="ask" bind:this={composer} rows={Math.min(6, input.split('\n').length)} bind:value={input} onkeydown={keydown}
      placeholder="Ask anything"></textarea>
    {#if busy}
      <button type="button" class="send" onclick={() => ctrl?.abort()} in:fade={{ duration: 200 }}>stop</button>
    {:else}
      <button type="submit" class="send go" disabled={!input.trim() || !models.length} in:fade={{ duration: 200 }}>go</button>
    {/if}
  </form>

  {#if model}
    <footer class="dock">
      <span class="fill" class:sweep={busy && !warming} style="transform: scaleX({warming ? warmFrac : 0})"></span>
      <div class="now">
        <span class="badge num" aria-hidden="true">{ctx ? kTokens(ctx).toLowerCase() : '—'}</span>
        <div class="title">
          <label class="model">
            <span class="sr-only">Model</span>
            <select value={selectedId} onchange={(e) => load(e.currentTarget.value)} disabled={busy || warming}>
              {#each models as m (m.id)}<option value={m.id}>{pretty(m.repo)}</option>{/each}
            </select>
          </label>
          <span class="eyebrow sub">{familyName(model.repo).toLowerCase()} · {model.quant.toLowerCase()} · {gb(model.weights_bytes).toLowerCase()}</span>
        </div>
      </div>
      <div class="state" aria-live="polite">
        {#if warming}
          <span>warming up · {Math.max(0, Math.round(now / 1000 - engine.since))}s</span>
        {:else if loadedHere && engine?.state === 'error'}
          <button class="pill primary" onclick={() => load(selectedId)}>the model stopped, load again</button>
        {:else if busy}
          <span class="accent">answering</span>
        {:else if loadedHere && engine?.state === 'ready'}
          <span>ready{lastStats ? ` · ${lastStats.tps} tok/s` : ''}</span>
        {:else}
          <button class="pill" onclick={() => load(selectedId)}>not loaded, load now</button>
        {/if}
      </div>
      <div class="right">
        <label class="ctxsel">
          <span class="sr-only">Context</span>
          <select value={ctx} onchange={(e) => load(selectedId, Number(e.currentTarget.value))} disabled={busy || warming}>
            {#each ctxOptions as o (o.value)}
              <option value={o.value} disabled={!o.fits}>{kTokens(o.value).toLowerCase()} context{o.fits ? '' : ' (won’t fit)'}</option>
            {:else}
              <option value={undefined}>not enough free vram</option>
            {/each}
          </select>
        </label>
        <span class="eyebrow local">local · nothing leaves this pc</span>
      </div>
    </footer>
  {/if}
</div>

<style>
  .chat { height: 100%; display: grid; grid-template-rows: minmax(0, 1fr) auto auto; }
  main { overflow-y: auto; position: relative; scrollbar-gutter: stable; }
  .layout {
    max-width: 1240px; margin: 0 auto; padding: var(--sp-7) var(--pad) var(--sp-5);
    display: flex; flex-wrap: wrap; gap: var(--sp-7); align-items: flex-start;
  }
  .turns { flex: 999 1 560px; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-8); }

  .empty { display: flex; flex-direction: column; gap: var(--sp-4); padding-top: var(--sp-6); }
  .empty h1 { margin: 0; font-size: clamp(72px, 11vw, 160px); }
  .lede { margin: var(--sp-3) 0 0; font-size: 20px; line-height: 1.5; max-width: 520px; }
  .starters { display: flex; flex-wrap: wrap; gap: 0 var(--sp-5); }

  .turn { display: grid; grid-template-columns: 32px minmax(0, 1fr); column-gap: var(--sp-4); row-gap: var(--sp-5); }
  .turn > .eyebrow { padding-top: var(--sp-3); }
  .prompt { margin: 0; font-weight: 300; letter-spacing: -0.03em; white-space: pre-wrap; overflow-wrap: anywhere; }
  .prompt.xl { font-size: 56px; line-height: 1; }
  .prompt.l { font-size: 36px; line-height: 1.1; }
  .prompt.m { font-size: 22px; line-height: 1.4; letter-spacing: -0.01em; }
  .answer { grid-column: 2; display: flex; flex-direction: column; gap: var(--sp-4); max-width: 720px; min-width: 0; }
  .thought summary { cursor: pointer; }
  .thought p { white-space: pre-wrap; font-size: 15px; margin: var(--sp-2) 0 0; color: var(--ink-2); }
  .caret { display: block; width: 10px; height: 2px; margin-top: calc(-1 * var(--sp-2)); background: var(--accent); animation: blink 0.9s steps(2) infinite; }
  @keyframes blink { 50% { opacity: 0; } }
  .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 0 var(--sp-5); }
  .stamp { margin-right: auto; }
  .error { margin: 0; padding: var(--sp-3) var(--sp-4); border-radius: var(--r); background: var(--surface); font-size: 15px; }

  aside { flex: 1 1 240px; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-2); align-items: flex-start; position: sticky; top: 0; }
  .speed { font-size: 144px; padding-top: var(--sp-2); }
  .speed-label { font-size: 20px; font-weight: 300; color: var(--accent); }
  .detail { margin: var(--sp-3) 0 0; line-height: 1.8; }
  .jump {
    position: sticky; bottom: var(--sp-4); display: block; margin: 0 auto var(--sp-4); padding: var(--sp-2) var(--sp-4);
    border-radius: var(--r); background: var(--surface); color: var(--accent); font-weight: 300;
  }

  .composer { width: 100%; max-width: 1240px; margin: 0 auto; padding: var(--sp-3) var(--pad) var(--sp-4); display: flex; align-items: flex-end; gap: var(--sp-5); }
  .composer textarea {
    flex: 1; min-width: 0; resize: none; min-height: 56px; max-height: 220px; padding: var(--sp-4) var(--sp-5);
    border: 0; border-radius: var(--r); background: var(--surface); font-size: 18px; line-height: 1.4; outline: none;
    transition: background-color 0.2s var(--ease), box-shadow 0.3s var(--ease);
  }
  .composer textarea:focus { box-shadow: inset 0 -2px 0 var(--accent); }
  .send { min-height: 56px; font-size: 28px; font-weight: 300; color: var(--ink); transition: color 0.2s, opacity 0.2s; }
  .send.go { color: var(--accent); }
  .send:disabled { opacity: 0.3; cursor: default; }

  .dock {
    position: relative; height: var(--dock); padding: 0 var(--pad); display: grid; gap: var(--sp-4);
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; background: var(--canvas);
  }
  .dock::before { content: ''; position: absolute; left: 0; right: 0; top: 0; height: 1px; background: var(--line); }
  .fill { position: absolute; left: 0; top: -1px; height: 2px; width: 100%; background: var(--accent); transform-origin: left center; transition: transform 1s var(--ease); }
  .fill.sweep { animation: sweep 1.4s var(--ease) infinite; }
  @keyframes sweep { 0% { transform: scaleX(0); transform-origin: left; } 50% { transform: scaleX(1); transform-origin: left; } 51% { transform-origin: right; } 100% { transform: scaleX(0); transform-origin: right; } }
  .now { display: flex; align-items: center; gap: var(--sp-3); min-width: 0; }
  .badge {
    width: 44px; height: 44px; flex: none; display: grid; place-items: center; border-radius: var(--r-sm);
    background: var(--accent); color: var(--on-accent); font-size: 14px; font-weight: 500; transition: background-color 0.6s var(--ease);
  }
  .title { display: flex; flex-direction: column; min-width: 0; }
  .model select, .ctxsel select {
    border: 0; background: transparent; color: var(--ink); font-size: 16px; font-weight: 500; cursor: pointer; padding: 0; max-width: 100%;
    appearance: none; -webkit-appearance: none; text-overflow: ellipsis;
  }
  .model select option, .ctxsel select option { background: var(--surface); color: var(--ink); }
  .sub { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .state { font-size: 15px; font-weight: 300; color: var(--ink-2); text-align: center; }
  .state .pill { font-size: 15px; }
  .right { display: flex; flex-direction: column; align-items: flex-end; min-width: 0; }
  .ctxsel select { color: var(--accent); font-weight: 400; text-align: right; }
  .local { font-size: 13px; }
</style>
