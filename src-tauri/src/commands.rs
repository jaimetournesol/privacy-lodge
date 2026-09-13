//! Tauri commands — the exact shared contract the frontend codes against.
//! All return Result<_, String>; arg keys arrive camelCase from JS and Tauri
//! v2 maps them onto these snake_case parameters automatically.

use rand::seq::SliceRandom;
use rand::Rng;
use serde::Serialize;
use tauri::AppHandle;

use crate::pairing;
use crate::state::{self, Phase, Status};
use crate::supervisor;
use crate::words::WORDS;

#[tauri::command]
pub fn get_status(app: AppHandle) -> Result<Status, String> {
    Ok(state::read(&app, |inner| inner.status()))
}

/// Word-dash style: "coral-armada-poem-baker-42".
#[tauri::command]
pub fn suggest_password() -> Result<String, String> {
    let mut rng = rand::thread_rng();
    let words: Vec<&str> = WORDS.choose_multiple(&mut rng, 4).copied().collect();
    let digits: u8 = rng.gen_range(10..100);
    Ok(format!("{}-{}", words.join("-"), digits))
}

/// [QW-rust e] A valid Matrix localpart: non-empty, within the spec grammar
/// `[a-z0-9._=/+-]`, and short enough that `@<localpart>:<onion>` stays under
/// Matrix's 255-byte user-id cap (a v3 onion server_name is 62 bytes, plus `@`
/// and `:`, leaving ~190 — we cap the localpart at 180 with generous headroom).
fn is_valid_localpart(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 180
        && s.bytes()
            .all(|b| matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'.' | b'_' | b'=' | b'/' | b'+' | b'-'))
}

