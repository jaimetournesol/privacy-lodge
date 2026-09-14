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
//! The HTTP server bounds concurrency and request deadlines; a separate async
//! task polls the admin device list to detect the phone and stop the server.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::AppHandle;
use hyper::{Method, Response};
use http_body_util::{BodyExt, Full, Limited};
use bytes::Bytes;
use hyper_util::rt::{TokioIo, TokioTimer};
use std::convert::Infallible;
static SETUP_WRITE: Mutex<()> = Mutex::new(());
type HttpResponse = Response<Full<Bytes>>;
struct SetupRequest {
    method: Method,
    path: String,
    headers: hyper::HeaderMap,
    body: String,
    body_length: Option<usize>,
    response: tokio::sync::oneshot::Sender<HttpResponse>,
}
fn http_response(code: u16, content_type: &str, body: String) -> HttpResponse {
    Response::builder().status(code)
        .header("Content-Type", content_type).header("Cache-Control", "no-store")
        .header("X-Content-Type-Options", "nosniff").header("Referrer-Policy", "no-referrer")
        .header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        .body(Full::new(Bytes::from(body))).expect("fixed response headers")
}
async fn http_request(req: hyper::Request<hyper::body::Incoming>, app: AppHandle, phone: Arc<AtomicBool>, csrf: String, stop: Arc<AtomicBool>) -> Result<HttpResponse, Infallible> {
    let (parts, body) = req.into_parts();
    let length = parts.headers.get("content-length").and_then(|s| s.to_str().ok()).and_then(|s| s.parse::<usize>().ok());
    if parts.headers.get_all("host").iter().count() != 1 || parts.headers.get_all("content-length").iter().count() > 1 {
        return Ok(http_response(400, "text/plain", "Invalid request headers".into()));
    }
    if parts.method == Method::POST && (length.is_none() || length.unwrap_or(0) > MAX_FORM_BYTES) {
        return Ok(http_response(413, "text/plain", "Request exceeds setup limits".into()));
    }
    let collected = tokio::time::timeout(Duration::from_secs(10), Limited::new(body, MAX_FORM_BYTES).collect()).await;
    let body = match collected {
        Ok(Ok(bytes)) => match String::from_utf8(bytes.to_bytes().to_vec()) { Ok(body) => body, Err(_) => return Ok(http_response(400, "text/plain", "Invalid form encoding".into())) },
        Ok(Err(_)) => return Ok(http_response(413, "text/plain", "Request exceeds setup limits".into())),
        Err(_) => return Ok(http_response(408, "text/plain", "Setup request timed out".into())),
    };
    if stop.load(Ordering::Relaxed) { return Ok(http_response(409, "text/plain", "Reload the current setup page".into())); }
    let (tx, rx) = tokio::sync::oneshot::channel();
    let request = SetupRequest { method: parts.method, path: parts.uri.path().into(), headers: parts.headers, body, body_length: length, response: tx };
    tokio::task::spawn_blocking(move || handle(request, &app, &phone, &csrf));
    Ok(rx.await.unwrap_or_else(|_| http_response(500, "text/plain", "Setup could not complete. Try again.".into())))
}


use crate::{commands, config, state};

static SERVER_STOP: Mutex<Option<Arc<AtomicBool>>> = Mutex::new(None);
const MAX_FORM_BYTES: usize = 3 * crate::backup::MAX_ENVELOPE_BYTES + 4096;

/// Start the setup web server once per setup lifecycle. Returns
/// the port it listens on. Spawns the HTTP thread + the phone-watch task.
pub fn start(app: AppHandle) -> u16 {
    let port = config::SETUP_PORT + config::off();
    // Loopback on the GUI; the container binds 0.0.0.0 (see module docs) — the
    // Docker entrypoint sets PRIVACY_LODGE_SETUP_BIND=0.0.0.0 and publishes only to
    // the host's 127.0.0.1.
    let bind = crate::envcompat::var("SETUP_BIND").unwrap_or_else(|_| "127.0.0.1".to_string());
    let addr = format!("{bind}:{port}");

    let mut active = SERVER_STOP.lock().unwrap_or_else(|e| e.into_inner());
    if active.as_ref().is_some_and(|stop| !stop.load(Ordering::Relaxed)) {
        return port;
    }
    let stop = Arc::new(AtomicBool::new(false));
    *active = Some(stop.clone());
    drop(active);
    let csrf: String = (0..32).map(|_| format!("{:02x}", rand::random::<u8>())).collect();
    let phone_connected = Arc::new(AtomicBool::new(false));

    // Phone-watch: poll the admin device list; stop the server once the phone signs in.
    {
        let app = app.clone();
        let stop = stop.clone();
        let phone = phone_connected.clone();
        tauri::async_runtime::spawn(async move { watch_for_phone(app, stop, phone).await });
    }

    // Bound headers, bodies and active connections independently. A slow browser
    // must not hold the setup server's sole request worker indefinitely.
    tauri::async_runtime::spawn(async move {
        let mut listener = None;
        for _ in 0..40 {
            if stop.load(Ordering::Relaxed) { return; }
            match tokio::net::TcpListener::bind(&addr).await {
                Ok(bound) => { listener = Some(bound); break; }
                Err(_) => tokio::time::sleep(Duration::from_millis(100)).await,
            }
        }
        let Some(listener) = listener else { stop.store(true, Ordering::Relaxed); eprintln!("[setup] could not bind setup listener"); return };
        let slots = Arc::new(tokio::sync::Semaphore::new(8));
        loop {
            if stop.load(Ordering::Relaxed) { break; }
            let accepted = tokio::select! {
                accepted = listener.accept() => accepted,
                _ = tokio::time::sleep(Duration::from_millis(100)) => continue,
            };
            let Ok((stream, _)) = accepted else { continue };
            let Ok(permit) = slots.clone().try_acquire_owned() else { drop(stream); continue };
            let app = app.clone(); let phone = phone_connected.clone(); let csrf = csrf.clone(); let stop = stop.clone();
            tokio::spawn(async move {
                let _permit = permit;
                let service = hyper::service::service_fn(move |req| http_request(req, app.clone(), phone.clone(), csrf.clone(), stop.clone()));
                let mut builder = hyper::server::conn::http1::Builder::new();
                builder.timer(TokioTimer::new()).header_read_timeout(Duration::from_secs(5)).max_headers(32).max_buf_size(16384).keep_alive(false);
                let _ = builder.serve_connection(TokioIo::new(stream), service).await;
            });
        }
        stop.store(true, Ordering::Relaxed);
    });

    port
}

