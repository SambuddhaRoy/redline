<script>
  import '@fontsource-variable/bricolage-grotesque';
  import '@fontsource-variable/inter';
  import '@fontsource/jetbrains-mono/500.css';
  import '@fontsource/jetbrains-mono/700.css';
  import '$lib/tokens.css';
  import { app, initThreads } from '$lib/app.svelte.js';
  import { setAccent } from '$lib/api.js';
  import Header from '$lib/Header.svelte';
  import Setup from '$lib/Setup.svelte';
  import Chat from '$lib/Chat.svelte';
  import Threads from '$lib/Threads.svelte';
  import Library from '$lib/Library.svelte';
  import Hermes from '$lib/Hermes.svelte';

  initThreads();

  // Nothing fails silently: any uncaught error shows in a bar at the bottom.
  let crash = $state('');
  const show = (m) => (crash = String(m?.stack ?? m?.message ?? m));
  window.addEventListener('error', (e) => show(e.error ?? e.message));
  window.addEventListener('unhandledrejection', (e) => show(e.reason));

  // Chat, Models and Setup set the accent from the model in focus; elsewhere it follows the loaded model.
  $effect(() => {
    if (!['chat', 'models', 'setup'].includes(app.screen)) setAccent(app.status?.engine?.repo);
  });
</script>

<div class="app">
  {#if app.screen === 'setup'}
    <Setup />
  {:else}
    <Header />
    {#if app.screen === 'chat'}<Chat />
    {:else if app.screen === 'threads'}<Threads />
    {:else if app.screen === 'models'}<Library />
    {:else if app.screen === 'hermes'}<main class="page"><Hermes /></main>
    {/if}
  {/if}
  {#if crash}
    <div class="crash" role="alert">
      <pre class="mono">{crash}</pre>
      <button class="pill" onclick={() => (crash = '')}>Dismiss</button>
    </div>
  {/if}
</div>

<style>
  .app { min-height: 100vh; display: flex; flex-direction: column; }
  .crash {
    position: fixed; left: var(--sp-4); right: var(--sp-4); bottom: var(--sp-4); display: flex; gap: var(--sp-4);
    align-items: flex-start; padding: var(--sp-4); border-radius: var(--r-md); background: var(--ink); color: var(--bg);
  }
  .crash pre { flex: 1; margin: 0; font-size: 12px; white-space: pre-wrap; max-height: 160px; overflow: auto; }
  .crash .pill { border-color: var(--bg); }
  .page { width: 100%; max-width: 860px; margin: 0 auto; padding: var(--sp-7) var(--sp-6); }
</style>