#[tauri::command]
pub fn begin_setup(
    app: AppHandle,
    box_name: String,
    username: String,
    password: String,
) -> Result<(), String> {
    let box_name = box_name.trim().to_string();
    let username = username.trim().to_string();
    if box_name.is_empty() || box_name.len() > 1024 {
        return Err("Give Lodge a name of at most 1024 bytes.".into());
    }
    if username.is_empty() {
        return Err("Pick a username first.".into());
    }
    // [QW-rust e] Validate the username is a valid Matrix localpart BEFORE it's
    // baked into @user:onion (used for login, account-data paths, and the admin
    // registration). The Matrix spec restricts a localpart to the grammar
    // [a-z0-9._=/+-]; anything else (uppercase, spaces, @, :) yields a malformed
    // user id that registration rejects — better to reject it here with a clear
    // message than to fail opaquely mid-setup. We also bound the length so the
    // full user id stays well under Matrix's 255-byte user-id limit.
    if !is_valid_localpart(&username) {
        return Err(
            "Usernames can only use lowercase letters, numbers, and . _ = + / - (no spaces or capitals)."
                .into(),
        );
    }
    if password.trim().chars().count() < 8 || password.len() > 1024 {
        return Err("Choose a password of at least 8 characters (at most 1024 bytes).".into());
    }
    let _identity = crate::identity_transaction::lock();
    if state::read(&app, |inner| inner.phase != state::Phase::Fresh || inner.onion.is_some()) {
        return Err("Setup is already under way. Use a fresh box to create another identity.".into());
    }
    // Validate secure storage before mutating the setup state.
    let (mut key, _) = crate::crypto::key_for_encrypt()?;
    zeroize::Zeroize::zeroize(&mut key);

    let mut rng = rand::thread_rng();
    let phrase: Vec<String> =
        WORDS.choose_multiple(&mut rng, 6).map(|w| w.to_string()).collect();
    let token_bytes: [u8; 16] = rng.gen();
    let token: String = token_bytes.iter().map(|b| format!("{b:02x}")).collect();
    // coturn long-term auth secret (32 random bytes, hex). The homeserver signs
    // short-lived TURN credentials with it; never leaves the box.
    let turn_bytes: [u8; 32] = rng.gen();
    let turn_secret: String = turn_bytes.iter().map(|b| format!("{b:02x}")).collect();
    // Homeserver registration token: gates registration (no open-reg) and is
    // shared by the owner to add more people.
    let join_bytes: [u8; 16] = rng.gen();
    let join_token: String = join_bytes.iter().map(|b| format!("{b:02x}")).collect();
    // LiveKit SFU credentials (Element Call group calls). The api_key (16 bytes)
    // + api_secret (32 bytes), hex, are shared by livekit-server and lk-jwt so
    // the JWTs lk-jwt mints are accepted by the SFU; never leave the box.
    let livekit_key_bytes: [u8; 16] = rng.gen();
    let livekit_api_key: String = livekit_key_bytes.iter().map(|b| format!("{b:02x}")).collect();
    let livekit_secret_bytes: [u8; 32] = rng.gen();
    let livekit_api_secret: String =
        livekit_secret_bytes.iter().map(|b| format!("{b:02x}")).collect();
    let created = chrono::Local::now().format("%Y-%m-%d %H:%M").to_string();

    let mut inner = state::Inner::default();
    {
        inner.phase = state::Phase::SettingUp;
        inner.box_name = box_name;
        inner.username = username;
        inner.created = created;
        inner.phrase = phrase;
        inner.token = token;
        inner.turn_secret = turn_secret;
        inner.join_token = join_token;
        inner.livekit_api_key = livekit_api_key;
        inner.livekit_api_secret = livekit_api_secret;
        // Persist the admin password: Privacy Lodge uses password login between the
        // phone and the box, so the box keeps its own credential (single-user
        // appliance) instead of dropping it after admin creation.
        inner.admin_password = password.clone();
    }
    let dir = state::app_data_dir(&app)?;
    let (boxed, secrets) = state::identity_snapshot(&inner).documents()?;
    crate::identity_transaction::commit(&dir, &[
        ("secrets.json", secrets.as_bytes()), ("pairings.json", b"{\"peers\":[]}"), ("box.json", boxed.as_bytes()),
    ])?;
    state::update(&app, |target| *target = inner);

    // Real sidecars if the binaries are there, demo simulation otherwise.
    // Progress is observable via get_status().setup_stage.
    supervisor::start_lifecycle(&app, Some(password));
    Ok(())
}

/// Export the actual encrypted identity, never unrelated recovery words.
#[tauri::command]
pub fn save_identity_backup(app: AppHandle, passphrase: String) -> Result<String, String> {
    use std::io::Write;
    use zeroize::Zeroize;
    let mut passphrase = passphrase;
    let envelope = crate::backup::create(&app, &passphrase);
    passphrase.zeroize();
    let envelope = envelope?;
    let downloads = dirs::download_dir().ok_or("Couldn't find your Downloads folder.")?;
    std::fs::create_dir_all(&downloads).map_err(|e| e.to_string())?;
    let nonce: u64 = rand::thread_rng().gen();
    let path = downloads.join(format!("privacy-lodge-identity-{}-{nonce:016x}.json", chrono::Local::now().format("%Y%m%d-%H%M%S")));
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)] {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&path).map_err(|e| format!("Couldn't save the backup: {e}"))?;
    if let Err(e) = file.write_all(envelope.as_bytes()).and_then(|_| file.sync_all()) {
        let _ = std::fs::remove_file(&path);
        return Err(format!("Couldn't finish the backup: {e}"));
    }
    Ok(path.to_string_lossy().into_owned())
}

#[derive(Serialize)]
pub struct ConnectQr {
    pub payload: String,
    pub svg: String,
}

