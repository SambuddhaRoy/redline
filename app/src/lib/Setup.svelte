<script>
  import { onMount } from 'svelte';
  import { app, waitOnline, startPolling, setTheme } from './app.svelte.js';
  import { api, FAMILY, BRAND } from './api.js';
  import { inApp, bootstrap, startBackend, installWsl } from './shell.js';
  import Hermes from './Hermes.svelte';

  const PICKS = [
    { repo: 'nvidia/NVIDIA-Nemotron-3-Nano-4B-FP8', name: 'Nemotron 3 Nano 4B', family: 'nemotron', size: '4.9 GB',
      why: 'Recommended. Fast, 64K context, tool calling: the one to use with Hermes Agent.' },
    { repo: 'nvidia/Qwen3-8B-NVFP4', name: 'Qwen3 8B', family: 'qwen', size: '6.0 GB',
      why: 'Stronger general answers and thinking mode. Context tops out at 40K.' },
    { repo: 'NousResearch/DeepHermes-3-Llama-3-3B-Preview', name: 'DeepHermes 3 3B', family: 'hermes', size: '6.0 GB',
      why: 'A small Nous Research model with a 64K context.' },
  ];
  // setup.sh steps in order, with roughly how many GB each downloads (drives the percentage).
  const STEPS = [
    ['image', 'Ubuntu for the engine', 0.39], ['apt', 'System packages', 0.3], ['cuda', 'CUDA compiler', 0.3],
    ['python', 'Python', 0.05], ['torch', 'PyTorch', 0.56], ['tensorrt_llm', 'TensorRT-LLM and NVIDIA libraries', 4.94],
    ['gateway', 'Peregrine engine', 0.5], ['verify', 'Check the GPU', 0],
  ];
  const TOTAL_GB = STEPS.reduce((a, s) => a + s[2], 0);
  const IMAGE_BYTES = 388975696;
  // The first cold load on an RTX 5070 Ti took 111 s; phases from its startup log, rounded.
  const PHASES = [['Start the engine', 13], ['Read the weights', 6], ['Compile kernels for your GPU', 79], ['Reserve memory and open the API', 13]];
  const TABS = [['check', 'check'], ['install', 'install'], ['load', 'first load'], ['ready', 'ready']];

  let step = $state('boot'); // boot | check | install | load | ready | error
  let check = $state(null);
  let progress = $state({}); // setup.sh step -> 'now' | 'done'
  let imageBytes = $state(0);
  let pick = $state(PICKS[0].repo);
  let phase = $state(''); // after setup.sh: backend | download | load
  let queued = $state('');
  let error = $state('');
  let errorLog = $state('');
  let loadStart = $state(0);
  let retry = $state(null); // what "Try again" re-runs: the check or the install
  let now = $state(Date.now());

  const model = $derived(PICKS.find((p) => p.repo === pick));
  const job = $derived(app.status?.downloads?.find((d) => d.repo === pick));
  const engine = $derived(app.status?.engine);
  const wslMissing = $derived(check && !check.wsl);
  const problems = $derived((check?.problems ?? []).filter((p) => !p.startsWith('WSL')));
  const doneGb = $derived(
    STEPS.reduce((a, [key, , g]) => a + (progress[key] === 'done' ? g : 0), 0) +
      (progress.image === 'now' ? (imageBytes / IMAGE_BYTES) * 0.39 : 0),
  );
  const pct = $derived(Math.min(100, Math.round((100 * doneGb) / TOTAL_GB)));
  const elapsed = $derived(loadStart ? Math.max(0, Math.round((now - loadStart) / 1000)) : 0);
  const tabIndex = $derived(TABS.findIndex(([k]) => k === step));

  $effect(() => {
    document.documentElement.style.setProperty('--accent', step === 'check' || step === 'boot' ? BRAND : FAMILY[model.family][0]);
  });
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  const mmss = (s) => Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  const until = async (cond, what) => {
    for (;;) {
      const r = cond();
      if (r) return r;
      if (what && engine?.state === 'error') throw new Error(engine.error?.split('\n')[0] ?? `${what} failed.`);
      await new Promise((res) => setTimeout(res, 1000));
    }
  };
  function fail(message, log = '', again = install) {
    error = message;
    errorLog = log;
    retry = again;
    step = 'error';
  }

  async function boot() {
    if (!inApp) return preview();
    try {
      const r = await bootstrap('check', () => {});
      check = r.events.find((e) => e.step === 'check') ?? null;
      if (!check) {
        const why = r.stderr?.trim() ? r.stderr.trim().split('\n').slice(-6).join('\n') : `exit code ${r.code}`;
        return fail(`Couldn’t check this PC. ${r.script}: ${why}`, '', boot);
      }
      if (check.distro) {
        await startBackend();
        await waitOnline();
        app.screen = 'chat';
      } else {
        step = 'check';
      }
    } catch (e) {
      fail(e.message, '', boot);
    }
  }

  async function install() {
    error = '';
    if (wslMissing) {
      await installWsl(); // UAC prompt; Windows needs a restart afterwards
      fail('WSL is turned on. Restart Windows, then open Redline again to finish setup.');
      return;
    }
    step = 'install';
    progress = {};
    try {
      const { code, events } = await bootstrap('install', (e) => {
        if (e.status === 'start') progress[e.step] = 'now';
        if (e.status === 'done') progress[e.step] = 'done';
        if (e.status === 'progress') imageBytes = e.bytes;
      });
      const failed = events.find((e) => e.status === 'error');
      if (code === 2) return fail('Windows needs administrator access to turn on WSL. Run Redline as administrator once.');
      if (code === 3) return fail('Restart Windows, then open Redline again to finish setup.');
      if (code !== 0) {
        const where = STEPS.find(([k]) => k === failed?.step)?.[1] ?? failed?.step ?? 'an unknown step';
        return fail(failed?.message ?? `Setup stopped at “${where}”.`, failed?.log ?? '');
      }
      phase = 'backend';
      await startBackend();
      await waitOnline();
      phase = 'download';
      const started = await api.download(pick);
      await until(() => app.status?.downloads?.find((d) => d.id === started.id && d.status !== 'downloading'));
      const finished = app.status.downloads.find((d) => d.id === started.id);
      if (finished.status !== 'done') throw new Error(finished.error ?? 'The model download stopped.');
      phase = 'load';
      step = 'load';
      loadStart = Date.now();
      await api.load(started.id);
      await until(() => engine?.model === started.id && engine.state === 'ready', 'Loading the model');
      if (queued.trim()) {
        app.queued = queued;
        app.screen = 'chat';
      } else {
        step = 'ready';
      }
    } catch (e) {
      fail(e.message);
    }
  }

  // Browser-only preview of each step with sample data (`npm run dev`, then ?setup=check|install|load|ready).
  function preview() {
    const which = new URLSearchParams(location.search).get('setup');
    if (!which) {
      startPolling();
      app.screen = 'chat';
      return;
    }
    check = { gpu: { name: 'NVIDIA GeForce RTX 5070 Ti', vram_mib: 16303, driver: '621.01', compute_cap: 12 }, wsl: '2.6.3.0', problems: [] };
    progress = { image: 'done', apt: 'done', cuda: 'done', python: 'done', torch: 'done', tensorrt_llm: 'now' };
    loadStart = Date.now() - 48000;
    step = which;
  }

  onMount(boot); // not during init: boot() may switch app.screen, which the parent's {#if} is still being built from
