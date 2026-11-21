<script>
  // A model's organization avatar from Hugging Face, with the family color as backdrop and the
  // first letter as fallback while it loads or when there's none.
  import { api, familyOf } from './api.js';

  // The thumbnail shows who made the model, not who uploaded this copy: nvidia/Qwen3-8B-NVFP4 gets Qwen's logo.
  const MAKERS = {
    qwen: 'Qwen', gemma: 'google', llama: 'meta-llama', nemotron: 'nvidia', gptoss: 'openai',
    hermes: 'NousResearch', deepseek: 'deepseek-ai', mistral: 'mistralai',
  };
  let { repo, size = 40 } = $props();
  const cache = (globalThis.__avatars ??= new Map()); // owner -> Promise<url | null>, shared by every card
  const owner = $derived(MAKERS[familyOf(repo)] ?? repo.split('/')[0]);
  let url = $state(null);
  let failed = $state(false);

  $effect(() => {
    const o = owner;
    if (!cache.has(o)) cache.set(o, api.avatar(o).then((r) => r.url).catch(() => null));
    cache.get(o).then((u) => { if (o === owner) url = u; });
  });
</script>

<span class="avatar" style="width: {size}px; height: {size}px; font-size: {Math.round(size * 0.42)}px" aria-hidden="true">
  {#if url && !failed}
    <img src={url} alt="" loading="lazy" onerror={() => (failed = true)} />
  {:else}
    {owner[0]?.toUpperCase()}
  {/if}
</span>

<style>
  .avatar {
    flex: none; display: grid; place-items: center; overflow: hidden; border-radius: var(--r-sm);
    background: var(--surface); color: var(--ink); font-family: var(--display); font-weight: 800;
    box-shadow: 0 0 0 2px rgb(0 0 0 / 0.12);
  }
  img { width: 100%; height: 100%; object-fit: cover; }
</style>
