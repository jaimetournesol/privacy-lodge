//! One-page web setup server (appliance-UX feature A).
//!
//! On first run (no admin account yet) the box serves a SINGLE local web page:
//! a username/password form → provisions the box (mint onion, create admin,
//! start sidecars) → shows the login QR the phone already understands
//! (`privacybolt://connect?hs=<onion>&user=<user>`). Once the phone signs in
//! (a new device appears on the admin account) the server shuts itself down —
//! setup is a one-time thing.
//!
//! This is the ONLY non-onion network surface, and only until setup completes:
//! - GUI: bound to `127.0.0.1` (loopback only) and opened in the default browser.
//! - Docker: bound to `0.0.0.0` INSIDE the container (a loopback bind there is
//!   unreachable via docker port-publishing), and docker-compose republishes it
//!   to the HOST's `127.0.0.1` only. Never mapped by tor.
//!
//! The HTTP server runs on its own thread (tiny_http, sync); a separate async
//! task polls the admin device list to detect the phone and stop the server.

use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tauri::AppHandle;
use tiny_http::{Header, Method, Response, Server};

use crate::{commands, config, state};

/// Start the setup web server (idempotent-ish: call once on first run). Returns
/// the port it listens on. Spawns the HTTP thread + the phone-watch task.
pub fn start(app: AppHandle) -> u16 {
    let port = config::SETUP_PORT + config::off();
    // Loopback on the GUI; the container binds 0.0.0.0 (see module docs) — the
    // Docker entrypoint sets PRIVACY_LODGE_SETUP_BIND=0.0.0.0 and publishes only to
    // the host's 127.0.0.1.
    let bind = crate::envcompat::var("SETUP_BIND").unwrap_or_else(|_| "127.0.0.1".to_string());
    let addr = format!("{bind}:{port}");

    let stop = Arc::new(AtomicBool::new(false));
    let phone_connected = Arc::new(AtomicBool::new(false));

    // Phone-watch: poll the admin device list; stop the server once the phone signs in.
    {
        let app = app.clone();
        let stop = stop.clone();
        let phone = phone_connected.clone();
        tauri::async_runtime::spawn(async move { watch_for_phone(app, stop, phone).await });
    }

    // HTTP server on its own thread.
    std::thread::spawn(move || {
        let server = match Server::http(&addr) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[setup] could not bind {addr}: {e}");
                return;
            }
        };
        eprintln!("[setup] setup page at http://127.0.0.1:{port}/");
        loop {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            match server.recv_timeout(Duration::from_millis(500)) {
                Ok(Some(req)) => handle(req, &app, &phone_connected),
                Ok(None) => continue, // timeout → re-check the stop flag
                Err(_) => break,
            }
        }
        eprintln!("[setup] setup complete — web server stopped");
    });

    port
}

/// The loopback URL a browser opens (always 127.0.0.1 from the user's side).
pub fn setup_url() -> String {
    format!("http://127.0.0.1:{}/", config::SETUP_PORT + config::off())
}

fn handle(req: tiny_http::Request, app: &AppHandle, phone: &Arc<AtomicBool>) {
    let method = req.method().clone();
    let path = req.url().split('?').next().unwrap_or("/").to_string();
    match (method, path.as_str()) {
        (Method::Get, "/") => respond(req, 200, "text/html; charset=utf-8", PAGE.to_string()),
        (Method::Get, "/status") => {
            let body = status_json(app, phone.load(Ordering::Relaxed));
            respond(req, 200, "application/json", body);
        }
        (Method::Post, "/provision") => provision(req, app),
        (Method::Post, "/restore") => restore(req, app),
        _ => respond(req, 404, "text/plain", "not found".to_string()),
    }
}

fn respond(req: tiny_http::Request, code: u16, content_type: &str, body: String) {
    let header = Header::from_bytes(&b"Content-Type"[..], content_type.as_bytes())
        .expect("valid header");
    let resp = Response::from_string(body)
        .with_status_code(code)
        .with_header(header);
    let _ = req.respond(resp);
}