</script>

<header>
  <span class="word">redline</span>
  {#if tabIndex >= 0}
    <ol class="segmented" aria-label="Setup steps">
      {#each TABS as [key, label], i}
        <li><span class="tab" class:on={i === tabIndex} class:past={i < tabIndex} aria-current={i === tabIndex ? 'step' : undefined}><span class="num">0{i + 1}</span>{label}</span></li>
      {/each}
    </ol>
  {/if}
  <div class="segmented" role="group" aria-label="Appearance">
    {#each [['system', 'auto'], ['light', 'light'], ['dark', 'dark']] as [key, label]}
      <button aria-pressed={app.theme === key} onclick={() => setTheme(key)}>{label}</button>
    {/each}
  </div>
</header>

<main>
  {#if step === 'boot'}
    <p class="muted">Checking this PC…</p>

  {:else if step === 'check'}
    <div class="split">
      <div class="lead">
        <h1 class="display">{wslMissing ? 'almost. wsl comes first.' : problems.length ? 'this pc can’t run it yet.' : 'your gpu is ready for this.'}</h1>
        <p class="big">TensorRT-LLM only runs on Linux, so Redline sets up its own small Ubuntu inside WSL. Your files and any Linux you already use stay untouched.</p>
        {#if wslMissing}
          <p class="note">Windows needs WSL turned on first. Redline can do it for you: Windows asks for administrator access, then a restart. Setup picks up where it left off.</p>
        {/if}
        {#each problems as p}<p class="note">{p}</p>{/each}
        <div class="row">
          <button class="go display" disabled={problems.length > 0} onclick={install}>{wslMissing ? 'turn on wsl' : 'install · about 7 gb'}</button>
          <span class="muted small">{wslMissing ? 'Needs administrator access and a restart' : 'About 10 minutes on a 100 Mbit line'}</span>
        </div>
      </div>
      <ul class="checks" aria-label="System check">
        {#each [
          ['Graphics card', check.gpu ? `${check.gpu.name.replace('NVIDIA GeForce ', '')} · ${Math.round(check.gpu.vram_mib / 1024)} GB` : 'No NVIDIA GPU found', !!check.gpu && check.gpu.compute_cap >= 8],
          ['NVIDIA driver', check.gpu ? `${check.gpu.driver} (580 or newer needed)` : '—', !!check.gpu && parseInt(check.gpu.driver) >= 580],
          ['WSL', check.wsl ?? 'Not turned on', !!check.wsl],
        ] as [label, value, good]}
          <li class="card">
            <span class="mark" class:bad={!good} aria-hidden="true">{good ? '✓' : '!'}</span>
            <span class="what"><b>{label}</b><span class="num muted">{value}</span></span>
            <span class="verdict">{good ? 'Good' : 'Needed'}</span>
          </li>
        {/each}
      </ul>
    </div>

  {:else if step === 'install'}
    <div class="split">
      <div class="progress">
        <div class="display pct">{pct}<span>%</span></div>
        <p class="highlight label">
          {phase === 'download' && job ? `Downloading ${model.name} · ${(job.bytes / 2 ** 30).toFixed(1)} of ${(job.total / 2 ** 30).toFixed(1)} GB`
            : phase === 'backend' ? 'Starting the engine' : `${doneGb.toFixed(1)} of ${TOTAL_GB.toFixed(1)} GB`}
        </p>
        <ol class="steps" aria-label="Install steps">
          {#each STEPS as [key, label, size]}
            <li>
              <span class="mark" class:todo={!progress[key]} class:now={progress[key] === 'now'} aria-hidden="true">{progress[key] === 'done' ? '✓' : ''}</span>
              <span class:muted={!progress[key]} class:bold={progress[key] === 'now'}>{label}</span>
              <span class="num muted size">{size ? size.toFixed(1) + ' GB' : ''}</span>
            </li>
          {/each}
        </ol>
      </div>
      <div class="lead">
        <h2 class="display h2">while that runs, pick your first model.</h2>
        <p class="muted">It downloads as soon as the install finishes. You can add more from the library any time.</p>
        <div class="picks" role="radiogroup" aria-label="First model">
          {#each PICKS as p (p.repo)}
            <button class="card pick" class:on={p.repo === pick} role="radio" aria-checked={p.repo === pick} disabled={phase !== ''} onclick={() => (pick = p.repo)}>
              <span class="dot" style="background: {FAMILY[p.family][0]}" aria-hidden="true"></span>
              <span class="what"><b class="display-sm">{p.name}</b><span class="muted">{p.why}</span></span>
              <span class="num size">{p.size}</span>
            </button>
          {/each}
        </div>
      </div>
    </div>

  {:else if step === 'load'}
    <div class="split">
      <div class="lead">
        <h1 class="display">warming up <span class="highlight">{model.name.toLowerCase()}</span></h1>
        <p class="big">The first load compiles kernels for your exact GPU, so it takes about two minutes. Every load after that takes about one.</p>
        <label for="queued" class="bold">Type your first message now. It sends the moment the model is ready.</label>
        <textarea id="queued" rows="2" bind:value={queued} placeholder="Ask anything"></textarea>
        <div class="row">
          {#each ['Explain this error message', 'Write a PowerShell script that', 'Review my Python function'] as s}
            <button class="pill quiet" onclick={() => (queued = s + ' ')}>{s}</button>
          {/each}
        </div>
      </div>
      <div class="timer">
        <div class="clock"><span class="display">{mmss(elapsed)}</span><span class="muted">of about 1:51</span></div>
        <ol class="phases" aria-label="Load phases">
          {#each PHASES as [name, secs], i}
            {@const start = PHASES.slice(0, i).reduce((a, p) => a + p[1], 0)}
            {@const f = Math.max(0, Math.min(1, (elapsed - start) / secs))}
            <li>
              <span class="ph"><span class:bold={f > 0 && f < 1} class:muted={f === 0}>{name}</span><span class="num muted">{f >= 1 ? secs + ' s' : 'about ' + secs + ' s'}</span></span>
              <span class="bar"><span style="width: {f * 100}%; background: {f < 1 ? 'var(--accent)' : 'var(--ink)'}"></span></span>
            </li>
          {/each}
        </ol>
        <p class="num muted log">{engine?.log_tail?.at(-1) ?? ''}</p>
      </div>
    </div>

  {:else if step === 'ready'}
    <div class="split">
      <div class="lead">
        <h1 class="display huge">ready.</h1>
        <p class="big"><span class="highlight">{model.name}</span> is loaded on your {check?.gpu?.name.replace('NVIDIA GeForce ', '') ?? 'GPU'}.</p>
        <button class="go display" onclick={() => (app.screen = 'chat')}>start chatting</button>
      </div>
      <div class="side"><Hermes compact /></div>
    </div>

  {:else if step === 'error'}
    <div class="lead narrow">
      <h1 class="display">setup stopped.</h1>
      <p class="big">{error}</p>
      {#if errorLog}<p class="muted small">Details are in <span class="num">{errorLog}</span> inside the Peregrine WSL distro.</p>{/if}
      <div class="row">
        <button class="go display" onclick={() => { step = 'boot'; retry(); }}>try again</button>
        <span class="muted small">Setup skips the parts that already finished.</span>
      </div>
    </div>
  {/if}
</main>

<style>
  header { padding: var(--sp-5) var(--sp-6) 0; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--sp-4); }
  .word { font-size: 26px; font-weight: 400; }
  ol.segmented { margin: 0; list-style: none; }
  .tab { min-height: var(--control); display: flex; align-items: center; gap: var(--sp-2); font-size: 18px; font-weight: 300; color: var(--muted); }
  .tab.past { color: var(--ink); }
  .tab.on { color: var(--accent); }
  .tab .num { font-size: 12px; }

  main { flex: 1; width: 100%; max-width: 1120px; margin: 0 auto; padding: var(--sp-7) var(--sp-6); }
  .split { display: flex; flex-wrap: wrap; gap: var(--sp-7); }
  .lead { flex: 999 1 480px; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-5); }
  .lead.narrow { max-width: 720px; }
  h1 { margin: 0; font-size: 64px; line-height: 0.98; }
  h1.huge { font-size: 120px; line-height: 0.85; letter-spacing: -0.06em; }
  .h2 { margin: 0; font-size: 48px; line-height: 1; letter-spacing: -0.03em; }
  .big { margin: 0; font-size: 19px; line-height: 1.6; max-width: 600px; }
  .note { margin: 0; padding: var(--sp-4); border-radius: var(--r-md); background: var(--surface-2); max-width: 600px; line-height: 1.6; }
  .small { font-size: 14px; }
  .bold { font-weight: 500; }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-3); }
  .go {
    min-height: var(--control-lg); padding: 0; border: none; cursor: pointer;
    background: none; color: var(--accent); font-size: 32px; font-weight: 300; letter-spacing: -0.02em;
  }
  .go:disabled { opacity: 0.45; cursor: default; }
  .mark {
    width: 20px; height: 20px; flex: none; border-radius: var(--r-pill); display: grid; place-items: center;
    font-size: 12px; font-weight: 700; background: var(--ink); color: var(--bg);
  }
  .mark.bad, .mark.now { background: var(--accent); color: var(--on-accent); }
  .mark.todo { background: var(--surface-2); }

  .checks { flex: 1 1 360px; margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: var(--sp-2); }
  .checks li { display: flex; align-items: center; gap: var(--sp-4); min-height: var(--control-lg); padding: var(--sp-2) var(--sp-4); }
  .what { display: flex; flex-direction: column; gap: var(--sp-1); min-width: 0; text-align: left; }
  .what .num { font-size: 12px; }
  .verdict { margin-left: auto; font-size: 13px; font-weight: 600; }

  .progress { flex: 1 1 380px; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-4); }
  .pct { font-size: 160px; line-height: 0.82; letter-spacing: -0.06em; }
  .pct span { font-size: 64px; }
  .label { margin: 0; align-self: flex-start; font-weight: 600; padding: var(--sp-1) var(--sp-3); }
  .steps { margin: var(--sp-3) 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: var(--sp-3); }
  .steps li { display: flex; align-items: center; gap: var(--sp-3); }
  .size { margin-left: auto; font-size: 12px; white-space: nowrap; }
  .picks { display: flex; flex-direction: column; gap: var(--sp-3); }
  .pick { display: flex; align-items: center; gap: var(--sp-4); padding: var(--sp-4) var(--sp-5); border: none; cursor: pointer; }
  .pick.on { box-shadow: 0 0 0 2px var(--line); }
  .pick:disabled { cursor: default; }
  .dot { width: 16px; height: 16px; flex: none; border-radius: var(--r-pill); }
  .display-sm { font-size: 20px; font-weight: 400; letter-spacing: -0.01em; }

  textarea {
    resize: none; padding: var(--sp-4) var(--sp-5); border: 0; border-radius: var(--r);
    background: var(--surface); font-size: 18px; outline: none; max-width: 640px;
  }
  .timer { flex: 1 1 340px; display: flex; flex-direction: column; gap: var(--sp-4); }
  .clock { display: flex; align-items: baseline; gap: var(--sp-3); }
  .clock .display { font-size: 120px; line-height: 0.85; letter-spacing: -0.06em; }
  .phases { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: var(--sp-3); }
  .phases li { display: flex; flex-direction: column; gap: var(--sp-1); }
  .ph { display: flex; justify-content: space-between; gap: var(--sp-3); }
  .ph .num { font-size: 12px; }
  .log { margin: 0; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .side { flex: 1 1 360px; }
</style>
