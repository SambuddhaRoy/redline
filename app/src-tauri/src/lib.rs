//! Redline's native shell (the app; the backend it drives is the Peregrine engine). Everything model-related lives in the backend (HTTP on 127.0.0.1:47800);
//! this side only runs bootstrap.ps1, owns the backend process, and stores chat threads on disk.
use std::io::{BufRead, BufReader};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::ipc::Channel;
use tauri::{AppHandle, Manager, RunEvent, State};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const COMMANDS: [&str; 4] = ["check", "install", "sync", "stop"];

#[derive(Default)]
struct Backend(Mutex<Option<Child>>);

/// bootstrap.ps1 ships as a resource next to backend/; in `tauri dev` it sits at the repo root.
fn bootstrap_path(app: &AppHandle) -> PathBuf {
    let bundled = app.path().resource_dir().map(|d| d.join("bootstrap.ps1"));
    match bundled {
        Ok(p) if p.exists() => plain(p),
        _ => PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..").join("bootstrap.ps1"),
    }
}

/// resource_dir() is a verbatim `\\?\C:\...` path; PowerShell runs a script from one but leaves
/// $PSScriptRoot empty, so bootstrap.ps1 couldn't find backend\ next to it.
fn plain(p: PathBuf) -> PathBuf {
    match p.to_str().and_then(|s| s.strip_prefix(r"\\?\")) {
        Some(rest) if !rest.starts_with("UNC\\") => PathBuf::from(rest),
        _ => p,
    }
}

fn powershell(app: &AppHandle, args: &[&str]) -> Command {
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        .arg(bootstrap_path(app))
        .args(args)
        .stdin(Stdio::null()) // a GUI app has no console stdin; PowerShell exits silently on the dead handle
        .creation_flags(CREATE_NO_WINDOW);
    cmd
}

/// Runs one bootstrap.ps1 command and forwards each JSON line it prints as it happens. Returns
/// `{code, events}` (code 0 ok, 1 error, 2 needs administrator, 3 reboot required). The events come back
/// too because channel messages can land after the command's reply, so decisions must not rely on them.
#[tauri::command]
async fn bootstrap(app: AppHandle, command: String, on_event: Channel<serde_json::Value>) -> Result<serde_json::Value, String> {
    if !COMMANDS.contains(&command.as_str()) {
        return Err(format!("unknown command {command}"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        let mut child = powershell(&app, &[&command])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| e.to_string())?;
        // Drain stderr on its own thread so a chatty error stream can't block stdout.
        let mut err_pipe = child.stderr.take().unwrap();
        let err_reader = std::thread::spawn(move || {
            let mut s = String::new();
            let _ = std::io::Read::read_to_string(&mut err_pipe, &mut s);
            s
        });
        let mut events = Vec::new();
        for line in BufReader::new(child.stdout.take().unwrap()).lines().map_while(Result::ok) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line.trim()) {
                let _ = on_event.send(v.clone());
                events.push(v);
            }
        }
        let code = child.wait().map(|s| s.code().unwrap_or(1)).map_err(|e| e.to_string())?;
        let stderr = err_reader.join().unwrap_or_default();
        let tail: String = stderr.chars().rev().take(2000).collect::<Vec<_>>().into_iter().rev().collect();
        Ok(serde_json::json!({ "code": code, "events": events, "stderr": tail, "script": bootstrap_path(&app) }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Turning WSL on needs an elevated prompt; Windows shows the UAC dialog.
#[tauri::command]
fn install_wsl(app: AppHandle) -> Result<(), String> {
    let script = bootstrap_path(&app);
    let inner = format!("-NoProfile -ExecutionPolicy Bypass -File \"{}\" install", script.display());
    Command::new("powershell.exe")
        .args(["-NoProfile", "-Command", &format!("Start-Process powershell -Verb RunAs -Wait -ArgumentList '{inner}'")])
        .creation_flags(CREATE_NO_WINDOW)
        .status()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// `bootstrap.ps1 start` blocks for the backend's lifetime, so it runs as a child we keep.
#[tauri::command]
fn start_backend(app: AppHandle, backend: State<Backend>) -> Result<(), String> {
    let mut slot = backend.0.lock().unwrap();
    if let Some(child) = slot.as_mut() {
        if child.try_wait().map_err(|e| e.to_string())?.is_none() {
            return Ok(());
        }
    }
    let child = powershell(&app, &["start"]).stdout(Stdio::null()).stderr(Stdio::null()).spawn().map_err(|e| e.to_string())?;
    *slot = Some(child);
    Ok(())
}

/// Stops the distro (gateway + trtllm-serve) and our child. Used on exit and before an update installs.
fn stop(app: &AppHandle) {
    let backend = app.state::<Backend>();
    let child = backend.0.lock().unwrap().take();
    if let Some(mut child) = child {
        let _ = powershell(app, &["stop"]).status();
        let _ = child.kill();
    }
}

#[tauri::command]
async fn stop_backend(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || stop(&app)).await.map_err(|e| e.to_string())
}

fn threads_file(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir.join("threads.json"))
}

#[tauri::command]
fn load_threads(app: AppHandle) -> Result<String, String> {
    match std::fs::read_to_string(threads_file(&app)?) {
        Ok(s) => Ok(s),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok("[]".into()),
        Err(e) => Err(e.to_string()),
    }
}

/// Write to a temp file and rename, so a crash mid-write never leaves half a history.
#[tauri::command]
fn save_threads(app: AppHandle, json: String) -> Result<(), String> {
    let path = threads_file(&app)?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, json).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &path).map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .manage(Backend::default())
        .invoke_handler(tauri::generate_handler![
            bootstrap, install_wsl, start_backend, stop_backend, load_threads, save_threads
        ])
        .build(tauri::generate_context!())
        .expect("error while building Redline")
        .run(|app, event| {
            // Closing the app frees the GPU.
            if let RunEvent::Exit = event {
                stop(app);
            }
        });
}
