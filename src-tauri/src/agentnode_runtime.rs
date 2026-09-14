//! Native Lodge owns the Docker runtime; agents never receive the Docker socket.
use std::{path::PathBuf, sync::OnceLock};
use serde::Serialize;
use tauri::{AppHandle, Manager};
use crate::agentnode_install::{Runtime, Progress};
static BASE: OnceLock<PathBuf> = OnceLock::new();
static NATIVE: OnceLock<Runtime> = OnceLock::new();

pub fn init(app: &AppHandle) -> Result<(), String> {
    let root = crate::state::app_data_dir(app)?;
    let _ = BASE.set(root.clone());
    if !container_box() {
        let context = app.path().resolve("agentnode-runtime", tauri::path::BaseDirectory::Resource).map_err(|e| e.to_string())?;
        let _ = NATIVE.set(Runtime::new(root, context, crate::config::off()));
    }
    Ok(())
}
fn container_box() -> bool { std::path::Path::new("/.dockerenv").exists() }
pub fn base() -> PathBuf {
    BASE.get().cloned().unwrap_or_else(|| PathBuf::from(crate::envcompat::var("DATA_DIR").unwrap_or("/data".into())))
}
pub fn handoff() -> PathBuf {
    if container_box() && std::path::Path::new("/handoff").is_dir() { PathBuf::from("/handoff/agentnode") }
    else { base().join("agentnode-handoff") }
}

#[derive(Serialize)]
pub struct RuntimeStatus {
    pub installed: bool, pub conductor: bool, pub worker: bool,
    #[serde(flatten)] pub progress: Progress,
}
#[tauri::command]
pub async fn agentnode_status() -> Result<RuntimeStatus, String> {
    let Some(runtime) = NATIVE.get() else {
        return Ok(RuntimeStatus { installed: handoff().join("runtime.json").is_file(), conductor: false, worker: false,
            progress: Progress { stage: "Managed by this box's Docker host".into(), error: None } });
    };
    let mut progress = runtime.progress();
    let conductor = runtime.running("conductor").await;
    let worker = runtime.running("worker").await;
    if progress.error.is_none() {
        progress.error = conductor.as_ref().err().or_else(|| worker.as_ref().err()).cloned();
    }
    Ok(RuntimeStatus { installed: runtime.handoff().join("runtime.json").is_file(), conductor: conductor.unwrap_or(false), worker: worker.unwrap_or(false), progress })
}

#[tauri::command]
pub async fn install_agentnode(app: AppHandle) -> Result<RuntimeStatus, String> {
    let runtime = NATIVE.get().ok_or("Manage this Docker box with pl-box agents on on its host.")?;
    runtime.install(|| crate::state::read(&app, |inner| !inner.box_name.is_empty() && inner.phase != crate::state::Phase::Fresh)).await?;
    agentnode_status().await
}

/// Reset retains the returned lock until local state/files are erased.
pub async fn stop(erase: bool) -> Result<Option<tokio::sync::MutexGuard<'static, ()>>, String> {
    match NATIVE.get() { Some(runtime) => Ok(Some(runtime.stop(erase).await?)), None => Ok(None) }
}

#[tauri::command]
pub async fn open_conductor(app: AppHandle) -> Result<(), String> {
    let token = std::fs::read_to_string(handoff().join("browser-token")).map_err(|_| "Set up Agentnode first.".to_string())?;
    let mut url = reqwest::Url::parse(&format!("http://127.0.0.1:{}/", 8787 + crate::config::off())).map_err(|e| e.to_string())?;
    url.query_pairs_mut().append_pair("lodge", "1").append_pair("surface_offset", &crate::config::off().to_string());
    url.set_fragment(Some(&format!("token={}", token.trim())));
    use tauri_plugin_opener::OpenerExt;
    app.opener().open_url(url.to_string(), None::<String>).map_err(|e| e.to_string())
}

pub fn start_background(app: &AppHandle) {
    if !container_box() {
        let app = app.clone();
        tauri::async_runtime::spawn(async move { let _ = install_agentnode(app).await; });
    }
}