/// Render `payload` to a complete `<svg>` QR (dark-on-white, scannable).
pub fn render_qr_svg(payload: &str) -> Result<String, String> {
    let code = qrcode::QrCode::new(payload.as_bytes())
        .map_err(|e| format!("Couldn't build the QR code: {e}"))?;
    let rendered = code
        .render::<qrcode::render::svg::Color>()
        .min_dimensions(240, 240)
        .quiet_zone(true)
        .dark_color(qrcode::render::svg::Color("#1A1A1A"))
        .light_color(qrcode::render::svg::Color("#FFFFFF"))
        .build();
    // The renderer prefixes an XML declaration; we want a bare <svg> element.
    Ok(match rendered.find("<svg") {
        Some(idx) => rendered[idx..].to_string(),
        None => rendered,
    })
}

#[tauri::command]
pub fn get_connect_qr(app: AppHandle) -> Result<ConnectQr, String> {
    let (onion, username, token) = state::read(&app, |inner| {
        (inner.onion.clone(), inner.username.clone(), inner.token.clone())
    });
    let onion = onion.ok_or("Lodge doesn't have an address yet.")?;
    if token.is_empty() {
        return Err("Set up Lodge first.".into());
    }
    let payload = format!("privacybolt://connect?hs={onion}&user={username}&token={token}");
    let svg = render_qr_svg(&payload)?;
    Ok(ConnectQr { payload, svg })
}

/// The loopback URL of the one-page web setup server (feature A). The GUI shows
/// this on the "finish setup in your browser" screen so the user can re-open it.
#[tauri::command]
pub fn get_setup_url() -> String {
    crate::setup_server::setup_url()
}

/// Open the web setup page in the default browser (the GUI's "open setup" button).
#[tauri::command]
pub fn open_setup_page(app: AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_url(crate::setup_server::setup_url(), None::<&str>)
        .map_err(|e| e.to_string())
}

#[derive(Serialize)]
pub struct JoinInfo {
    /// The box's onion (the new person's homeserver address).
    pub onion: String,
    /// The registration token they enter to create their account.
    pub join_token: String,
    /// A QR encoding both, for the "show this to your friend" hand-off.
    pub svg: String,
}

/// Everything a new person needs to join this box: the homeserver address + the
/// registration token. The owner shows this (the People → Add a person flow);
/// the person installs a client, points at the onion, and registers with the
/// token. (tuwunel registration is token-gated — verified.)
#[tauri::command]
pub fn get_join_info(app: AppHandle) -> Result<JoinInfo, String> {
    let (onion, join_token) =
        state::read(&app, |inner| (inner.onion.clone(), inner.join_token.clone()));
    let onion = onion.ok_or("Lodge doesn't have an address yet.")?;
    if join_token.is_empty() {
        return Err("Set up Lodge first.".into());
    }
    let payload = format!("privacybolt://join?hs={onion}&token={join_token}");
    let svg = render_qr_svg(&payload)?;
    Ok(JoinInfo { onion, join_token, svg })
}

#[derive(Serialize)]
pub struct AppInfo {
    pub version: String,
    pub data_dir: String,
    pub demo_mode: bool,
}

/// App + box facts for the Settings page.
#[tauri::command]
pub fn app_info(app: AppHandle) -> Result<AppInfo, String> {
    let data_dir = state::app_data_dir(&app)?.to_string_lossy().into_owned();
    let demo_mode = state::read(&app, |inner| inner.demo_mode);
    Ok(AppInfo {
        version: env!("CARGO_PKG_VERSION").to_string(),
        data_dir,
        demo_mode,
    })
}