fn status_json(app: &AppHandle, phone_connected: bool) -> String {
    let (phase, onion, stage, username) =
        state::read(app, |i| (i.phase, i.onion.clone(), i.setup_stage, i.username.clone()));
    let qr = if onion.is_some() {
        commands::get_connect_qr(app.clone()).ok()
    } else {
        None
    };
    serde_json::json!({
        "phase": phase,
        "stage": stage,
        "onion": onion,
        "username": username,
        "qr_payload": qr.as_ref().map(|q| &q.payload),
        "qr_svg": qr.as_ref().map(|q| &q.svg),
        "phone_connected": phone_connected,
    })
    .to_string()
}

/// Handle POST /provision: parse the form, provision (once), respond.
fn provision(mut req: tiny_http::Request, app: &AppHandle) {
    let mut body = String::new();
    let _ = req.as_reader().read_to_string(&mut body);
    let form = parse_form(&body);
    let username = form.get("username").cloned().unwrap_or_default();
    let password = form.get("password").cloned().unwrap_or_default();
    let box_name = form
        .get("box_name")
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| format!("{}'s box", username.trim()));

    // Only ever provision from Fresh — a reload or double-submit must not re-run it.
    let phase = state::read(app, |i| i.phase);
    let (code, out) = if phase != state::Phase::Fresh {
        (409, serde_json::json!({"error": "Setup is already under way."}).to_string())
    } else {
        match commands::begin_setup(app.clone(), box_name, username, password) {
            Ok(()) => (200, serde_json::json!({"ok": true}).to_string()),
            Err(e) => (400, serde_json::json!({"error": e}).to_string()),
        }
    };
    respond(req, code, "application/json", out);
}

/// Handle POST /restore (feature D): rebuild this box from an encrypted backup instead of
/// creating a new one. On success the box boots on the SAME .onion with the same admin login.
fn restore(mut req: tiny_http::Request, app: &AppHandle) {
    let mut body = String::new();
    let _ = req.as_reader().read_to_string(&mut body);
    let form = parse_form(&body);
    let envelope = form.get("envelope").cloned().unwrap_or_default();
    let passphrase = form.get("passphrase").cloned().unwrap_or_default();

    // Same guard as provision: only ever from Fresh, so a restore can't clobber a live box.
    let phase = state::read(app, |i| i.phase);
    let (code, out) = if phase != state::Phase::Fresh {
        (409, serde_json::json!({"error": "Setup is already under way."}).to_string())
    } else {
        match crate::backup::restore(app, envelope.trim(), &passphrase) {
            Ok(()) => {
                // Boot it: tor picks the restored onion key back up, and the admin account is
                // re-created in the fresh homeserver DB from the restored credentials.
                let pass = state::read(app, |i| i.admin_password.clone());
                crate::supervisor::start_lifecycle(app, Some(pass));
                (200, serde_json::json!({"ok": true}).to_string())
            }
            Err(e) => (400, serde_json::json!({"error": e}).to_string()),
        }
    };
    respond(req, code, "application/json", out);
}

