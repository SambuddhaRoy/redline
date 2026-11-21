// Bridge to the Rust shell (src-tauri/src/lib.rs). In a plain browser (UI work on `npm run dev`) there is no
// shell: setup is skipped, the backend is assumed to be running, and threads live in localStorage.
import { invoke, Channel } from '@tauri-apps/api/core';

export const inApp = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

/** Runs a bootstrap.ps1 command; onEvent gets each JSON line live (for progress). Resolves to {code, events}:
 * use `events` for decisions, since live messages can arrive after the command resolves. */
export function bootstrap(command, onEvent) {
  const channel = new Channel();
  channel.onmessage = onEvent;
  return invoke('bootstrap', { command, onEvent: channel });
}

export const startBackend = () => (inApp ? invoke('start_backend') : Promise.resolve());
export const stopBackend = () => (inApp ? invoke('stop_backend') : Promise.resolve());

/** Links in answers open in the default browser: navigating the app's own webview would leave Redline. */
export async function openExternal(url) {
  if (!/^https?:\/\//i.test(url)) return;
  if (!inApp) return void window.open(url, '_blank', 'noopener');
  const { openUrl } = await import('@tauri-apps/plugin-opener');
  await openUrl(url);
}
export const installWsl = () => invoke('install_wsl');

export async function loadThreads() {
  const raw = inApp ? await invoke('load_threads') : localStorage.getItem('threads') || '[]';
  try { return JSON.parse(raw); } catch { return []; }
}
export function saveThreads(threads) {
  const json = JSON.stringify(threads);
  return inApp ? invoke('save_threads', { json }) : Promise.resolve(localStorage.setItem('threads', json));
}
