<script>
  import { fly, slide } from 'svelte/transition';
  import { backOut, cubicOut } from 'svelte/easing';
  import { app, newThread } from './app.svelte.js';
  import { api, familyColor, familyName, gb, kTokens, setAccent } from './api.js';
  import { render } from './markdown.js';
  import Avatar from './Avatar.svelte';

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
  let aboutOpen = $state(false);
  let about = $state(null); // GET /manage/about: avatar, benchmarks
  let hf = $state(null); // signed-in Hugging Face user, for gated models
  let token = $state('');
  api.hfAccount().then((a) => (hf = a)).catch(() => {});

  // Benchmarks load only when someone opens the section.
  $effect(() => {
    const repo = selRepo;
    if (!aboutOpen || !repo || about?.repo === repo) return;
    about = { repo, loading: true };
    api.about(repo).then((a) => { if (selRepo === repo) about = a; })
      .catch((e) => { if (selRepo === repo) about = { repo, error: e.message }; });
  });

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

  async function select(repo, openAbout = false) {
    if (selRepo === repo) { if (openAbout) aboutOpen = true; return; }
    aboutOpen = openAbout;
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
    const value = Math.max(4096, ctx ?? (loaded ? engine.context : fit.default_context) ?? 4096);
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

  // Plain-language strengths: the catalog's own notes plus what the model's size, format and features imply.
  const BYTES_PER_PARAM = { NVFP4: 0.56, MXFP4: 0.53, FP8: 1, BF16: 2, FP16: 2, FP32: 4 };
  const FORMAT = {
    NVFP4: '4-bit (NVFP4): about a quarter of full-size memory, with a small quality cost',
    MXFP4: '4-bit (MXFP4): very compact, the way its makers trained it',
    FP8: '8-bit (FP8): close to full quality at half the memory',
    BF16: 'Full precision (BF16): best quality, most memory',
  };
  function plain(x) {
    const launch = x.launch ?? {};
    const fit = localOf(x.repo)?.fit ?? x.fit;
    const named = x.repo.match(/(\d+(?:\.\d+)?)\s*B\b/i);
    const per = BYTES_PER_PARAM[x.quant];
    const params = named ? Number(named[1]) : x.weights_bytes && per ? x.weights_bytes / per / 1e9 : null;
    const good = [...(x.good_at ?? [])];
    const weak = [...(x.weaker_at ?? [])];
    // A generated line only appears when the curated notes don't already cover its topic.
    const covered = (re) => [...good, ...weak].some((line) => re.test(line));
    if (params) {
      const p = params < 10 ? String(Math.round(params * 10) / 10) : String(Math.round(params));
      if (params < 5) {
        good.push(`Small (about ${p}B parameters): quick, and leaves room for long context`);
        if (!weak.length) weak.push('Less knowledge and weaker on hard problems than bigger models');
      } else if (params < 10) {
        good.push(`Mid-size (about ${p}B parameters): a good balance of speed and quality`);
      } else {
        good.push(`Large for one GPU (about ${p}B parameters): more knowledge, better reasoning`);
        if (!covered(/slow/i)) weak.push('Slower, and leaves less memory for context');
      }
    }
    if (FORMAT[x.quant] && !covered(/precision|memory than/i)) good.push(FORMAT[x.quant]);
    if (launch.reasoning && !covered(/think|reason/i)) good.push('Thinks step by step before answering: slower, but better on hard questions');
    if (launch.tools && !covered(/tool/i)) good.push('Can call tools, so Hermes Agent can drive it');
    if (!launch.tools && !covered(/tool/i)) weak.push('Can’t call tools here, so it’s for chat only');
    if (fit?.model_max >= 100000 && !covered(/context|long input/i)) good.push(`Reads very long inputs: up to ${kTokens(fit.model_max)} tokens`);
    return { good: [...new Set(good)], weak: [...new Set(weak)] };
  }
  // Scores come as 0-1 fractions or as percentages; show both as percentages.
  const fmtScore = (v) => String(v <= 1 && v % 1 !== 0 ? Math.round(v * 1000) / 10 : Math.round(v * 10) / 10);

  const strengths = $derived(detail ? plain(detail) : { good: [], weak: [] });

  function chatWith(id) {
    newThread(id);
    app.screen = 'chat';
  }
</script>

<main>
  <section class="list" aria-label="Models">
    <div class="top">
      <div class="title">
        <span class="eyebrow">{library.length ? `${library.length} on this pc` : 'nothing downloaded yet'}</span>
        <h1 class="display">models</h1>
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

    <div class="tabs segmented" role="tablist" aria-label="Which models">
      {#each [['picks', 'picked for your gpu'], ['pc', 'on this pc'], ['search', results ? `“${query}”` : 'search']] as [key, label]}
        <button role="tab" aria-selected={tab === key} onclick={() => (tab = key)}>{label}</button>
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
      {#each items as c, i (c.repo)}
        <button class="model card" class:selected={c.repo === selRepo} aria-pressed={c.repo === selRepo}
          in:fly={{ y: 24, delay: Math.min(i, 12) * 35, duration: 550, easing: backOut }}
          onclick={(e) => select(c.repo, !!e.target.closest('.thumb'))}>
          <span class="band" class:faded={c.faded}>
            <span class="thumb" title="Strengths and benchmarks"><Avatar repo={c.repo} size={36} /></span>
            <span class="eyebrow fam">{familyName(c.repo).toLowerCase()}</span>
            <span class="quant" style="color: {familyColor(c.repo)}">{c.quant.toLowerCase()}</span>
          </span>
          <span class="body">
            <span class="name">{c.repo.split('/').pop()}</span>
            <span class="num muted meta">{c.meta}</span>
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
    <div class="hf">
        {#if hf?.user}
          <span class="muted small">Signed in to Hugging Face as <b>{hf.user}</b>, so gated models work.</span>
          <button class="pill quiet" onclick={async () => { await api.hfLogout(); hf = { user: null }; }}>Sign out</button>
        {:else}
          <span class="muted small">Some models are gated: accept their license on huggingface.co, then add a token.</span>
          <form class="token" onsubmit={async (e) => { e.preventDefault(); try { hf = await api.hfLogin(token); token = ''; } catch (err) { error = err.message; } }}>
            <label class="sr-only" for="hf-token">Hugging Face token</label>
            <input id="hf-token" type="password" bind:value={token} placeholder="HF token, for gated models" autocomplete="off" />
            <button class="pill quiet" disabled={!token.trim()}>Sign in</button>
          </form>
        {/if}
      </div>
  </section>

  <aside aria-label="Selected model">
    {#if !selRepo}
      <div class="card hint"><p class="muted">Pick a model to see how much of your GPU it needs and what it can do.</p></div>
    {:else if !d}
      <div class="card hint"><p class="muted">{error || 'Reading the model’s config from Hugging Face…'}</p></div>
    {:else}
      <div class="card panel">
        <div class="head">
          <button class="head-thumb" onclick={() => (aboutOpen = !aboutOpen)} aria-expanded={aboutOpen} aria-label="Strengths and benchmarks">
            <Avatar repo={detail.repo} size={56} />
          </button>
          <span class="eyebrow">{familyName(detail.repo).toLowerCase()} · {d.c.quant.toLowerCase()}</span>
          <h2 class="display">{detail.repo.split('/').pop()}</h2>
          <span class="eyebrow repo">{detail.repo}</span>
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
                {#if d.marker}<span class="marker num muted" style="left: {d.marker}">Hermes 64K</span>{/if}
              </div>
            </div>
            <div class="mem">
              <div class="membar" role="img" aria-label="GPU memory with this context">
                {#each d.segs as s}<span style="width: {s.width}; background: {s.fill}"></span>{/each}
              </div>
              <div class="legend">
                {#each d.segs as s}
                  <span><i style="background: {s.fill}" aria-hidden="true"></i>{s.label}<b class="num">{gb(s.bytes)}</b></span>
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

          <div class="about">
            <button class="about-toggle" onclick={() => (aboutOpen = !aboutOpen)} aria-expanded={aboutOpen}>
              <span>Strengths and benchmarks</span>
              <svg class="chev" class:open={aboutOpen} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M6 9l6 6 6-6"></path></svg>
            </button>
            {#if aboutOpen}
              <div class="about-body" transition:slide={{ duration: 450, easing: cubicOut }}>
                <div class="plain">
                  <div>
                    <h4>Good at</h4>
                    <ul>{#each strengths.good as g, i}<li in:fly={{ x: -12, delay: i * 50, duration: 450, easing: backOut }}>{g}</li>{/each}</ul>
                  </div>
                  {#if strengths.weak.length}
                    <div>
                      <h4>Not so good at</h4>
                      <ul>{#each strengths.weak as w, i}<li in:fly={{ x: -12, delay: 150 + i * 50, duration: 450, easing: backOut }}>{w}</li>{/each}</ul>
                    </div>
                  {/if}
                </div>
                <h4>Benchmarks</h4>
                {#if about?.loading}
                  <p class="muted small">Reading benchmarks from Hugging Face…</p>
                {:else if about?.error}
                  <p class="muted small">{about.error}</p>
                {:else if about?.evals?.length || about?.tables?.length}
                  {#if about.evals.length}
                    <table class="evals num">
                      <tbody>
                        {#each about.evals as e}
                          <tr>
                            <td>{e.benchmark.split('/').pop()}{e.task && e.task !== e.benchmark.split('/').pop() ? ` · ${e.task}` : ''}</td>
                            <td class="score">{fmtScore(e.value)}</td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                    <p class="muted small">Published on Hugging Face for <b>{about.evals[0].from}</b>{about.evals[0].from !== detail.repo ? ', the model this one is built from' : ''}. Higher is better.</p>
                  {/if}
                  {#if about.tables.length}
                    <div class="prose card-table">{@html render(about.tables.join('\n\n'))}</div>
                    <p class="muted small">Quoted from the model card of <b>{about.tables_from}</b>.</p>
                  {/if}
                {:else if about}
                  <p class="muted small">No benchmarks are published on Hugging Face for this model or the one it’s built from.</p>
                {/if}
              </div>
            {/if}
          </div>

          {#if variants.length}
            <div class="variants">
              <h3>Other versions</h3>
              {#each variants as v (v.repo)}
                <button onclick={() => select(v.repo)}>
                  <span class="num vq">{v.quant}</span>
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
  h1 { margin: 0; font-size: clamp(72px, 9vw, 128px); }
  .title p { margin: 0; }
  .search {
    flex: 1 1 280px; max-width: 400px; display: flex; align-items: center; gap: var(--sp-2);
    background: var(--surface); border-radius: var(--r); padding: 0 var(--sp-4); color: var(--ink-2);
  }
  .search input { flex: 1; min-width: 0; height: var(--control); border: none; outline: none; background: transparent; }
  .tabs { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
  .tabs button { font-size: 22px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: var(--sp-4); }
  .model { display: flex; flex-direction: column; padding: 0; border: none; overflow: hidden; text-align: left; cursor: pointer; }
  .model.selected { background: var(--surface-2); box-shadow: inset 0 0 0 2px var(--accent); }
  .band { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-4) var(--sp-4) 0; }
  .band .quant { margin-left: auto; }
  .thumb { display: flex; border-radius: var(--r-sm); transition: transform 0.45s var(--spring); }
  .thumb:hover { transform: scale(1.15) rotate(-4deg); }
  .model { transition: transform 0.45s var(--spring), box-shadow 0.25s var(--ease); }
  .model:hover { transform: translateY(-4px); }
  .model:active { transform: scale(0.97); transition-duration: 0.08s; }
  .head-thumb { align-self: flex-start; border: none; padding: 0; background: none; cursor: pointer; border-radius: var(--r-sm); transition: transform 0.45s var(--spring); }
  .head-thumb:hover { transform: scale(1.08) rotate(-3deg); }
  .about { display: flex; flex-direction: column; border-top: 1px solid var(--line-soft); padding-top: var(--sp-3); }
  .about-toggle {
    display: flex; justify-content: space-between; align-items: center; min-height: var(--control); padding: 0;
    border: none; background: none; font-weight: 300; font-size: 20px; cursor: pointer; text-align: left;
  }
  .chev { transition: transform 0.45s var(--spring); }
  .chev.open { transform: rotate(180deg); }
  .about-body { display: flex; flex-direction: column; gap: var(--sp-3); padding-bottom: var(--sp-2); }
  .about-body h4 { margin: var(--sp-2) 0 0; font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .plain { display: flex; flex-direction: column; gap: var(--sp-2); }
  .plain ul { margin: var(--sp-2) 0 0; padding-left: var(--sp-5); display: flex; flex-direction: column; gap: var(--sp-1); font-size: 14px; line-height: 1.45; }
  .evals { width: 100%; border-collapse: collapse; font-size: 14px; }
  .evals td { padding: var(--sp-2) 0; border-bottom: 1px solid var(--line-soft); }
  .evals .score { text-align: right; font-weight: 500; color: var(--accent); }
  /* Verza's voice: actions and states in lowercase */
  .status, .badges span, .row :global(.pill), .about-toggle, .hf :global(.pill), .variants .vs { text-transform: lowercase; }
  .card-table { font-size: 13px; overflow-wrap: normal; }
  .card-table :global(th), .card-table :global(td) { white-space: nowrap; padding: var(--sp-2); }
  .hf { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-3); padding-top: var(--sp-3); border-top: 1px solid var(--line-soft); }
  .token { display: flex; gap: var(--sp-2); }
  .token input { height: var(--control); width: 240px; padding: 0 var(--sp-4); border-radius: var(--r); border: 0; background: var(--surface); outline: none; }
  .band.faded { opacity: 0.45; }
  .fam { font-size: 13px; }
  .quant { font-size: 22px; font-weight: 300; letter-spacing: -0.02em; transition: color 0.6s var(--ease); }
  .body { display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-4); }
  .name { font-weight: 400; font-size: 20px; letter-spacing: -0.01em; line-height: 1.2; overflow-wrap: anywhere; }
  .meta { font-size: 12px; overflow-wrap: anywhere; }
  .badges { display: flex; flex-wrap: wrap; gap: var(--sp-1); }
  .badges { gap: var(--sp-1) var(--sp-3) !important; }
  .badges span { font-size: 13px; color: var(--ink-2); }
  .status { font-size: 16px; font-weight: 300; color: var(--accent); padding-top: var(--sp-2); }

  aside { flex: 1 1 380px; max-width: 460px; position: sticky; top: var(--sp-5); }
  .hint { padding: var(--sp-5); }
  .hint p { margin: 0; }
  .panel { overflow: hidden; }
  .head { padding: var(--sp-5) var(--sp-5) 0; display: flex; flex-direction: column; gap: var(--sp-2); }
  .head h2 { margin: 0; font-size: 40px; line-height: 0.95; overflow-wrap: anywhere; }
  .repo { font-size: 12px; }
  .inner { padding: var(--sp-5); display: flex; flex-direction: column; gap: var(--sp-5); }
  .reason { margin: 0; font-size: 16px; line-height: 1.55; }
  .ctx { display: flex; flex-direction: column; gap: var(--sp-3); }
  .ctx-top { display: flex; justify-content: space-between; align-items: baseline; }
  .ctx-top label { font-weight: 600; }
  .ctx-value { font-size: 40px; color: var(--accent); }
  .slider { position: relative; padding-bottom: var(--sp-5); }
  .slider input { width: 100%; margin: 0; accent-color: var(--accent); }
  .marker { position: absolute; top: 24px; transform: translateX(-50%); font-size: 11px; white-space: nowrap; border-left: 1px solid var(--muted); padding-left: var(--sp-1); }
  .mem { display: flex; flex-direction: column; gap: var(--sp-3); }
  .membar { display: flex; height: 12px; border-radius: 6px; overflow: hidden; background: var(--surface-2); }
  .membar span { display: block; height: 100%; }
  .legend { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-2) var(--sp-4); font-size: 13px; }
  .legend span { display: flex; align-items: center; gap: var(--sp-2); }
  .legend i { width: 12px; height: 12px; border-radius: 4px; flex: none; }
  .legend b { margin-left: auto; font-weight: 500; }
  .verdict { margin: 0; font-size: 18px; font-weight: 300; }
  .small { margin: 0; font-size: 14px; }
  .warn { margin: 0; font-size: 13px; line-height: 1.5; padding: var(--sp-3); border-radius: var(--r-sm); background: var(--surface-2); }
  .row { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
  .variants { display: flex; flex-direction: column; gap: var(--sp-2); padding-top: var(--sp-4); border-top: 1px solid var(--line-soft); }
  .variants h3 { margin: 0 0 var(--sp-1); font-size: 15px; }
  .variants button {
    display: flex; align-items: center; gap: var(--sp-3); min-height: var(--control); padding: var(--sp-2) var(--sp-3);
    border-radius: var(--r-sm); background: var(--surface-2); text-align: left; transition: background-color 0.2s;
  }
  .variants button:hover { background: var(--line); }
  .vq { font-size: 13px; font-weight: 500; min-width: 64px; }
  .vr { flex: 1; min-width: 0; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .vs { font-size: 12px; }
</style>
