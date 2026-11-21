// App-wide state: which screen, theme, live backend status, chat threads.
import { api } from './api.js';
import { loadThreads, saveThreads } from './shell.js';

const savedTheme = () => {
  try { return localStorage.getItem('theme') || 'system'; } catch { return 'system'; }
};

export const app = $state({
  screen: 'setup', // setup | chat | threads | models | hermes
  theme: savedTheme(),
  status: null, // GET /manage/status, refreshed while the app runs
  online: false,
  threads: [],
  threadId: null,
  queued: '', // a first message typed during setup, sent once the model is ready
});

export function setTheme(t) {
  app.theme = t;
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('theme', t); } catch {}
}

let timer;
export function startPolling() {
  if (timer) return;
  const tick = async () => {
    try {
      app.status = await api.status();
      app.online = true;
    } catch {
      app.online = false;
    }
  };
  tick();
  timer = setInterval(tick, 1500);
}

/** Resolves once the gateway answers, or rejects after `ms`. */
export async function waitOnline(ms = 120000) {
  startPolling();
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      app.status = await api.status();
      app.online = true;
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
  throw new Error("The Peregrine engine didn't start. Try Repair in setup.");
}

export async function initThreads() {
  app.threads = await loadThreads();
  app.threadId = app.threads[0]?.id ?? null;
}

let saveTimer;
export function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveThreads($state.snapshot(app.threads)), 300);
}

export function newThread(model = null) {
  const t = { id: crypto.randomUUID(), title: 'New thread', model, messages: [], updated: Date.now() };
  app.threads.unshift(t);
  app.threadId = t.id;
  persist();
  return app.threads[0]; // the reactive copy: edits to the raw `t` wouldn't redraw anything
}

export const currentThread = () => app.threads.find((t) => t.id === app.threadId) ?? null;
