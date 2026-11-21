<script>
  import { scale } from 'svelte/transition';
  import { elasticOut } from 'svelte/easing';
  import { app, setTheme } from './app.svelte.js';
  import { update, installUpdate } from './updater.svelte.js';

  const screens = [['chat', 'Chat'], ['threads', 'Threads'], ['models', 'Models'], ['hermes', 'Hermes']];
  const themes = [['system', 'Auto'], ['light', 'Light'], ['dark', 'Dark']];
</script>

<header>
  <span class="display word">Redline</span>
  <nav class="segmented" aria-label="Main">
    {#each screens as [key, label]}
      <button aria-current={app.screen === key ? 'page' : undefined} onclick={() => (app.screen = key)}>
        {#if key === 'hermes'}<span class="dot" class:on={app.online} aria-hidden="true"></span>{/if}{label}
      </button>
    {/each}
  </nav>
  <div class="right">
    {#if update.version}
      <button class="pill primary update" in:scale={{ start: 0.5, duration: 900, easing: elasticOut }}
        disabled={update.progress !== null} onclick={installUpdate} title={update.error || update.notes}>
        {#if update.progress !== null}
          Updating {Math.round(update.progress * 100)}%
        {:else if update.error}
          Update failed, retry
        {:else}
          Update to {update.version}
        {/if}
      </button>
    {/if}
    <div class="segmented" role="group" aria-label="Appearance">
      {#each themes as [key, label]}
        <button aria-pressed={app.theme === key} onclick={() => setTheme(key)}>{label}</button>
      {/each}
    </div>
  </div>
</header>

<style>
  header {
    padding: var(--sp-5) var(--sp-6) var(--sp-3); display: flex; flex-wrap: wrap; align-items: center;
    justify-content: space-between; gap: var(--sp-4);
  }
  .word { font-size: 20px; letter-spacing: -0.02em; }
  nav button { font-size: 14px; display: flex; align-items: center; gap: var(--sp-2); }
  .dot { width: 8px; height: 8px; border-radius: var(--r-pill); background: var(--muted); transition: background-color 0.4s; }
  .dot.on { background: #1a9e55; animation: pulse 2.4s ease-in-out infinite; }
  @keyframes pulse { 50% { box-shadow: 0 0 0 4px rgb(26 158 85 / 0.2); } }
  .right { display: flex; align-items: center; gap: var(--sp-3); }
</style>
