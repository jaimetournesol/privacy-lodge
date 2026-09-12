//! Application state: the single source of truth the UI polls via `get_status`.
//!
//! Everything lives behind one `Mutex` (`AppState`). Minimal non-secret facts
//! are persisted to `<app_data_dir>/box.json`; secrets (recovery phrase and
//! connect token) go to `<app_data_dir>/secrets.json` with 0600 perms.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::{AppHandle, Manager};
use zeroize::Zeroize;

#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Fresh,
    SettingUp,
    Running,
    Stopped,
    Error,
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SetupStage {
    StartingServices,
    MintingAddress,
    Ready,
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ServiceState {
    Starting,
    Healthy,
    Stopped,
    Error,
}

#[derive(Clone, Serialize)]
pub struct Service {
    pub name: &'static str,
    pub state: ServiceState,
}

/// The exact shape the frontend polls. Field names match the shared contract.
#[derive(Clone, Serialize)]
pub struct Status {
    pub phase: Phase,
    pub onion: Option<String>,
    pub demo_mode: bool,
    pub setup_stage: Option<SetupStage>,
    pub services: Vec<Service>,
    pub people_count: u32,
    pub paired_count: u32,
    pub box_name: String,
}

pub struct Inner {
    pub phase: Phase,
    pub onion: Option<String>,
    pub demo_mode: bool,
    pub setup_stage: Option<SetupStage>,
    pub homeserver: ServiceState,
    pub tor: ServiceState,
    /// coturn (1:1 voice). Optional sidecar: `Stopped` if the binary is absent
    /// or the box is running without voice — never blocks the box.
    pub voice: ServiceState,
    pub people_count: u32,
    pub paired_count: u32,
    pub box_name: String,
    pub username: String,
    pub created: String,
    /// Six-word recovery phrase, empty until `begin_setup`.
    pub phrase: Vec<String>,
    /// Hex pairing token embedded in the connect QR, empty until `begin_setup`.
    pub token: String,
    /// coturn long-term auth secret; the homeserver signs short-lived TURN
    /// credentials with it. Generated at `begin_setup`, persisted with the
    /// other secrets. Empty until then.
    pub turn_secret: String,
    /// Homeserver registration token. Gates registration (so the box is never
    /// open-reg); used to create the admin on first run and shared by the owner
    /// to add more people. Generated at `begin_setup`.
    pub join_token: String,
    /// LiveKit SFU API key (16 random bytes, hex). Shared by livekit-server and
    /// lk-jwt so the JWTs lk-jwt mints are accepted by the SFU. Generated at
    /// `begin_setup`, persisted with the other secrets. Empty until then.
    pub livekit_api_key: String,
    /// LiveKit SFU API secret (32 random bytes, hex). The signing secret paired
    /// with `livekit_api_key`. Generated at `begin_setup`. Empty until then.
    pub livekit_api_secret: String,
    /// The admin account's password. Privacy Lodge uses password login between the
    /// phone and the box, so — unlike a multi-device server — the box persists its
    /// own admin password (single-user appliance) so the owner's phone can sign in.
    /// Empty until `begin_setup`.
    pub admin_password: String,
    /// True once the admin account has been created on the homeserver. Gates BOTH the
    /// registration token in tuwunel.toml and the create_admin call: while false (fresh
    /// box) registration is token-open so the admin can be made; once true every
    /// subsequent bring-up renders registration OFF and skips create_admin, so the box
    /// isn't a standing (token-gated) registration server after setup. Persisted in
    /// box.json (non-secret). (Security review #2, 2026-07-24.)
    pub admin_created: bool,
}

impl Default for Inner {
    fn default() -> Self {
        Self {
            phase: Phase::Fresh,
            onion: None,
            demo_mode: false,
            setup_stage: None,
            homeserver: ServiceState::Stopped,
            tor: ServiceState::Stopped,
            voice: ServiceState::Stopped,
            people_count: 0,
            paired_count: 0,
            box_name: String::new(),
            username: String::new(),
            created: String::new(),
            phrase: Vec::new(),
            token: String::new(),
            turn_secret: String::new(),
            join_token: String::new(),
            livekit_api_key: String::new(),
            livekit_api_secret: String::new(),
            admin_password: String::new(),
            admin_created: false,
        }
    }
}

impl Inner {
    pub fn status(&self) -> Status {
        Status {
            phase: self.phase,
            onion: self.onion.clone(),
            demo_mode: self.demo_mode,
            setup_stage: self.setup_stage,
            services: vec![
                Service { name: "homeserver", state: self.homeserver },
                Service { name: "tor", state: self.tor },
                Service { name: "voice", state: self.voice },
            ],
            people_count: self.people_count,
            paired_count: self.paired_count,
            box_name: self.box_name.clone(),
        }
    }
}

#[derive(Default)]
pub struct AppState(pub Mutex<Inner>);

/// One-line tray summary derived from phase. Plain, calm, no jargon.
fn tray_line(inner: &Inner) -> String {
    match inner.phase {
        Phase::Fresh => "Privacy Lodge — not set up yet".to_string(),
        Phase::SettingUp => "Privacy Lodge — setting up your box…".to_string(),
        Phase::Running => "Privacy Lodge — running, people can reach you".to_string(),
        Phase::Stopped => "Privacy Lodge — paused, your box is offline".to_string(),
        Phase::Error => "Privacy Lodge — something needs attention".to_string(),
    }
}

/// Mutate state, then refresh the tray status line (outside the lock).
pub fn update<F: FnOnce(&mut Inner)>(app: &AppHandle, f: F) {
    let text = {
        let state = app.state::<AppState>();
        let mut guard = state.0.lock().expect("state mutex poisoned");
        f(&mut guard);
        tray_line(&guard)
    };
    crate::tray::set_status_text(app, &text);
}

/// Read state without mutating it.
pub fn read<T, F: FnOnce(&Inner) -> T>(app: &AppHandle, f: F) -> T {
    let state = app.state::<AppState>();
    let guard = state.0.lock().expect("state mutex poisoned");
    f(&guard)
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Default)]
struct PersistedBox {
    box_name: String,
    username: String,
    created: String,
    onion: Option<String>,
    /// Whether the admin account exists yet (gates registration + create_admin). Defaults
    /// false for box.json written before this field existed; such a box self-heals on its
    /// next bring-up (create_admin sees M_USER_IN_USE, sets this true), then registration is
    /// off from the following start on.
    #[serde(default)]
    admin_created: bool,
}