/// Wipe the box: stop everything, delete the data dir (RocksDB, tor keys/onion,
/// configs, secrets), and return to the fresh-setup state. Destructive and
/// irreversible — the onion identity is gone. (Plan task T-UNINST; the GUI heir
/// of `privacy-lodge reset`.) The frontend confirms before calling this.
#[tauri::command]
pub async fn reset_box(app: AppHandle) -> Result<(), String> {
    let _stopped = supervisor::stop_lifecycle(&app).await;
    let _agents = crate::agentnode_runtime::stop(true).await?;
    let _identity = crate::identity_transaction::lock();
    let dir = state::app_data_dir(&app)?;
    // This is the application's dedicated data directory. Pairings, pending
    // identity transactions and command receipts belong to the erased identity too.
    // Preserve only installed sidecars; never follow a symlink outside the directory.
    for entry in std::fs::read_dir(&dir).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        if entry.file_name() == "bin" { continue; }
        let result = if entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            std::fs::remove_dir_all(entry.path())
        } else { std::fs::remove_file(entry.path()) };
        result.map_err(|e| format!("Could not finish erasing local identity data: {e}"))?;
    }
    state::reset_to_fresh(&app);
    crate::setup_server::restart(app.clone());
    Ok(())
}

// ── Federation pairing ───────────────────────────────────────────────────

#[derive(Serialize)]
pub struct PairCodeOut {
    pub code: String,
    pub svg: String,
}

/// Mint a 15-minute pair code carrying this box's address, for a friend's box
/// to accept. (They paste it into their Boxes → Accept; you paste theirs.)
#[tauri::command]
pub fn pair_create(app: AppHandle) -> Result<PairCodeOut, String> {
    let onion = state::read(&app, |i| i.onion.clone())
        .ok_or("Lodge doesn't have an address yet.")?;
    let mut rng = rand::thread_rng();
    let nonce: [u8; 8] = rng.gen();
    let nonce_hex: String = nonce.iter().map(|b| format!("{b:02x}")).collect();
    let code = pairing::mint_code(&onion, &nonce_hex)?;
    let svg = render_qr_svg(&code)?;
    Ok(PairCodeOut { code, svg })
}

/// Accept another box's pair code: add it to the federation allowlist and
/// hot-reload the fed-proxy. Returns the peer's onion.
#[tauri::command]
pub async fn pair_accept(app: AppHandle, code: String) -> Result<String, String> {
    let my_onion = state::read(&app, |i| i.onion.clone());
    let peer = pairing::parse_code(&code)?;
    // Defence in depth: parse_code already validates the onion, but never let
    // anything that isn't a strict v3 onion reach the Caddy allowlist.
    if !pairing::is_valid_onion(&peer) {
        return Err("That pair code doesn't contain a valid address.".into());
    }
    if my_onion.as_deref() == Some(peer.as_str()) {
        return Err("That's this box's own code — paste your friend's code instead.".into());
    }
    // Write account-data FIRST (finding C5): account-data is the authoritative
    // source the box reconcile (run_pairing_sync) syncs against — it revokes any
    // peer in known−desired every ~3s. If we only added to pairings.json the
    // reconcile would revoke this peer within a tick, making "Connect a box"
    // non-functional. Recording it in account-data BEFORE the local add makes the
    // peer part of the "desired" set, so the reconcile keeps it. Best-effort: a
    // failure here (e.g. flaky Tor, or a pre-passwords box with no creds) is
    // logged but does not block the local add — the next phone QR scan or a
    // re-accept still records it, and the operator sees the warning.
    //
    // Bounded retries (3) on this interactive path: pair_accept blocks the
    // "Connect a box" button on this await, so a small budget keeps a
    // fully-failing-Tor box from hanging the UI for minutes (a healthy box
    // completes in well under a second). On a box where Tor is so broken the
    // write can't land in 3 tries, federation/calls to the peer wouldn't work
    // anyway — and a re-accept (or phone QR scan) records it when Tor recovers.
    if let Err(e) = supervisor::pair_add_onion_to_account_data(&app, &peer, 3).await {
        eprintln!("[privacy-lodge] pair_accept: account-data not updated ({e}); the reconcile may revoke {peer} until it's recorded");
    }
    let dir = state::app_data_dir(&app)?;
    pairing::add(&dir, &peer)?;
    refresh_paired_count(&app, &dir);
    supervisor::reload_fedproxy(&app);
    Ok(peer)
}

