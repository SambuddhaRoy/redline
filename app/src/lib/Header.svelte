<script>
  import { fade } from 'svelte/transition';
  import { app, setTheme } from './app.svelte.js';
  import { update, installUpdate } from './updater.svelte.js';

  const screens = [['chat', 'chat'], ['threads', 'threads'], ['models', 'models'], ['hermes', 'hermes']];
  const themes = [['system', 'auto'], ['light', 'light'], ['dark', 'dark']];
</script>

<header>
  <nav class="segmented" aria-label="Main">
    <span class="word">redline</span>
    {#each screens as [key, label]}
      <button aria-current={app.screen === key ? 'page' : undefined} onclick={() => (app.screen = key)}>
        {label}{#if key === 'hermes'}<span class="dot" class:on={app.online} aria-hidden="true"></span>{/if}
      </button>
    {/each}
  </nav>
  <div class="right segmented">
    {#if update.version}
      <button class="update" in:fade={{ duration: 400 }} disabled={update.progress !== null} onclick={installUpdate} title={update.error || update.notes}>
        {#if update.progress !== null}updating {Math.round(update.progress * 100)}%{:else if update.error}update failed, retry{:else}update to {update.version}{/if}
      </button>
    {/if}
    <span role="group" aria-label="Appearance" class="themes">
      {#each themes as [key, label]}
        <button aria-pressed={app.theme === key} onclick={() => setTheme(key)}>{label}</button>
      {/each}
    </span>
  </div>
</header>

<style>
  header {
    min-height: 56px; padding: 0 var(--pad); display: flex; flex-wrap: wrap; align-items: center;
    justify-content: space-between; gap: 0 var(--sp-5);
  }
  nav { font-size: 26px; }
  nav button { font-size: 26px; display: inline-flex; align-items: center; gap: var(--sp-2); }
  .word { font-size: 26px; font-weight: 400; margin-right: var(--sp-3); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-2); transition: background-color 0.4s; }
  .dot.on { background: var(--accent); }
  .right { font-size: 16px; gap: var(--sp-5); }
  .themes { display: flex; gap: var(--sp-3); }
  .themes button { font-size: 16px; }
  .update { font-size: 16px; color: var(--accent) !important; }
</style>
