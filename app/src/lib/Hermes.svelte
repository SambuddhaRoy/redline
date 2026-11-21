<script>
  import { api } from './api.js';

  let { compact = false } = $props();
  let installs = $state(null);
  let busy = $state(false);
  let error = $state('');

  async function refresh() {
    try { installs = await api.hermes(); } catch (e) { error = e.message; installs = []; }
  }
  async function run(fn) {
    busy = true;
    error = '';
    try {
      const results = await fn();
      const failed = results.filter((r) => r.error);
      if (failed.length) error = failed.map((r) => `${r.where}: ${r.error}`).join(' ');
      await refresh();
    } catch (e) {
      error = e.message;
    }
    busy = false;
  }
  refresh();
  const connected = $derived(installs?.some((i) => i.connected));
</script>

<section class:card={compact} class:compact>
  <h2 class="display">{compact ? 'Hermes Agent found' : 'Hermes Agent'}</h2>
  <p class="muted">
    Connect it and Hermes can code with your local model. Redline adds its Peregrine engine as a provider named
    <span class="num">peregrine</span> and leaves your current settings alone. Models need tool calling and 64K of context.
  </p>
  {#if installs === null}
    <p class="muted">Looking for Hermes installs…</p>
  {:else if !installs.length}
    <p>No Hermes Agent install found in Windows or WSL.</p>
  {:else}
    <ul>
      {#each installs as i}
        <li>
          <span class="where">
            <b>{i.kind === 'wsl' ? `${i.where} (WSL)` : 'Windows'}</b>
            <span class="num muted">Hermes {i.version}</span>
          </span>
          <span class="state" class:on={i.connected}>{i.connected ? 'Connected' : 'Not connected'}</span>
        </li>
      {/each}
    </ul>
    <div class="row">
      <button class="pill primary" disabled={busy} onclick={() => run(api.hermesConnect)}>
        {busy ? 'Working…' : connected ? 'Refresh models in Hermes' : 'Connect'}
      </button>
      {#if connected}<button class="pill" disabled={busy} onclick={() => run(api.hermesDisconnect)}>Disconnect</button>{/if}
    </div>
    <code class="num muted">hermes chat --provider peregrine</code>
  {/if}
  {#if error}<p class="error">{error}</p>{/if}
</section>

<style>
  section { display: flex; flex-direction: column; gap: var(--sp-4); }
  section.compact { padding: var(--sp-5); }
  h2 { margin: 0; font-size: 64px; line-height: 0.95; }
  .compact h2 { font-size: 28px; letter-spacing: -0.02em; }
  p { margin: 0; line-height: 1.6; }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
  li {
    display: flex; align-items: center; gap: var(--sp-3); min-height: var(--control-lg);
    padding: var(--sp-2) var(--sp-4); border-radius: var(--r-md); background: var(--surface-2);
  }
  .where { display: flex; flex-direction: column; gap: var(--sp-1); }
  .where .num { font-size: 12px; }
  .state { margin-left: auto; font-size: 13px; font-weight: 600; }
  .state.on { background: var(--accent); color: var(--on-accent); padding: var(--sp-1) var(--sp-3); border-radius: var(--r-sm); }
  .row { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
  code { font-size: 12px; }
  .error { background: var(--surface-2); padding: var(--sp-3) var(--sp-4); border-radius: var(--r-sm); font-size: 14px; }
</style>