#[derive(Serialize, Deserialize, Default)]
struct PersistedSecrets {
    phrase: Vec<String>,
    token: String,
    #[serde(default)]
    turn_secret: String,
    #[serde(default)]
    join_token: String,
    #[serde(default)]
    livekit_api_key: String,
    #[serde(default)]
    livekit_api_secret: String,
    #[serde(default)]
    admin_password: String,
}

/// On-disk envelope for `secrets.json` (review finding H2). v2 wraps the
/// `PersistedSecrets` JSON encrypted with AES-256-GCM (see [`crate::crypto`]);
/// `key_source` records which master-key source to decrypt with. v1 (legacy) had
/// no version field — the bare `PersistedSecrets` JSON in cleartext — and is
/// auto-migrated to v2 on first load.
#[derive(Serialize, Deserialize)]
struct SecretsFile {
    secrets_version: u32,
    key_source: String,
    enc: String,
}

const SECRETS_VERSION: u32 = 2;

/// Outcome of reading `secrets.json`.
enum SecretsLoad {
    /// Decrypted v2 envelope.
    Ok(PersistedSecrets),
    /// Cleartext v1 file — caller migrates it to v2.
    Legacy(PersistedSecrets),
    /// No file yet (fresh box) or unrecognisable contents — load empty.
    Missing,
    /// v2 envelope present but undecryptable (missing/wrong key, tampered).
    Error(String),
}

fn load_secrets(dir: &std::path::Path) -> SecretsLoad {
    let Ok(raw) = std::fs::read_to_string(dir.join("secrets.json")) else {
        return SecretsLoad::Missing;
    };
    // v2 encrypted envelope?
    if let Ok(file) = serde_json::from_str::<SecretsFile>(&raw) {
        if file.secrets_version >= SECRETS_VERSION {
            let Some(source) = crate::crypto::KeySource::parse(&file.key_source) else {
                return SecretsLoad::Error(format!("unknown key_source '{}'", file.key_source));
            };
            let mut key = match crate::crypto::key_for_decrypt(source) {
                Ok(k) => k,
                Err(e) => return SecretsLoad::Error(e),
            };
            let plaintext = crate::crypto::decrypt(&file.enc, &key);
            key.zeroize();
            return match plaintext {
                Ok(p) => match serde_json::from_str::<PersistedSecrets>(&p) {
                    Ok(s) => SecretsLoad::Ok(s),
                    Err(e) => SecretsLoad::Error(format!("decrypted secrets not valid JSON: {e}")),
                },
                Err(e) => SecretsLoad::Error(e),
            };
        }
    }
    // Legacy cleartext PersistedSecrets (v1, no version field).
    match serde_json::from_str::<PersistedSecrets>(&raw) {
        Ok(s) => SecretsLoad::Legacy(s),
        Err(_) => SecretsLoad::Missing,
    }
}