/// The loopback URL a browser opens (always 127.0.0.1 from the user's side).
pub fn setup_url() -> String {
    format!("http://127.0.0.1:{}/", config::SETUP_PORT + config::off())
}

fn trusted_authority(host: &str) -> bool {
    // Docker may publish a different host port. Trust only literal loopback
    // authorities, never substring matches or DNS names that can be rebound.
    let Ok(url) = reqwest::Url::parse(&format!("http://{host}")) else { return false };
    matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
        && url.username().is_empty() && url.password().is_none()
        && url.path() == "/" && url.query().is_none() && url.fragment().is_none()
}

fn handle(req: SetupRequest, app: &AppHandle, phone: &Arc<AtomicBool>, csrf: &str) {
    let header = |name: &str| req.headers.get(name).and_then(|value| value.to_str().ok()).unwrap_or("");
    let host = header("Host");
    let origin = header("Origin");
    if !trusted_authority(host) || (!origin.is_empty() && origin != format!("http://{host}"))
        || header("Sec-Fetch-Site") == "cross-site" {
        respond(req, 403, "text/plain", "Open setup using its local address.".into());
        return;
    }
    let method = req.method.clone();
    if method == Method::POST && header("X-Lodge-Setup") != csrf {
        respond(req, 403, "text/plain", "Reload the setup page and try again.".into());
        return;
    }
    if method == Method::POST && (req.body_length.is_none() || req.body_length.unwrap_or(0) > MAX_FORM_BYTES) {
        respond(req, 413, "application/json", r#"{"error":"Setup request is too large or has no Content-Length."}"#.into());
        return;
    }
    let _write = if method == Method::POST {
        match SETUP_WRITE.try_lock() {
            Ok(lock) => Some(lock),
            Err(_) => { respond(req, 409, "text/plain", "Setup is already processing a request.".into()); return; }
        }
    } else { None };
    let path = req.path.split('?').next().unwrap_or("/").to_string();
    match (method, path.as_str()) {
        (Method::GET, "/") => respond(req, 200, "text/html; charset=utf-8", PAGE.replace("__LODGE_SETUP_CSRF__", csrf)),
        (Method::GET, "/status") => respond(req, 200, "application/json", status_json(app, phone.load(Ordering::Relaxed))),
        (Method::POST, "/provision") => provision(req, app),
        (Method::POST, "/restore") => restore(req, app),
        _ => respond(req, 404, "text/plain", "not found".to_string()),
    }
}

fn read_form(req: &mut SetupRequest) -> Result<String, String> {
    Ok(std::mem::take(&mut req.body))
}
fn respond(req: SetupRequest, code: u16, content_type: &str, body: String) {
    let _ = req.response.send(http_response(code, content_type, body));
}
pub fn restart(app: AppHandle) {
    if let Some(stop) = SERVER_STOP.lock().unwrap_or_else(|e| e.into_inner()).take() { stop.store(true, Ordering::Relaxed); }
    start(app);
}

fn status_json(app: &AppHandle, phone_connected: bool) -> String {
    let (phase, onion, stage, username, error) =
        state::read(app, |i| (i.phase, i.onion.clone(), i.setup_stage, i.username.clone(), i.error.clone()));
    let qr = if onion.is_some() {
        commands::get_connect_qr(app.clone()).ok()
    } else {
        None
    };
    serde_json::json!({
        "phase": phase,
        "error": error,
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
fn provision(mut req: SetupRequest, app: &AppHandle) {
    let body = match read_form(&mut req) {
        Ok(body) => body,
        Err(error) => { respond(req, 400, "application/json", serde_json::json!({"error": error}).to_string()); return; }
    };
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
fn restore(mut req: SetupRequest, app: &AppHandle) {
    let body = match read_form(&mut req) {
        Ok(body) => body,
        Err(error) => { respond(req, 400, "application/json", serde_json::json!({"error": error}).to_string()); return; }
    };
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
                let phone_here = ids.iter().any(|id| *id != mine && id != "LODGE_SERVICE");
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
            "device_id": "LODGE_SETUP",
            "initial_device_display_name": "Privacy Lodge setup",
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

#[cfg(test)]
mod security_tests {
    use super::*;
    #[test]
    fn setup_authority_accepts_only_literal_loopback() {
        for host in ["127.0.0.1:8470", "localhost:9999", "[::1]:8470"] { assert!(trusted_authority(host), "{host}"); }
        for host in ["evil.example", "127.0.0.1.evil.example", "localhost@evil.example", "evil@localhost", "localhost/path", "localhost#fragment", "0.0.0.0:8470"] { assert!(!trusted_authority(host), "{host}"); }
    }
}
