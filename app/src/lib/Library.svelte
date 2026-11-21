<script>
  import { app, newThread } from './app.svelte.js';
  import { api, accentOf, familyName, gb, kTokens, setAccent } from './api.js';

  let tab = $state('picks');
  let catalog = $state(null);
  let library = $state([]);
  let results = $state(null);
  let query = $state('');
  let searching = $state(false);
  let selRepo = $state(null);
  let detail = $state(null);
  let variants = $state([]);
  let ctx = $state(null);
  let busy = $state(false);
  let error = $state('');
  let confirmDelete = $state(false);

  const engine = $derived(app.status?.engine);
  const gpu = $derived(app.status?.gpu);
  const downloads = $derived(app.status?.downloads ?? []);

  $effect(() => setAccent(selRepo ?? engine?.repo));

  const refreshLibrary = () => api.library().then((l) => (library = l)).catch(() => {});
  api.catalog().then((c) => (catalog = c)).catch((e) => (error = e.message));
  refreshLibrary();
  // Re-read the library whenever a download finishes.
  const finished = $derived(downloads.filter((d) => d.status === 'done').length);
  $effect(() => {
    finished;
    refreshLibrary();
  });

  const localOf = (repo) => library.find((l) => l.repo === repo);
  const jobOf = (repo) => downloads.find((d) => d.repo === repo && d.status === 'downloading');

  function card(x) {
    const local = localOf(x.repo);
    const job = jobOf(x.repo);
    const fit = local?.fit ?? x.fit;
    const launch = local?.launch ?? x.launch ?? {};
    const runnable = x.ok ?? x.status !== 'no';
    const size = local?.download_bytes ?? x.download_bytes ?? x.approx_bytes;
    let status;
    if (local && engine?.model === local.id && engine.state !== 'error') status = engine.state === 'ready' ? 'Loaded and ready' : 'Warming up';
    else if (local) status = 'On this PC';
    else if (job) status = `${gb(job.bytes)} of ${gb(job.total)}`;
    else if (!runnable) status = x.reason?.startsWith('Too large') ? 'Too big for this GPU' : 'Won’t run';
    else if (fit && !fit.fits) status = 'Needs more GPU memory';
    else status = size ? 'Get · ' + gb(size) : 'Check';
    const badges = [];
    if (launch.tools) badges.push('Tools');
    if (launch.reasoning) badges.push('Thinking');
    if (launch.tools && fit?.hermes_ready) badges.push('Hermes Agent');
    return {
      repo: x.repo, quant: x.quant ?? local?.quant ?? '?', status, badges, job, runnable,
      meta: fit?.fits ? `${size ? gb(size) + ' · ' : ''}up to ${kTokens(fit.max_context)} context` : (size ? gb(size) : x.repo),
      faded: !runnable || (fit && !fit.fits && !local),
    };
  }

  const items = $derived.by(() => {
    if (tab === 'picks') return (catalog ?? []).map(card);
    if (tab === 'pc') {
      const jobs = downloads.filter((d) => d.status === 'downloading' && !localOf(d.repo)).map((d) => ({ repo: d.repo, download_bytes: d.total, ok: true }));
      return [...library, ...jobs].map(card);
    }
    return (results ?? []).map(card);
  });

  async function select(repo) {
    selRepo = repo;
    detail = null;
    variants = [];
    ctx = null;
    confirmDelete = false;
    error = '';
    const local = localOf(repo);
    const fromCatalog = catalog?.find((c) => c.repo === repo);
    try {
      detail = local ? { ...fromCatalog, ...local, ok: true } : fromCatalog ?? (await api.inspect(repo));
      if (selRepo !== repo) return;
      if (detail.base && detail.base !== repo) variants = (await api.variants(detail.base)).filter((v) => v.repo !== repo).slice(0, 8);
    } catch (e) {
      error = e.message;
    }
  }

  async function search(e) {
    e.preventDefault();
    if (!query.trim()) return;
    tab = 'search';
    searching = true;
    try { results = await api.search(query.trim()); } catch (err) { error = err.message; results = []; }
    searching = false;
  }

  async function act(fn) {
    busy = true;
    error = '';
    try { await fn(); } catch (e) { error = e.message; }
    busy = false;
    await refreshLibrary();
  }

  // Selected model: memory bar and context slider.
  const d = $derived.by(() => {
    if (!detail) return null;
    const local = localOf(detail.repo);
    const fit = local?.fit ?? detail.fit;
    const c = card(detail);
    const loaded = local && engine?.model === local.id;
    if (!fit || !gpu?.total_mib) return { c, local, loaded, fit: null };
    const value = ctx ?? (loaded ? engine.context : fit.default_context) ?? 4096;
    const total = gpu.total_mib * 2 ** 20;
    const kv = value * fit.kv_bytes_per_token;
    const spare = fit.budget_bytes - fit.weights_bytes - fit.overhead_bytes - kv;
    const pct = (b) => Math.max(0, Math.min(100, (100 * b) / total)).toFixed(2) + '%';
    const segs = [
      ['Windows and apps', total - fit.budget_bytes, 'var(--muted)'],
      ['Model weights', fit.weights_bytes, 'var(--ink)'],
      ['Context memory', kv, 'var(--accent)'],
      ['Engine', fit.overhead_bytes, 'var(--line-soft)'],
    ].map(([label, bytes, fill]) => ({ label, bytes, fill, width: pct(bytes) }));
    const max = Math.max(8192, Math.min(fit.model_max ?? fit.max_context, 131072));
    let hermes = 'Ready for Hermes Agent at this context.';
    const tools = (local?.launch ?? detail.launch)?.tools;
    if (!tools) hermes = 'No tool calling, so Hermes Agent can’t use it. Fine for chat.';
    else if (value < 64000) hermes = (fit.model_max ?? 0) < 64000
      ? `Hermes Agent needs 64K of context. This model tops out at ${kTokens(fit.model_max)}.`
      : 'Hermes Agent needs 64K of context. Slide past the marker.';
    else if (spare < 0) hermes = 'Hermes Agent would need this to fit first.';
    return {
      c, local, loaded, fit, value, max, segs, spare, hermes,
      marker: max > 64000 ? (((64000 - 4096) / (max - 4096)) * 100).toFixed(1) + '%' : null,
    };
  });

  function chatWith(id) {
    newThread(id);
    app.screen = 'chat';
  }
