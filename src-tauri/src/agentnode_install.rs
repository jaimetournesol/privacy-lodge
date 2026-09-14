//! Host-owned Docker installation. Nothing inside an agent gets a Docker socket.
use std::{collections::BTreeMap, path::{Path, PathBuf}, process::Stdio, sync::Mutex as StateMutex, time::Duration};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::{process::Command, sync::{watch, Mutex, MutexGuard}};

const OWNER: &str = "ai.tournesol.privacylodge.owner";
#[derive(Clone, Default, Serialize)]
pub struct Progress { pub stage: String, pub error: Option<String> }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Bundle { version: u32, digest: String, files: BTreeMap<String, String> }

pub struct Runtime {
    pub root: PathBuf,
    pub context: PathBuf,
    pub offset: u16,
    operation: Mutex<()>,
    epoch: watch::Sender<u64>,
    progress: StateMutex<Progress>,
}
impl Runtime {
    pub fn new(root: PathBuf, context: PathBuf, offset: u16) -> Self {
        Self { root, context, offset, operation: Mutex::new(()), epoch: watch::channel(0).0,
            progress: StateMutex::new(Progress::default()) }
    }
    pub fn prefix(&self) -> String { format!("privacy-lodge-{:x}", Sha256::digest(self.root.to_string_lossy().as_bytes()))[..28].to_string() }
    pub fn handoff(&self) -> PathBuf { self.root.join("agentnode-handoff") }
    pub fn progress(&self) -> Progress { self.progress.lock().unwrap_or_else(|p| p.into_inner()).clone() }
    fn stage(&self, stage: &str) { *self.progress.lock().unwrap_or_else(|p| p.into_inner()) = Progress { stage: stage.into(), error: None }; }
    fn check(&self, ticket: u64) -> Result<(), String> {
        if *self.epoch.borrow() != ticket { Err("Agent setup was cancelled.".into()) } else { Ok(()) }
    }
    pub async fn install(&self, configured: impl Fn() -> bool) -> Result<(), String> {
        let ticket = *self.epoch.borrow();
        let _operation = self.operation.lock().await;
        self.check(ticket)?;
        if !configured() { return Err("Set up Lodge before starting agents.".into()); }
        self.stage("Preparing agents");
        let result = self.install_locked(ticket).await;
        match &result {
            Ok(()) => self.stage("Ready"),
            Err(error) => *self.progress.lock().unwrap_or_else(|p| p.into_inner()) = Progress { stage: "Setup needs attention".into(), error: Some(error.clone()) },
        }
        result
    }
    async fn install_locked(&self, ticket: u64) -> Result<(), String> {
        docker(&["info", "--format", "{{.ServerVersion}}"] ).await?;
        let image = match std::env::var("PL_AGENT_IMAGE") {
            Ok(image) if !image.trim().is_empty() => {
                docker(&["image", "inspect", "--format", "{{.Id}}", &image]).await
                    .map_err(|_| "The configured Agentnode image is unavailable. Load that image or use the bundled runtime.".to_string())?;
                image
            },
            _ => {
                let digest = validate_bundle(&self.context)?;
                let image = format!("privacy-lodge-agentnode:native-{}", &digest[..24]);
                if docker(&["image", "inspect", "--format", "{{.Id}}", &image]).await.is_err() {
                    self.stage("Downloading and preparing agents. First setup can take several minutes");
                    self.build(&image, ticket).await?;
                }
                image
            },
        };
        self.check(ticket)?;
        let contract = docker(&["image", "inspect", "--format", "{{index .Config.Labels \"ai.tournesol.privacylodge.runtime-contract\"}}", &image]).await?;
        if contract.trim() != "2" { return Err("This Agentnode image is incompatible with the desktop app. Use its bundled runtime or a matching runtime image.".into()); }
        std::fs::create_dir_all(self.handoff()).map_err(|_| "Could not create the private agent connection directory.")?;
        let prefix = self.prefix();
        for (kind, name) in [("network", prefix.clone()), ("volume", format!("{prefix}-worker")),
            ("volume", format!("{prefix}-conductor")), ("volume", format!("{prefix}-peer"))] {
            self.ensure_resource(kind, &name).await?; self.check(ticket)?;
        }
        let (uid, gid) = ownership(&self.root)?;
        #[cfg(unix)] { use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(self.handoff(), std::fs::Permissions::from_mode(0o700)).map_err(|_| "Could not protect the agent connection directory.")?;
        }
        self.stage("Starting Conductor and Docker worker");
        let image_id = docker(&["image", "inspect", "--format", "{{.Id}}", &image]).await?;
        for role in ["worker", "conductor"] {
            self.check(ticket)?;
            let name = format!("{prefix}-{role}");
            if self.exists_owned("container", &name).await? {
                let installed = docker(&["container", "inspect", "--format", "{{.Image}}", &name]).await?;
                if installed.trim() == image_id.trim() { docker(&["start", &name]).await?; continue; }
                docker(&["stop", "--time", "30", &name]).await?;
                docker(&["rm", &name]).await?;
            }
            self.check(ticket)?;
            let mut args: Vec<String> = ["run", "-d", "--restart", "unless-stopped", "--label", &format!("{OWNER}={prefix}"),
                "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "SETUID", "--cap-add", "SETGID",
                "--pids-limit", "256", "--name", &name, "--network", &prefix].iter().map(|s| s.to_string()).collect();
            args.extend(["-e".into(), format!("LODGE_ROLE={}", if role == "conductor" { "control" } else { "worker" }),
                "-e".into(), format!("LODGE_RUNTIME_UID={uid}"), "-e".into(), format!("LODGE_RUNTIME_GID={gid}"),
                "-v".into(), format!("{prefix}-{role}:/data"), "-v".into(), format!("{prefix}-peer:/peer{}", if role == "conductor" { ":ro" } else { "" })]);
            if role == "worker" { args.extend(["--network-alias".into(), "agent-worker".into()]); }
            else {
                args.extend(["-v".into(), format!("{}:/handoff/agentnode", self.handoff().to_string_lossy()),
                    "-p".into(), format!("127.0.0.1:{}:8787", 8787 + self.offset)]);
                for slot in 0..=16 { args.extend(["-p".into(), format!("127.0.0.1:{}:{}", 4400 + slot + self.offset, 4400 + slot)]); }
            }
            args.push(image.clone());
            docker_owned(args).await?;
        }
        let token_file = self.handoff().join("browser-token");
        let client = reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(2)).build().map_err(|_| "Could not check agents.")?;
        for _ in 0..90 {
            self.check(ticket)?;
            if let Ok(token) = std::fs::read_to_string(&token_file) {
                if let Ok(response) = client.get(format!("http://127.0.0.1:{}/api/lodge/auth", 8787 + self.offset))
                    .header("x-agentnode-token", token.trim()).send().await {
                    if response.status().is_success() && self.running("worker").await? { return Ok(()); }
                }
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
        Err("Agents did not become ready. Check Docker and refresh their status.".into())
    }
    async fn build(&self, image: &str, ticket: u64) -> Result<(), String> {
        let mut changes = self.epoch.subscribe();
        let mut child = Command::new("docker").args(["build", "-f", "docker/Dockerfile", "-t", image, "."])
            .current_dir(&self.context).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).kill_on_drop(true)
            .spawn().map_err(|_| "Could not prepare the bundled agent runtime. Start Docker and retry.")?;
        let result = tokio::select! {
            status = child.wait() => status.map_err(|_| "Could not finish preparing agents.".to_string())
                .and_then(|status| if status.success() { Ok(()) } else { Err("Could not download or build the bundled agents. Check internet access and Docker, then retry.".into()) }),
            _ = changes.wait_for(|value| *value != ticket) => Err("Agent setup was cancelled.".into()),
            _ = tokio::time::sleep(Duration::from_secs(1200)) => Err("Preparing agents took too long. Check internet access and Docker, then retry.".into()),
        };
        if result.is_err() { let _ = child.kill().await; }
        result
    }
    async fn ensure_resource(&self, kind: &str, name: &str) -> Result<(), String> {
        if !self.exists_owned(kind, name).await? {
            docker(&[kind, "create", "--label", &format!("{OWNER}={}", self.prefix()), name]).await?;
        }
        Ok(())
    }
    async fn exists_owned(&self, kind: &str, name: &str) -> Result<bool, String> {
        let args = if kind == "container" { vec![kind, "ls", "-a", "--format", "{{.Names}}"] }
            else { vec![kind, "ls", "--format", "{{.Name}}"] };
        if !docker(&args).await?.lines().any(|line| line == name) { return Ok(false); }
        let label = if kind == "container" { format!("{{{{index .Config.Labels \"{OWNER}\"}}}}") }
            else { format!("{{{{index .Labels \"{OWNER}\"}}}}") };
        if docker(&[kind, "inspect", "--format", &label, name]).await?.trim() != self.prefix() {
            return Err("An existing Docker resource uses this box's name but is not owned by it. Resolve the name conflict before continuing.".into());
        }
        Ok(true)
    }
    pub async fn running(&self, role: &str) -> Result<bool, String> {
        let name = format!("{}-{role}", self.prefix());
        if !self.exists_owned("container", &name).await? { return Ok(false); }
        Ok(docker(&["container", "inspect", "--format", "{{.State.Running}}", &name]).await?.trim() == "true")
    }
    /// Keep this guard through any subsequent data wipe, so a queued install
    /// rechecks configuration only after the erased state has been published.
    pub async fn stop(&self, erase: bool) -> Result<MutexGuard<'_, ()>, String> {
        self.epoch.send_modify(|value| *value += 1);
        let guard = self.operation.lock().await;
        // A box that never installed agents must still be stoppable without Docker.
        if !self.handoff().exists() { self.stage(if erase { "Not installed" } else { "Stopped" }); return Ok(guard); }
        self.stage(if erase { "Removing this box's agents" } else { "Stopping agents" });
        for role in ["conductor", "worker"] {
            let name = format!("{}-{role}", self.prefix());
            if self.exists_owned("container", &name).await? {
                docker(&["stop", "--time", "30", &name]).await?;
                if erase { docker(&["rm", &name]).await?; }
            }
        }
        if erase {
            for role in ["conductor", "worker", "peer"] {
                let name = format!("{}-{role}", self.prefix());
                if self.exists_owned("volume", &name).await? { docker(&["volume", "rm", &name]).await?; }
            }
            if self.exists_owned("network", &self.prefix()).await? { docker(&["network", "rm", &self.prefix()]).await?; }
        }
        self.stage(if erase { "Not installed" } else { "Stopped" });
        Ok(guard)
    }
}
async fn docker(args: &[&str]) -> Result<String, String> { docker_owned(args.iter().map(|s| s.to_string()).collect()).await }
async fn docker_owned(args: Vec<String>) -> Result<String, String> {
    let result = tokio::time::timeout(Duration::from_secs(180), Command::new("docker").args(&args).kill_on_drop(true).output()).await
        .map_err(|_| "Docker took too long. Check Docker and retry.")?.map_err(|_| "Docker is unavailable. Start Docker and retry.")?;
    if !result.status.success() {
        #[cfg(test)] eprintln!("Docker {:?}: {}", &args[..args.len().min(2)], String::from_utf8_lossy(&result.stderr).chars().take(1200).collect::<String>());
        return Err("Docker could not complete the operation. Check Docker and its permissions, then retry.".into());
    }
    String::from_utf8(result.stdout).map_err(|_| "Unexpected Docker response.".into())
}
fn ownership(root: &Path) -> Result<(u32, u32), String> {
    #[cfg(unix)] { use std::os::unix::fs::MetadataExt;
        let meta = std::fs::metadata(root).map_err(|_| "Cannot read the box's data-directory ownership.")?;
        if meta.uid() == 0 { return Err("Run the desktop app as your normal user, not root.".into()); }
        Ok((meta.uid(), meta.gid()))
    }
    #[cfg(not(unix))] { let _ = root; Ok((1000, 1000)) }
}
fn validate_bundle(context: &Path) -> Result<String, String> {
    let bytes = std::fs::read(context.join("manifest.json")).map_err(|_| "This app is missing its bundled Agentnode runtime. Install the complete Privacy Lodge package.")?;
    if bytes.len() > 1024 * 1024 { return Err("Invalid bundled runtime manifest.".into()); }
    let bundle: Bundle = serde_json::from_slice(&bytes).map_err(|_| "Invalid bundled runtime manifest.")?;
    if bundle.version != 1 || bundle.files.is_empty() || bundle.files.len() > 4096 { return Err("Unsupported bundled runtime.".into()); }
    let mut digest = Sha256::new();
    for (name, expected) in &bundle.files {
        if name.split('/').any(|p| p.is_empty() || p == "." || p == ".." || p.contains('\\') || p.contains(':')) {
            return Err("Invalid bundled runtime path.".into());
        }
        let bytes = std::fs::read(context.join(name)).map_err(|_| "The bundled agent runtime is incomplete.")?;
        let hash = format!("{:x}", Sha256::digest(bytes));
        if &hash != expected { return Err("The bundled agent runtime failed its integrity check. Reinstall the application.".into()); }
        digest.update(name); digest.update(b"\0"); digest.update(hash); digest.update(b"\n");
    }
    let actual = format!("{:x}", digest.finalize());
    if actual != bundle.digest { return Err("The bundled runtime manifest failed its integrity check.".into()); }
    Ok(actual)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(context: &Path) {
        let mut digest = Sha256::new();
        let name = "docker/Dockerfile"; let bytes = b"FROM scratch\n";
        let hash = format!("{:x}", Sha256::digest(bytes));
        digest.update(name); digest.update(b"\0"); digest.update(&hash); digest.update(b"\n");
        std::fs::create_dir_all(context.join("docker")).unwrap();
        std::fs::write(context.join(name), bytes).unwrap();
        std::fs::write(context.join("manifest.json"), serde_json::json!({"version":1,"digest":format!("{:x}", digest.finalize()),"files":{name:hash}}).to_string()).unwrap();
    }
    #[test] fn validates_bundled_source_and_rejects_tampering() {
        let dir = tempfile::tempdir().unwrap(); fixture(dir.path());
        assert_eq!(validate_bundle(dir.path()).unwrap().len(), 64);
        std::fs::write(dir.path().join("docker/Dockerfile"), "changed").unwrap();
        assert!(validate_bundle(dir.path()).is_err());
    }
    #[test] fn manifest_cannot_read_outside_the_runtime_bundle() {
        let dir = tempfile::tempdir().unwrap();
        for path in ["../secret", "/secret", "C:/secret", "docker\\secret"] {
            std::fs::write(dir.path().join("manifest.json"), serde_json::json!({"version":1,"digest":"bad","files":{path:"bad"}}).to_string()).unwrap();
            assert!(validate_bundle(dir.path()).is_err());
        }
    }
    #[tokio::test] async fn cancelled_queued_setup_cannot_run_after_reset() {
        let dir = tempfile::tempdir().unwrap();
        let runtime = std::sync::Arc::new(Runtime::new(dir.path().into(), dir.path().into(), 0));
        let held = runtime.operation.lock().await;
        let copy = runtime.clone();
        let queued = tokio::spawn(async move { copy.install(|| panic!("stale setup reached configuration")).await });
        tokio::task::yield_now().await;
        runtime.epoch.send_modify(|value| *value += 1); drop(held);
        assert_eq!(queued.await.unwrap().unwrap_err(), "Agent setup was cancelled.");
    }

    /// Explicit local QA only: builds the bundled image, operates on temporary
    /// labeled containers/volumes, never copies provider credentials or uses Tor.
    #[tokio::test]
    #[ignore = "requires Docker and the staged runtime; creates isolated development agents"]
    async fn native_docker_install_pause_restart_reset() {
        let dir = tempfile::tempdir().unwrap();
        let context = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("agentnode-runtime");
        let runtime = Runtime::new(dir.path().into(), context, 23000);
        let prefix = runtime.prefix();
        let result: Result<(), String> = async {
            runtime.install(|| true).await?;
            if !runtime.running("conductor").await? || !runtime.running("worker").await? { return Err("Both agents must be running".into()); }
            let token = std::fs::read(runtime.handoff().join("browser-token")).map_err(|_| "Host could not read its agent handoff")?;
            let conductor = format!("{prefix}-conductor");
            let worker = format!("{prefix}-worker");
            if docker(&["exec", &conductor, "id", "-u"]).await?.trim() != "0" {
                // docker exec defaults to the image's bootstrap user; validate
                // the actual Python/Node process UID separately below.
                return Err("Unexpected bootstrap user".into());
            }
            let expected_uid = ownership(&runtime.root)?.0.to_string();
            let processes = docker(&["top", &conductor, "-eo", "pid,uid,args"]).await?;
            let agents: Vec<_> = processes.lines().filter(|l| l.contains("python -m agentnode") || l.contains("server/index.ts")).collect();
            if agents.len() < 2 || agents.iter().any(|l| l.split_whitespace().nth(1) != Some(expected_uid.as_str())) {
                return Err("Agent process has the wrong owner".into());
            }
            let mounts = docker(&["inspect", "--format", "{{json .Mounts}}", &worker]).await?;
            let mounts: serde_json::Value = serde_json::from_str(&mounts).map_err(|_| "Invalid mount metadata")?;
            if mounts.as_array().unwrap().iter().any(|m| m["Type"] != "volume") { return Err("Worker has a host bind mount".into()); }
            docker(&["exec", "--user", &expected_uid, &worker, "sh", "-c", "printf retained > /data/qa-pause-retained"]).await?;
            drop(runtime.stop(false).await?);
            if runtime.running("conductor").await? || runtime.running("worker").await? { return Err("Pause left an agent running".into()); }
            runtime.install(|| true).await?;
            if std::fs::read(runtime.handoff().join("browser-token")).map_err(|_| "Missing resumed handoff")? != token {
                return Err("Resume changed the agent identity".into());
            }
            if docker(&["exec", "--user", &expected_uid, &worker, "cat", "/data/qa-pause-retained"]).await?.trim() != "retained" { return Err("Pause lost worker data".into()); }
            Ok(())
        }.await;
        let cleanup = runtime.stop(true).await;
        let cleanup_error = cleanup.as_ref().err().cloned(); drop(cleanup);
        assert!(result.is_ok(), "{}", result.unwrap_err());
        assert!(cleanup_error.is_none(), "{}", cleanup_error.unwrap());
        for role in ["conductor", "worker", "peer"] {
            assert!(!runtime.exists_owned("volume", &format!("{prefix}-{role}")).await.unwrap());
        }
        assert!(!runtime.exists_owned("network", &prefix).await.unwrap());
    }
    #[tokio::test]
    #[ignore = "requires Docker; tests isolated resource ownership and cancellation"]
    async fn native_docker_conflicts_and_build_cancellation() {
        let dir = tempfile::tempdir().unwrap();
        let context = tempfile::tempdir().unwrap();
        let runtime = std::sync::Arc::new(Runtime::new(dir.path().into(), context.path().into(), 23020));
        let conflict = format!("{}-worker", runtime.prefix());
        docker(&["volume", "create", &conflict]).await.unwrap();
        let rejected = runtime.ensure_resource("volume", &conflict).await;
        let retained = docker(&["volume", "inspect", "--format", "{{.Name}}", &conflict]).await;
        docker(&["volume", "rm", &conflict]).await.unwrap();
        assert!(rejected.unwrap_err().contains("not owned"));
        assert_eq!(retained.unwrap().trim(), conflict);

        let base = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("agentnode-runtime");
        let base_digest = validate_bundle(&base).unwrap();
        let dockerfile = format!("FROM privacy-lodge-agentnode:native-{}\nRUN [\"node\", \"-e\", \"setTimeout(()=>{{}}, 60000)\"]\n", &base_digest[..24]);
        let hash = format!("{:x}", Sha256::digest(dockerfile.as_bytes()));
        let name = "docker/Dockerfile";
        let mut digest = Sha256::new(); digest.update(name); digest.update(b"\0"); digest.update(&hash); digest.update(b"\n");
        let digest = format!("{:x}", digest.finalize());
        std::fs::create_dir_all(context.path().join("docker")).unwrap();
        std::fs::write(context.path().join(name), dockerfile).unwrap();
        std::fs::write(context.path().join("manifest.json"), serde_json::json!({"version":1,"digest":digest,"files":{name:hash}}).to_string()).unwrap();
        let copy = runtime.clone();
        let installer = tokio::spawn(async move { copy.install(|| true).await });
        for _ in 0..100 {
            if runtime.progress().stage.starts_with("Downloading") { break; }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
        let stopping = std::time::Instant::now();
        drop(runtime.stop(false).await.unwrap());
        assert!(stopping.elapsed() < Duration::from_secs(10));
        assert_eq!(installer.await.unwrap().unwrap_err(), "Agent setup was cancelled.");
        assert!(!runtime.handoff().exists());
        assert!(!runtime.running("conductor").await.unwrap());
        assert!(!runtime.running("worker").await.unwrap());
        let image = format!("privacy-lodge-agentnode:native-{}", &digest[..24]);
        assert!(docker(&["image", "inspect", "--format", "{{.Id}}", &image]).await.is_err());
    }

}