/// Poll the admin account's device list; when a device beyond the box's own
/// appears (the phone), flip `phone_connected`, give the page a moment to show
/// "connected", then set `stop` so the HTTP thread exits and the port closes.
async fn watch_for_phone(app: AppHandle, stop: Arc<AtomicBool>, phone: Arc<AtomicBool>) {
    let base = format!("http://127.0.0.1:{}", config::HOMESERVER_PORT + config::off());
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .no_proxy()
        .build()
        .unwrap_or_default();

    // Wait until the box is Running and we have admin creds.
    let (mut token, mut my_device): (Option<String>, Option<String>) = (None, None);
    loop {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        tokio::time::sleep(Duration::from_secs(3)).await;
        let (phase, user, pass) =
            state::read(&app, |i| (i.phase, i.username.clone(), i.admin_password.clone()));
        if phase != state::Phase::Running || user.is_empty() || pass.is_empty() {
            continue;
        }
        if token.is_none() {
            if let Some((t, d)) = admin_login(&client, &base, &user, &pass).await {
                token = Some(t);
                my_device = Some(d);
            } else {
                continue;
            }
        }
        break;
    }

    // Let the box's own pairing-sync login settle so its device is in the baseline
    // (the user is reading the QR / grabbing their phone anyway).
    tokio::time::sleep(Duration::from_secs(15)).await;

    // Baseline the current device set (box's own logins). Any device beyond this
    // set — other than our own poll device — is the phone.
    let mut baseline: HashSet<String> = HashSet::new();
    if let Some(t) = &token {
        if let Some(ids) = device_ids(&client, &base, t).await {
            baseline = ids.into_iter().collect();
        }
    }

    loop {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        tokio::time::sleep(Duration::from_secs(3)).await;
        let (user, pass) = state::read(&app, |i| (i.username.clone(), i.admin_password.clone()));
        let Some(t) = token.clone() else { break };
        match device_ids(&client, &base, &t).await {
            Some(ids) => {
                let mine = my_device.clone().unwrap_or_default();
                let phone_here = ids.iter().any(|id| *id != mine && !baseline.contains(id));
                if phone_here {
                    phone.store(true, Ordering::Relaxed);
                    // Let the page poll once more and show "connected", then stop.
                    tokio::time::sleep(Duration::from_secs(4)).await;
                    stop.store(true, Ordering::Relaxed);
                    return;
                }
            }
            None => {
                // Token likely rejected → re-login (a new device of our own); fold the
                // old one into the baseline so it isn't mistaken for the phone.
                if let Some(m) = my_device.take() {
                    baseline.insert(m);
                }
                if let Some((nt, nd)) = admin_login(&client, &base, &user, &pass).await {
                    token = Some(nt);
                    my_device = Some(nd);
                }
            }
        }
    }
}

/// Log in as the box admin; returns (access_token, device_id).
async fn admin_login(
    client: &reqwest::Client,
    base: &str,
    user: &str,
    pass: &str,
) -> Option<(String, String)> {
    let r = client
        .post(format!("{base}/_matrix/client/v3/login"))
        .json(&serde_json::json!({
            "type": "m.login.password",
            "identifier": { "type": "m.id.user", "user": user },
            "password": pass,
        }))
        .send()
        .await
        .ok()?;
    let v: serde_json::Value = r.json().await.ok()?;
    let token = v.get("access_token")?.as_str()?.to_string();
    let device = v.get("device_id").and_then(|d| d.as_str()).unwrap_or("").to_string();
    Some((token, device))
}

/// Fetch all device ids on the admin account. `None` on a failed/unauthorized read.
async fn device_ids(client: &reqwest::Client, base: &str, token: &str) -> Option<Vec<String>> {
    let r = client
        .get(format!("{base}/_matrix/client/v3/devices"))
        .bearer_auth(token)
        .send()
        .await
        .ok()?;
    if !r.status().is_success() {
        return None;
    }
    let v: serde_json::Value = r.json().await.ok()?;
    let ids = v
        .get("devices")?
        .as_array()?
        .iter()
        .filter_map(|d| d.get("device_id").and_then(|x| x.as_str()).map(String::from))
        .collect();
    Some(ids)
}

fn parse_form(body: &str) -> std::collections::HashMap<String, String> {
    body.split('&')
        .filter_map(|kv| {
            let mut it = kv.splitn(2, '=');
            let k = it.next()?;
            let v = it.next().unwrap_or("");
            Some((urldecode(k), urldecode(v)))
        })
        .collect()
}

fn urldecode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b'%' if i + 2 < bytes.len() => {
                match (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                    (Some(h), Some(l)) => {
                        out.push(h * 16 + l);
                        i += 3;
                    }
                    _ => {
                        out.push(bytes[i]);
                        i += 1;
                    }
                }
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// The single self-contained setup page (inline CSS + JS). Talks to /status and
/// /provision on the same origin. Branded to match the app (Ink + Sunflower).
const PAGE: &str = include_str!("setup_page.html");