</script>

<main>
  <section class="list" aria-label="Models">
    <div class="top">
      <div class="title">
        <h1 class="display">Models</h1>
        {#if gpu?.name}
          <p class="muted">{gpu.name.replace('NVIDIA GeForce ', '')} · {gb(gpu.total_mib * 2 ** 20)}. {gb(gpu.used_mib * 2 ** 20)} in use right now{engine?.model ? ', including the loaded model' : ''}.</p>
        {/if}
      </div>
      <form class="search" onsubmit={search}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4-4"></path></svg>
        <label class="sr-only" for="hf-q">Search Hugging Face</label>
        <input id="hf-q" bind:value={query} placeholder="Search Hugging Face" />
      </form>
    </div>

    <div class="tabs" role="tablist" aria-label="Which models">
      {#each [['picks', 'Picked for your GPU'], ['pc', `On this PC (${library.length})`], ['search', results ? `Search: ${query}` : 'Search']] as [key, label]}
        <button role="tab" class="pill" class:quiet={tab !== key} class:active={tab === key} aria-selected={tab === key} onclick={() => (tab = key)}>{label}</button>
      {/each}
    </div>

    {#if tab === 'picks' && !catalog}
      <p class="muted">Checking the catalog against your GPU…</p>
    {:else if tab === 'search' && searching}
      <p class="muted">Searching Hugging Face…</p>
    {:else if tab === 'search' && !results}
      <p class="muted">Search for a model by name, like “qwen3 8b”. Results that can’t run on TensorRT-LLM say why.</p>
    {:else if !items.length}
      <p class="muted">{tab === 'pc' ? 'No models downloaded yet. Pick one above.' : 'Nothing found.'}</p>
    {/if}

    <div class="grid">
      {#each items as c (c.repo)}
        <button class="model card" class:selected={c.repo === selRepo} aria-pressed={c.repo === selRepo} onclick={() => select(c.repo)}>
          <span class="band" class:faded={c.faded} style="background: {accentOf(c.repo)}">
            <span class="fam">{familyName(c.repo)}</span>
            <span class="display quant">{c.quant}</span>
          </span>
          <span class="body">
            <span class="name">{c.repo.split('/').pop()}</span>
            <span class="mono muted meta">{c.meta}</span>
            {#if c.badges.length}
              <span class="badges">{#each c.badges as b}<span>{b}</span>{/each}</span>
            {/if}
            {#if c.job}
              <span class="bar"><span style="width: {(100 * c.job.bytes) / c.job.total}%"></span></span>
            {/if}
            <span class="status" class:muted={c.faded}>{c.status}</span>
          </span>
        </button>
      {/each}
    </div>
  </section>

  <aside aria-label="Selected model">
    {#if !selRepo}
      <div class="card hint"><p class="muted">Pick a model to see how much of your GPU it needs and what it can do.</p></div>
    {:else if !d}
      <div class="card hint"><p class="muted">{error || 'Reading the model’s config from Hugging Face…'}</p></div>
    {:else}
      <div class="card panel">
        <div class="head" style="background: {accentOf(detail.repo)}">
          <span class="fam">{familyName(detail.repo)} · {d.c.quant}</span>
          <h2 class="display">{detail.repo.split('/').pop()}</h2>
          <span class="mono repo">{detail.repo}</span>
        </div>
        <div class="inner">
          {#if !d.c.runnable}
            <p class="reason">{detail.reason}</p>
          {:else if d.fit}
            <div class="ctx">
              <div class="ctx-top">
                <label for="ctx">Context</label>
                <span class="display ctx-value">{kTokens(d.value)}</span>
              </div>
              <div class="slider">
                <input id="ctx" type="range" min="4096" max={d.max} step="1024" value={d.value} oninput={(e) => (ctx = Number(e.currentTarget.value))} />
                {#if d.marker}<span class="marker mono muted" style="left: {d.marker}">Hermes 64K</span>{/if}
              </div>
            </div>
            <div class="mem">
              <div class="membar" role="img" aria-label="GPU memory with this context">
                {#each d.segs as s}<span style="width: {s.width}; background: {s.fill}"></span>{/each}
              </div>
              <div class="legend">
                {#each d.segs as s}
                  <span><i style="background: {s.fill}" aria-hidden="true"></i>{s.label}<b class="mono">{gb(s.bytes)}</b></span>
                {/each}
              </div>
              <p class="verdict">{d.spare >= 0 ? `Fits, with ${gb(d.spare)} to spare.` : `Needs ${gb(-d.spare)} more. Lower the context or close other GPU apps.`}</p>
              <p class="muted small">{d.hermes}</p>
            </div>
          {/if}
          {#if detail.warning}<p class="warn">{detail.warning}</p>{/if}
          {#if error}<p class="warn">{error}</p>{/if}

          <div class="row">
            {#if d.loaded}
              <button class="pill primary" onclick={() => chatWith(d.local.id)}>Chat with it</button>
              <button class="pill" disabled={busy} onclick={() => act(api.unload)}>Unload</button>
            {:else if d.local}
              <button class="pill primary" disabled={busy || d.spare < 0} onclick={() => act(async () => { await api.load(d.local.id, d.value); chatWith(d.local.id); })}>Load</button>
              {#if confirmDelete}
                <button class="pill" disabled={busy} onclick={() => act(async () => { await api.deleteModel(d.local.id); selRepo = null; })}>Delete {gb(d.local.download_bytes)} from this PC</button>
              {:else}
                <button class="pill" onclick={() => (confirmDelete = true)}>Delete</button>
              {/if}
            {:else if d.c.job}
              <button class="pill" disabled={busy} onclick={() => act(() => api.cancelDownload(d.c.job.id))}>Cancel download</button>
            {:else if d.c.runnable && !detail.gated}
              <button class="pill" class:primary={d.spare >= 0} disabled={busy} onclick={() => act(() => api.download(detail.repo))}>
                {d.spare >= 0 ? `Download ${gb(detail.download_bytes)}` : 'Download anyway'}
              </button>
            {/if}
          </div>

          {#if variants.length}
            <div class="variants">
              <h3>Other versions</h3>
              {#each variants as v (v.repo)}
                <button onclick={() => select(v.repo)}>
                  <span class="mono vq">{v.quant}</span>
                  <span class="vr">{v.repo}</span>
                  <span class="muted vs">{v.status === 'no' ? 'Won’t run' : v.status === 'ok' ? 'Runs' : 'Check'}</span>
                </button>
              {/each}
            </div>
          {/if}
        </div>
      </div>
    {/if}
  </aside>
</main>

<style>
  main {
    width: 100%; max-width: 1280px; margin: 0 auto; padding: var(--sp-7) var(--sp-6);
    display: flex; flex-wrap: wrap; gap: var(--sp-6); align-items: flex-start;
  }
  .list { flex: 999 1 560px; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-5); }
  .top { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: var(--sp-4); }
  .title { display: flex; flex-direction: column; gap: var(--sp-2); }
  h1 { margin: 0; font-size: 64px; line-height: 0.95; }
  .title p { margin: 0; }
  .search {
    flex: 1 1 280px; max-width: 400px; display: flex; align-items: center; gap: var(--sp-2);
    background: var(--surface); border: 1.5px solid var(--line); border-radius: var(--r-pill); padding: 0 var(--sp-1) 0 var(--sp-4);
  }
  .search input { flex: 1; min-width: 0; height: var(--control); border: none; outline: none; background: transparent; }
  .tabs { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
  .tabs .active { background: var(--ink); color: var(--bg); font-weight: 700; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: var(--sp-4); }
  .model { display: flex; flex-direction: column; padding: 0; border: none; overflow: hidden; text-align: left; cursor: pointer; }
  .model.selected { box-shadow: 0 0 0 2px var(--line); }
  .band {
    display: flex; justify-content: space-between; align-items: center; padding: var(--sp-3) var(--sp-4); color: var(--on-accent);
  }
  .band.faded { opacity: 0.45; }
  .fam { font-size: 13px; font-weight: 600; }
  .quant { font-size: 28px; letter-spacing: -0.02em; }
  .body { display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-4); }
  .name { font-family: var(--display); font-weight: 700; font-size: 19px; line-height: 1.15; overflow-wrap: anywhere; }
  .meta { font-size: 12px; overflow-wrap: anywhere; }
  .badges { display: flex; flex-wrap: wrap; gap: var(--sp-1); }
  .badges span { font-size: 12px; font-weight: 600; padding: var(--sp-1) var(--sp-2); border-radius: var(--r-sm); border: 1px solid var(--line-soft); }
  .status { font-size: 13px; font-weight: 600; padding-top: var(--sp-2); }

  aside { flex: 1 1 380px; max-width: 460px; position: sticky; top: var(--sp-5); }
  .hint { padding: var(--sp-5); }
  .hint p { margin: 0; }
  .panel { overflow: hidden; }
  .head { padding: var(--sp-5); display: flex; flex-direction: column; gap: var(--sp-2); color: var(--on-accent); }
  .head h2 { margin: 0; font-size: 36px; line-height: 1; letter-spacing: -0.03em; overflow-wrap: anywhere; }
  .repo { font-size: 12px; }
  .inner { padding: var(--sp-5); display: flex; flex-direction: column; gap: var(--sp-5); }
  .reason { margin: 0; font-size: 16px; line-height: 1.55; }
  .ctx { display: flex; flex-direction: column; gap: var(--sp-3); }
  .ctx-top { display: flex; justify-content: space-between; align-items: baseline; }
  .ctx-top label { font-weight: 600; }
  .ctx-value { font-size: 32px; letter-spacing: -0.02em; }
  .slider { position: relative; padding-bottom: var(--sp-5); }
  .slider input { width: 100%; margin: 0; accent-color: var(--ink); }
  .marker { position: absolute; top: 24px; transform: translateX(-50%); font-size: 11px; white-space: nowrap; border-left: 1.5px solid var(--muted); padding-left: var(--sp-1); }
  .mem { display: flex; flex-direction: column; gap: var(--sp-3); }
  .membar { display: flex; height: 24px; border-radius: var(--r-sm); overflow: hidden; background: var(--surface-2); }
  .membar span { display: block; height: 100%; }
  .legend { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-2) var(--sp-4); font-size: 13px; }
  .legend span { display: flex; align-items: center; gap: var(--sp-2); }
  .legend i { width: 12px; height: 12px; border-radius: 4px; flex: none; }
  .legend b { margin-left: auto; font-weight: 500; }
  .verdict { margin: 0; font-weight: 600; }
  .small { margin: 0; font-size: 14px; }
  .warn { margin: 0; font-size: 13px; line-height: 1.5; padding: var(--sp-3); border-radius: var(--r-sm); background: var(--surface-2); }
  .row { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
  .variants { display: flex; flex-direction: column; gap: var(--sp-2); padding-top: var(--sp-4); border-top: 1px solid var(--line-soft); }
  .variants h3 { margin: 0 0 var(--sp-1); font-size: 15px; }
  .variants button {
    display: flex; align-items: center; gap: var(--sp-3); min-height: var(--control); padding: var(--sp-2) var(--sp-3);
    border-radius: var(--r-sm); border: none; background: var(--surface-2); text-align: left; cursor: pointer;
  }
  .vq { font-size: 12px; font-weight: 700; min-width: 64px; }
  .vr { flex: 1; min-width: 0; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .vs { font-size: 12px; }
</style>
