<script>
  import { app, setTheme } from './app.svelte.js';

  const screens = [['chat', 'Chat'], ['threads', 'Threads'], ['models', 'Models'], ['hermes', 'Hermes']];
  const themes = [['system', 'Auto'], ['light', 'Light'], ['dark', 'Dark']];
</script>

<header>
  <span class="display word">Peregrine</span>
  <nav class="segmented" aria-label="Main">
    {#each screens as [key, label]}
      <button aria-current={app.screen === key ? 'page' : undefined} onclick={() => (app.screen = key)}>
        {#if key === 'hermes'}<span class="dot" class:on={app.online} aria-hidden="true"></span>{/if}{label}
      </button>
    {/each}
  </nav>
  <div class="segmented" role="group" aria-label="Appearance">
    {#each themes as [key, label]}
      <button aria-pressed={app.theme === key} onclick={() => setTheme(key)}>{label}</button>
    {/each}
  </div>
</header>

<style>
  header {
    padding: var(--sp-5) var(--sp-6) 0; display: flex; flex-wrap: wrap; align-items: center;
    justify-content: space-between; gap: var(--sp-4);
  }
  .word { font-size: 20px; letter-spacing: -0.02em; }
  nav button { font-size: 14px; display: flex; align-items: center; gap: var(--sp-2); }
  .dot { width: 8px; height: 8px; border-radius: var(--r-pill); background: var(--muted); }
  .dot.on { background: #1a9e55; }
</style>
