<script>
  // The "working" indicator: a small Redline tachometer whose needle revs, plus a live timer and, while
  // the model reasons, the tail of its latest thought. Revs harder while warming up the engine.
  let { label = 'Thinking', since = Date.now(), peek = '', warming = false } = $props();
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 250);
    return () => clearInterval(id);
  });
  const secs = $derived(Math.max(0, Math.floor((now - since) / 1000)));
  const clock = $derived(secs < 60 ? secs + 's' : Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0'));
  const tail = $derived(peek.replace(/\s+/g, ' ').trim().slice(-90));
</script>

<div class="thinking" class:warming role="status" aria-live="polite">
  <svg class="gauge" viewBox="0 0 64 64" aria-hidden="true">
    <path d="M 17.9 46.1 A 20 20 0 1 1 49.3 22" class="dial" />
    <path d="M 50.3 23.9 A 20 20 0 0 1 46.1 46.1" class="red" />
    <g class="needle"><line x1="32" y1="35" x2="47" y2="33" /></g>
    <circle cx="32" cy="35" r="4" class="hub" />
  </svg>
  <span class="label">{label}</span>
  <span class="num muted clock">{clock}</span>
  {#if tail}<span class="peek muted">{tail}</span>{/if}
</div>

<style>
  .thinking { display: flex; align-items: center; gap: var(--sp-3); min-height: 40px; min-width: 0; }
  .gauge { width: 40px; height: 40px; flex: none; }
  .dial, .red { fill: none; stroke-width: 7; }
  .dial { stroke: var(--accent); }
  .red { stroke: #ff3b1f; }
  .hub { fill: var(--ink); }
  .needle line { stroke: var(--ink); stroke-width: 4; stroke-linecap: round; }
  .needle { transform-origin: 32px 35px; animation: rev 1.6s var(--spring) infinite; }
  .warming .needle { animation-duration: 0.9s; }
  /* Blip the throttle: sweep up toward the red, overshoot, fall back. */
  @keyframes rev {
    0% { transform: rotate(-150deg); }
    45% { transform: rotate(8deg); }
    60% { transform: rotate(-14deg); }
    100% { transform: rotate(-150deg); }
  }
  .label { font-weight: 300; font-size: 20px; color: var(--accent); animation: breathe 1.6s ease-in-out infinite; }
  @keyframes breathe { 50% { opacity: 0.55; } }
  .clock { font-size: 13px; }
  .peek { min-width: 0; font-size: 13px; font-style: italic; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
</style>