/// Rename (0.2.0): the identifier-derived app-data dir moved from `…/ai.tournesol.pureprivacy`
/// to `…/ai.tournesol.privacylodge`. A native box that already has data under the old dir keeps
/// using it — nothing is moved — so its onion key, DB and secrets stay exactly where they are.
/// A fresh install (neither exists) gets the new dir. Only the platform-derived path is
/// affected: Docker and the testbed pass an explicit data dir and never come through here.
fn legacy_or(new: PathBuf) -> PathBuf {
    if new.exists() {
        return new;
    }
    if let Some(parent) = new.parent() {
        let legacy = parent.join("ai.tournesol.pureprivacy");
        if legacy.is_dir() {
            eprintln!(
                "[privacy-lodge] using the pre-rename data dir {} (nothing was moved)",
                legacy.display()
            );
            return legacy;
        }
    }
    new
}

pub fn app_data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    // Per-instance override (env `PRIVACY_LODGE_DATA_DIR`) so two boxes can run on
    // one host with separate state (tor onion keys, tuwunel db, secrets). Unset in
    // production, where Tauri's identifier-derived app-data dir is used.
    let dir = match crate::envcompat::var("DATA_DIR") {
        Ok(d) if !d.is_empty() => PathBuf::from(d),
        _ => legacy_or(
            app.path()
                .app_data_dir()
                .map_err(|e| format!("couldn't resolve app data dir: {e}"))?,
        ),
    };
    std::fs::create_dir_all(&dir).map_err(|e| format!("couldn't create app data dir: {e}"))?;
    // The data dir holds secrets.json, the tor onion keys, and the tuwunel db —
    // owner-only (single-user appliance), so lock it down to 0700.
    set_0700(&dir);
    Ok(dir)
}

#[cfg(unix)]
fn set_0600(path: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn set_0600(_path: &std::path::Path) {}

#[cfg(unix)]
fn set_0700(path: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
}

#[cfg(not(unix))]
fn set_0700(_path: &std::path::Path) {}

fn write_private(path: &std::path::Path, contents: &str) -> Result<(), String> {
    // Atomic write (review CRITICAL): write a sibling temp (created 0600 BEFORE any
    // bytes, so secrets are never briefly world-readable), fsync it, then rename(2)
    // over the target — atomic on the same filesystem. A crash / power-loss / OOM-kill
    // mid-write can therefore never leave a truncated secrets.json, which would read as
    // "Missing" → an empty admin_password → a permanent, unrecoverable box lockout (that
    // password is the only login credential and nobody can reset it). The v1→v2 secrets
    // migration, which rewrites the sole cleartext copy in place, is the exact moment
    // this protects. Mirrors the temp+rename pairing.rs already uses for pairings.json.
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let tmp = path.with_extension(format!(
        "tmp.{}.{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    let write_tmp = || -> std::io::Result<()> {
        use std::io::Write;
        #[cfg(unix)]
        let mut f = {
            use std::os::unix::fs::OpenOptionsExt;
            std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&tmp)?
        };
        #[cfg(not(unix))]
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(contents.as_bytes())?;
        f.sync_all()?; // durable on disk before the rename
        Ok(())
    };
    if let Err(e) = write_tmp() {
        let _ = std::fs::remove_file(&tmp);
        return Err(format!("couldn't write {}: {e}", tmp.display()));
    }
    std::fs::rename(&tmp, path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("couldn't finalize {}: {e}", path.display())
    })?;
    set_0600(path); // rename preserves the temp's mode; explicit for non-unix/clarity
    Ok(())
}

