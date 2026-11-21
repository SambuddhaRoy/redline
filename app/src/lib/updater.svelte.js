// Updates from GitHub Releases (tauri-plugin-updater). The release's latest.json names the new installer and
// its signature; the app refuses anything not signed with Redline's key.
import { inApp, stopBackend } from './shell.js';

export const update = $state({ version: null, notes: '', progress: null, error: '' });
let pending = null; // the plugin's Update object (kept out of $state: it's a class instance)

export async function checkForUpdate() {
  if (!inApp) return;
  try {
    const { check } = await import('@tauri-apps/plugin-updater');
    pending = await check();
    if (pending) {
      update.version = pending.version;
      update.notes = pending.body ?? '';
    }
  } catch {
    // offline, or no release published yet: nothing to offer
  }
}

export async function installUpdate() {
  if (!pending) return;
  let total = 0;
  let got = 0;
  update.error = '';
  update.progress = 0;
  try {
    await pending.download((ev) => {
      if (ev.event === 'Started') total = ev.data.contentLength ?? 0;
      else if (ev.event === 'Progress') {
        got += ev.data.chunkLength;
        update.progress = total ? got / total : null;
      }
    });
    await stopBackend(); // free the GPU; the new version copies its backend into WSL when it starts
    await pending.install(); // the installer closes Redline, updates it in place and starts it again
    const { relaunch } = await import('@tauri-apps/plugin-process');
    await relaunch();
  } catch (e) {
    update.error = e?.message ?? String(e);
    update.progress = null;
  }
}