#[derive(Serialize)]
pub struct PairingView {
    pub onion: String,
    pub added_at: u64,
}

#[tauri::command]
pub fn pair_list(app: AppHandle) -> Result<Vec<PairingView>, String> {
    let dir = state::app_data_dir(&app)?;
    Ok(pairing::load(&dir)
        .peers
        .into_iter()
        .map(|p| PairingView { onion: p.onion, added_at: p.added_at })
        .collect())
}

#[tauri::command]
pub async fn pair_remove(app: AppHandle, onion: String) -> Result<(), String> {
    // Clear account-data FIRST (guard #4): account-data is the authoritative
    // source the box reconcile syncs against, so if we dropped pairings.json
    // before account-data the add-and-remove reconcile would re-add the peer
    // within ~3s. Best-effort — a failure here (e.g. flaky Tor) must not block
    // the local cut, which still removes the peer from this box's allowlist.
    let _ = supervisor::pair_remove_onion_from_account_data(&app, &onion).await;
    let dir = state::app_data_dir(&app)?;
    pairing::remove(&dir, &onion)?;
    refresh_paired_count(&app, &dir);
    supervisor::reload_fedproxy(&app);
    Ok(())
}

fn refresh_paired_count(app: &AppHandle, dir: &std::path::Path) {
    let count = pairing::onions(dir).len() as u32;
    state::update(app, |i| i.paired_count = count);
}

#[tauri::command]
pub async fn stop_box(app: AppHandle) -> Result<(), String> {
    let _stopped = supervisor::stop_lifecycle(&app).await;
    let _agents = crate::agentnode_runtime::stop(false).await?;
    Ok(())
}

#[tauri::command]
pub fn start_box(app: AppHandle) -> Result<(), String> {
    let configured = state::read(&app, |inner| inner.phase != Phase::Fresh && !inner.box_name.is_empty());
    if !configured {
        return Err("Set up Lodge first.".into());
    }
    // Restart of an already-set-up box: the admin account already exists.
    supervisor::start_lifecycle(&app, None);
    Ok(())
}

#[derive(serde::Serialize)]
pub struct LegacyInstall {
    /// True if a v0.1 Docker appliance is running on this machine.
    pub present: bool,
    /// The `pureprivacy-*` container names found — the pre-Rust appliance's own naming, kept as-is
    /// because that is what is being detected.
    pub containers: Vec<String>,
}

/// Detect an existing v0.1 Docker appliance — the pre-Rust *PurePrivacy* stack, whose
/// container names are `pureprivacy-*` for good (a historical artefact, not a rename miss) — so the native
/// app never silently orphans someone's running box. (Plan task T-MIG.) The
/// native app uses a different engine (tuwunel, fresh server identity — no DB
/// migration from Synapse), so the UI must offer an explicit choice rather than
/// stomping the running stack. Best-effort: missing/!running docker => none.
#[tauri::command]
pub fn detect_legacy_install() -> Result<LegacyInstall, String> {
    let out = std::process::Command::new("docker")
        .args([
            "ps",
            "--filter",
            "name=pureprivacy-",
            "--format",
            "{{.Names}}",
        ])
        .output();
    let containers: Vec<String> = match out {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout)
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            // Don't mistake a second native install's helpers for the appliance:
            // the v0.1 stack uses these exact service suffixes.
            .filter(|l| {
                l.starts_with("pureprivacy-")
                    && (l.contains("synapse")
                        || l.contains("tor")
                        || l.contains("wizard")
                        || l.contains("postgres")
                        || l.contains("coturn")
                        || l.contains("mcp"))
            })
            .map(String::from)
            .collect(),
        // docker absent, daemon down, or no perms => treat as "no legacy box".
        _ => Vec::new(),
    };
    Ok(LegacyInstall {
        present: !containers.is_empty(),
        containers,
    })
}