/// Persist box.json + secrets.json from current state.
pub fn persist(app: &AppHandle) -> Result<(), String> {
    let dir = app_data_dir(app)?;
    let (boxed, secrets) = read(app, |inner| {
        (
            PersistedBox {
                box_name: inner.box_name.clone(),
                username: inner.username.clone(),
                created: inner.created.clone(),
                onion: inner.onion.clone(),
                admin_created: inner.admin_created,
            },
            PersistedSecrets {
                phrase: inner.phrase.clone(),
                token: inner.token.clone(),
                turn_secret: inner.turn_secret.clone(),
                join_token: inner.join_token.clone(),
                livekit_api_key: inner.livekit_api_key.clone(),
                livekit_api_secret: inner.livekit_api_secret.clone(),
                admin_password: inner.admin_password.clone(),
            },
        )
    });
    write_private(
        &dir.join("box.json"),
        &serde_json::to_string_pretty(&boxed).map_err(|e| e.to_string())?,
    )?;
    // Encrypt the whole secrets envelope at rest (H2): the PersistedSecrets JSON —
    // admin password, recovery phrase, TURN secret, registration token, LiveKit
    // keys — is AES-256-GCM'd with a master key held outside the data dir. box.json
    // holds no secrets and stays cleartext. The native daemons' own config files
    // (tuwunel.toml, turnserver.conf, livekit.yaml) must stay 0600 plaintext —
    // they read them directly and can't decrypt — so this protects the one copy we
    // control, not the daemon configs.
    let mut plaintext = serde_json::to_string(&secrets).map_err(|e| e.to_string())?;
    let (mut key, source) = crate::crypto::key_for_encrypt();
    let enc = crate::crypto::encrypt(&plaintext, &key);
    plaintext.zeroize();
    key.zeroize();
    let file = SecretsFile {
        secrets_version: SECRETS_VERSION,
        key_source: source.as_str().to_string(),
        enc: enc?,
    };
    write_private(
        &dir.join("secrets.json"),
        &serde_json::to_string_pretty(&file).map_err(|e| e.to_string())?,
    )?;
    Ok(())
}

/// Reset all in-memory state back to a fresh, unconfigured box (after a wipe).
pub fn reset_to_fresh(app: &AppHandle) {
    update(app, |inner| *inner = Inner::default());
}

/// Load persisted state at launch. If box.json exists the box was set up
/// before, so we come up in `stopped` (the user explicitly starts it).
pub fn load_persisted(app: &AppHandle) {
    let Ok(dir) = app_data_dir(app) else { return };
    let Ok(raw) = std::fs::read_to_string(dir.join("box.json")) else { return };
    let Ok(boxed) = serde_json::from_str::<PersistedBox>(&raw) else { return };

    // Decrypt the v2 envelope, or read-then-migrate a legacy v1 cleartext file.
    let loaded = load_secrets(&dir);
    let was_legacy = matches!(loaded, SecretsLoad::Legacy(_));
    let (secrets, decrypt_err) = match loaded {
        SecretsLoad::Ok(s) | SecretsLoad::Legacy(s) => (s, None),
        SecretsLoad::Missing => (PersistedSecrets::default(), None),
        SecretsLoad::Error(e) => (PersistedSecrets::default(), Some(e)),
    };

    update(app, |inner| {
        // A box whose secrets won't decrypt comes up in `Error`, not `Stopped` —
        // it must not spawn daemons with empty credentials.
        inner.phase = if decrypt_err.is_some() { Phase::Error } else { Phase::Stopped };
        inner.box_name = boxed.box_name;
        inner.username = boxed.username;
        inner.created = boxed.created;
        inner.onion = boxed.onion;
        inner.admin_created = boxed.admin_created;
        inner.phrase = secrets.phrase;
        inner.token = secrets.token;
        inner.turn_secret = secrets.turn_secret;
        inner.join_token = secrets.join_token;
        inner.livekit_api_key = secrets.livekit_api_key;
        inner.livekit_api_secret = secrets.livekit_api_secret;
        inner.admin_password = secrets.admin_password;
        inner.paired_count = crate::pairing::onions(&dir).len() as u32;
    });

    if let Some(e) = decrypt_err {
        eprintln!(
            "[privacy-lodge][state] secrets.json could not be decrypted: {e}. The box is in an error \
             state — provide the right PRIVACY_LODGE_SECRETS_KEY or restore the OS keychain."
        );
        return;
    }
    // One-shot upgrade of a legacy cleartext secrets.json to the encrypted v2 form.
    if was_legacy {
        match persist(app) {
            Ok(()) => eprintln!("[privacy-lodge][state] migrated secrets.json to encrypted at-rest (v2)."),
            Err(e) => eprintln!("[privacy-lodge][state] failed to migrate secrets.json to v2: {e}"),
        }
    }
}
