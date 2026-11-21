<script>
  import { app, persist, newThread } from './app.svelte.js';
  import { accentOf } from './api.js';

  const sorted = $derived([...app.threads].sort((a, b) => b.updated - a.updated));
  const when = (ms) => new Date(ms).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

  function open(id) {
    app.threadId = id;
    app.screen = 'chat';
  }
  function remove(id) {
    app.threads = app.threads.filter((t) => t.id !== id);
    if (app.threadId === id) app.threadId = app.threads[0]?.id ?? null;
    persist();
  }
</script>

<main>
  <div class="head">
    <h1 class="display">Threads</h1>
    <button class="pill primary" onclick={() => { newThread(); app.screen = 'chat'; }}>New thread</button>
  </div>
  {#if !sorted.length}
    <p class="muted">Nothing yet. Your conversations show up here, saved on this PC.</p>
  {/if}
  <ul>
    {#each sorted as t (t.id)}
      <li class="card">
        <span class="dot" style="background: {accentOf(t.model ?? '')}" aria-hidden="true"></span>
        <button class="open" onclick={() => open(t.id)}>
          <span class="title">{t.title}</span>
          <span class="mono muted meta">{t.model ?? 'no model'} · {Math.ceil(t.messages.length / 2)} turns · {when(t.updated)}</span>
        </button>
        <button class="pill quiet" onclick={() => remove(t.id)} aria-label="Delete {t.title}">Delete</button>
      </li>
    {/each}
  </ul>
</main>

<style>
  main { width: 100%; max-width: 860px; margin: 0 auto; padding: var(--sp-7) var(--sp-6); }
  .head { display: flex; align-items: flex-end; justify-content: space-between; gap: var(--sp-4); margin-bottom: var(--sp-5); }
  h1 { margin: 0; font-size: 64px; line-height: 0.95; }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
  li { display: flex; align-items: center; gap: var(--sp-4); padding: var(--sp-2) var(--sp-2) var(--sp-2) var(--sp-4); }
  .dot { width: 12px; height: 12px; flex: none; border-radius: var(--r-pill); }
  .open {
    flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-1); text-align: left;
    border: none; background: none; padding: var(--sp-2) 0; cursor: pointer;
  }
  .title { font-family: var(--display); font-weight: 700; font-size: 18px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 12px; }
</style>
