//! Renders tuwunel.toml + torrc + turnserver.conf into `<app_data_dir>/config`,
//! mirroring the proven spike / v0.1 appliance configs.
//!
//! Real-mode sequencing quirk: tuwunel needs `server_name = <onion>`, and
//! coturn needs the onion for `realm`/`external-ip` — but the onion only
//! exists after tor mints it. So we first write a placeholder tuwunel.toml
//! (so the file always exists), and re-render with the real onion once
//! `<data>/tor/hs/hostname` appears — only then are tuwunel + coturn started.

use std::fmt::Write as _;
use std::path::PathBuf;
use tauri::AppHandle;

use crate::state::app_data_dir;

/// Homeserver listens here. 8118 deliberately avoids colliding with a dev
/// Synapse on 8008/8448.
pub const HOMESERVER_PORT: u16 = 8118;
/// Tor SOCKS port tuwunel uses for outbound federation.
pub const SOCKS_PORT: u16 = 9150;
/// coturn's loopback listener. Tor maps the well-known onion ports 3478 and
/// 5349 here; 3479 locally avoids colliding with a system coturn on 3478.
pub const TURN_PORT: u16 = 3479;
/// coturn TCP relay range. Each active TURN allocation pins one port for
/// ~15 minutes, so 40 ports ≈ 40 concurrent 1:1 calls — the same sizing the
/// v0.1 appliance proved out. v0.1 used 49152-49191 inside a dedicated
/// container netns; on a desktop host that sits inside Linux's default
/// ephemeral range (32768-60999), so we move just above it to keep loopback
/// relay binds from racing the kernel's source-port allocator.
///
/// THIS RANGE MUST EXACTLY MATCH the HiddenServicePort lines in torrc — tor
/// cannot wildcard-map a port range, so every port needs an explicit line.
/// Both renders below derive from these two constants, so they agree by
/// construction; if you change them, both files re-render together.
pub const TURN_RELAY_PORT_MIN: u16 = 61000;
pub const TURN_RELAY_PORT_MAX: u16 = 61039;
/// Caddy fed-proxy: TLS-terminates inbound federation and enforces the paired-
/// peer allowlist (tuwunel has no allowlist of its own). tor maps the onion's
/// federation port 8448 here; Caddy reverse-proxies to the homeserver.
pub const FEDPROXY_PORT: u16 = 8449;

/// Caddy's local admin/config API. Caddy defaults every instance to :2019; when
/// several boxes share one host (the dev testbed), they all bind that single
/// endpoint and the last `caddy run` POSTs its config to it — silently clobbering
/// the others' running config (so only one box's fed-proxy ports stay served, and
/// every other box's federation goes dark). Offsetting it per instance gives each
/// box its own admin endpoint. In production (one box per host) off()=0 keeps it
/// at the default :2019 — no behaviour change.
pub const CADDY_ADMIN_PORT: u16 = 2019;

/// LiveKit SFU group-call sidecars (Element Call). All loopback; Tor maps the
/// well-known onion ports here. These mirror the v0.1 appliance exactly:
/// LiveKit is TCP-only (Tor carries no UDP) and the SFU URL handed to clients
/// is wss:// (Element Call refuses ws://), terminated by a second Caddy site.
/// LiveKit signaling WebSocket (loopback; Caddy wss site reverse-proxies here).
pub const LIVEKIT_WS_PORT: u16 = 7880;
/// LiveKit TCP media relay (Tor carries no UDP, so media rides TCP).
pub const LIVEKIT_TCP_PORT: u16 = 7881;
/// lk-jwt-service: validates a Matrix OpenID token and mints a LiveKit JWT.
pub const LKJWT_PORT: u16 = 8082;
/// Onion port for the wss-terminated SFU signaling path (handed to clients).
pub const LIVEKIT_WSS_ONION_PORT: u16 = 7443;
/// Caddy's loopback listener for the wss SFU site; tor maps 7443 here.
pub const CADDY_WSS_PORT: u16 = 7444;
/// Element Call (in the phone WebView) reaches the box over Tor's HTTP CONNECT
/// tunnel, which only tunnels TLS — plain-http onion ports (8082 lk-jwt, 8008
/// client API) aren't reachable that way. So Caddy ALSO TLS-terminates lk-jwt and
/// the client API on dedicated onion ports, mirroring the wss SFU site. (The phone
/// app's call code targets exactly these: https onion:8443 / :8009.)
pub const LKJWT_TLS_ONION_PORT: u16 = 8443;
/// Caddy loopback listener for the TLS lk-jwt site; tor maps 8443 here.
pub const CADDY_JWT_PORT: u16 = 8445;
/// Onion port serving the client API (well-known + C-S) over TLS, same reason.
pub const HS_TLS_ONION_PORT: u16 = 8009;
/// Caddy loopback listener for the TLS client-API site; tor maps 8009 here.
pub const CADDY_HS_PORT: u16 = 8455;
/// Onion port serving the **self-hosted Element Call** web app (feature J). Previously the
/// phone fetched this bundle from call.element.io — a third party on the CLEARNET — which
/// contradicted "all connections are to your own box, over Tor". Serving it from the box makes
/// calls genuinely box-only. TLS for the same reason as 8443/8009: the WebView reaches the box
/// through Tor's HTTP CONNECT tunnel, which only carries TLS.
pub const EC_TLS_ONION_PORT: u16 = 8444;
/// Caddy's loopback listener for the Element Call site; tor maps 8444 here.
pub const CADDY_EC_PORT: u16 = 8446;
/// Directory (inside the box's bin dir) holding the unpacked Element Call bundle.
pub const EC_DIR_NAME: &str = "element-call";
/// Loopback federation-allowlist validator (review item W3-T1). Caddy
/// `forward_auth`s authenticated federation requests here; it parses the
/// X-Matrix Authorization origin and 200/403s it against the live pairings
/// allowlist. Not exposed via tor — pure loopback between Caddy and the box.
pub const FEDAUTH_PORT: u16 = 8460;

/// One-page web setup server (appliance-UX feature A). Serves the first-run
/// username/password form + login QR to a browser, then shuts down once the
/// phone signs in. The ONLY non-onion surface, and only until setup completes:
/// bound to loopback on the GUI, and to the container (0.0.0.0) in Docker where
/// docker-compose republishes it to the HOST's 127.0.0.1 only. Never mapped by tor.
pub const SETUP_PORT: u16 = 8470;

/// Loopback port the agent container's Hermes WebUI listens on (it shares the box's network
/// namespace, so this is the box's own loopback).
pub const AGENT_WEBUI_PORT: u16 = 8787;
/// Onion port for the agent WebUI, published on the SEPARATE agent hidden service — never
/// on the main onion, which every paired peer box knows.
pub const AGENT_WEBUI_ONION_PORT: u16 = 8788;

/// The highest fixed loopback base port any `PORT + off()` expression adds the
/// offset to (the top of the coturn TCP relay range). The offset is clamped so
/// even this port can't overflow a u16 — see `off()`. [QW-rust d]
const MAX_BASE_PORT: u16 = TURN_RELAY_PORT_MAX;
/// Largest offset that keeps every `PORT + off()` inside u16 (no wraparound).
const MAX_PORT_OFFSET: u16 = u16::MAX - MAX_BASE_PORT;

/// Per-instance LOOPBACK port offset (env `PRIVACY_LODGE_PORT_OFFSET`, default 0).
/// Lets two boxes run on one host: every loopback bind/map target shifts by this,
/// while the .onion-facing ports stay standard (each box has its own onion, so
/// 8008/8448/3478/7443/8082 never collide and clients see the same ports on both).
/// Unset (= 0) in production and tests, so behaviour is unchanged by default.
///
/// [QW-rust d] CLAMP against port overflow: every loopback port is computed as
/// `<base> + off()` (u16 + u16). An offset large enough to push the highest base
/// port (the coturn relay-range top, ~61039) past 65535 would silently WRAP in
/// release builds, mapping a sidecar to a bogus low port. So we clamp the parsed
/// value to `MAX_PORT_OFFSET` — the largest offset that keeps every `PORT+off()`
/// inside u16. A non-numeric / absent env stays 0 (the production default).
pub fn off() -> u16 {
    crate::envcompat::var("PORT_OFFSET")
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(0)
        .min(MAX_PORT_OFFSET)
}

pub struct Paths {
    pub config_dir: PathBuf,
    pub torrc: PathBuf,
    pub tuwunel_toml: PathBuf,
    pub turnserver_conf: PathBuf,
    pub caddyfile: PathBuf,
    pub livekit_yaml: PathBuf,
    pub fed_cert: PathBuf,
    pub fed_key: PathBuf,
    pub data_root: PathBuf,
    pub tor_data: PathBuf,
    pub hs_dir: PathBuf,
    pub hostname_file: PathBuf,
    /// SECOND hidden service, for the agent WebUI only.
    ///
    /// It gets its own onion rather than a port on the main one because the main onion is
    /// shared with federation: every paired peer box knows that address, and the WebUI is
    /// an agent control plane that can execute shell commands. A separate service means the
    /// admin surface has an address that is never handed to a peer, and it's the only shape
    /// that lets tor v3 client authorisation be added later — that's per-service, so
    /// enabling it on the main onion would demand a key from every federating peer and
    /// break federation outright — which is exactly what [`ensure_agent_client_auth`] does
    /// here, safely, because this service is shared with nobody.
    pub agent_hs_dir: PathBuf,
    pub agent_hostname_file: PathBuf,
    pub tuwunel_data: PathBuf,
}

impl Paths {
    /// The phone's half of the agent onion's client-authorisation keypair. Deliberately
    /// beside the hidden-service dir, not inside it — tor parses everything it finds in a
    /// `HiddenServiceDir`, and this is ours, not tor's.
    pub fn agent_client_auth_key(&self) -> PathBuf {
        self.tor_data.join("agent-client-auth.key")
    }
}

pub fn paths(app: &AppHandle) -> Result<Paths, String> {
    let base = app_data_dir(app)?;
    let config_dir = base.join("config");
    let tor_data = base.join("data").join("tor");
    let hs_dir = tor_data.join("hs");
    let agent_hs_dir = tor_data.join("hs-agent");
    Ok(Paths {
        agent_hostname_file: agent_hs_dir.join("hostname"),
        agent_hs_dir,
        torrc: config_dir.join("torrc"),
        tuwunel_toml: config_dir.join("tuwunel.toml"),
        turnserver_conf: config_dir.join("turnserver.conf"),
        caddyfile: config_dir.join("Caddyfile"),
        livekit_yaml: config_dir.join("livekit.yaml"),
        fed_cert: config_dir.join("fed-cert.pem"),
        fed_key: config_dir.join("fed-key.pem"),
        hostname_file: hs_dir.join("hostname"),
        tuwunel_data: base.join("data").join("tuwunel"),
        data_root: base.clone(),
        config_dir,
        tor_data,
        hs_dir,
    })
}

#[cfg(unix)]
fn set_0700(path: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
}

#[cfg(not(unix))]
fn set_0700(_path: &std::path::Path) {}

#[cfg(unix)]
fn set_0600(path: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}

#[cfg(not(unix))]
fn set_0600(_path: &std::path::Path) {}

/// Create config/data directories. The hidden-service dir must be 0700 or
/// tor refuses to start.
pub fn ensure_dirs(app: &AppHandle) -> Result<Paths, String> {
    let p = paths(app)?;
    for dir in [&p.config_dir, &p.tor_data, &p.hs_dir, &p.agent_hs_dir, &p.tuwunel_data] {
        std::fs::create_dir_all(dir)
            .map_err(|e| format!("couldn't create {}: {e}", dir.display()))?;
    }
    // config_dir holds rendered files carrying secrets (registration_token,
    // turn_secret, livekit api secret, fed key) — owner-only.
    set_0700(&p.config_dir);
    set_0700(&p.tor_data);
    set_0700(&p.hs_dir);
    set_0700(&p.agent_hs_dir);
    ensure_agent_client_auth(&p)?;
    Ok(p)
}

/// Base32 (RFC 4648, uppercase, unpadded) — the encoding tor uses for v3 client-auth keys.
/// Hand-rolled rather than pulling a crate for 30 lines used in exactly one place.
fn base32(data: &[u8]) -> String {
    const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let mut out = String::new();
    let (mut acc, mut bits) = (0u32, 0u32);
    for &b in data {
        acc = (acc << 8) | b as u32;
        bits += 8;
        while bits >= 5 {
            bits -= 5;
            out.push(A[((acc >> bits) & 31) as usize] as char);
        }
    }
    if bits > 0 {
        out.push(A[((acc << (5 - bits)) & 31) as usize] as char);
    }
    out
}

/// Tor v3 client authorisation for the agent WebUI's hidden service.
///
/// This is the cryptographic gate in front of an admin surface that can run shell commands.
/// Without the matching private key a client cannot even FETCH the service's descriptor —
/// the service is invisible, and the refusal happens in the Tor layer, before any HTTP
/// request reaches the WebUI. Password alone would put the entire gate behind a form served
/// to anyone who knows the address.
///
/// Why this is safe to do unconditionally, and why it must be the *agent* service only:
/// v3 client auth is per-SERVICE, not per-port. The box's main onion carries federation, so
/// enabling it there would demand a key from every peer box and break federation outright.
/// `hs-agent` is shared with nobody, so locking it costs nothing.
///
/// Generated HERE, at directory-creation time, rather than during agent setup — tor reads
/// `authorized_clients/` when it starts the service, so a key written afterwards wouldn't
/// take effect until a restart. Doing it before tor ever launches means the gate is up from
/// the first boot, and the private key is simply waiting for the owner's phone to collect it.
///
/// Generated once and kept: regenerating on each boot would silently lock out the phone
/// that already holds the old key.
fn ensure_agent_client_auth(p: &Paths) -> Result<(), String> {
    let priv_file = p.agent_client_auth_key();
    let clients_dir = p.agent_hs_dir.join("authorized_clients");
    let pub_file = clients_dir.join("phone.auth");
    if priv_file.exists() && pub_file.exists() {
        return Ok(());
    }
    // x25519 secret: 32 random bytes, clamped as the curve requires.
    use rand::RngCore;
    let mut sk = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut sk);
    sk[0] &= 248;
    sk[31] &= 127;
    sk[31] |= 64;
    let pk = x25519_dalek::x25519(sk, x25519_dalek::X25519_BASEPOINT_BYTES);

    std::fs::create_dir_all(&clients_dir)
        .map_err(|e| format!("couldn't create {}: {e}", clients_dir.display()))?;
    set_0700(&clients_dir);
    // Public half, for tor: one line, `descriptor:x25519:<base32 pubkey>`.
    std::fs::write(&pub_file, format!("descriptor:x25519:{}\n", base32(&pk)))
        .map_err(|e| format!("couldn't write {}: {e}", pub_file.display()))?;
    set_0600(&pub_file);
    // Private half, for the phone. Stored OUTSIDE the hidden-service directory so tor never
    // tries to parse it as one of its own files.
    std::fs::write(&priv_file, base32(&sk))
        .map_err(|e| format!("couldn't write {}: {e}", priv_file.display()))?;
    set_0600(&priv_file);
    eprintln!("[privacy-lodge] agent WebUI: tor v3 client authorisation enabled");
    Ok(())
}

/// Pure builder for torrc — unit-tested; `render_torrc` just adds paths + I/O.
/// Federation (8448) goes to the Caddy fed-proxy (TLS + allowlist); the client
/// API (8008) and TURN go straight to their services.
fn torrc_string(
    socks: u16,
    data: &str,
    hs: &str,
    hsport: u16,
    fedproxy: u16,
    voice: bool,
    agent_hs: &str,
) -> String {
    // Loopback bind/map targets shift by off() per instance; the onion-facing
    // ports (8448/8008/80/3478/5349/7443/8082) stay standard so clients see the same
    // ports on every box. off() == 0 in production + tests (env unset).
    // Port 80 → tuwunel mirrors 8008: matrix-rust-sdk derives some client calls
    // (e.g. account data) from the bare server_name and hits the onion's default
    // http port, so the homeserver must answer there too.
    let o = off();
    // NoIsolateClientAddr: share circuits across local SOCKS clients.
    // G1 finding (2026-06-13): tor's default per-client isolation hands the
    // homeserver cold circuits, and first-contact federation then exceeds
    // its connect timeout.  See docs/redesign/2026-06-phase0-spike-results.md.
    let mut torrc = format!(
        "SocksPort {socks} NoIsolateClientAddr\n\
         DataDirectory {data}\n\
         # Exercise OR-connections more often (default 300s) so a half-dead\n\
         # circuit is detected/torn down sooner, not only on a homeserver send.\n\
         KeepalivePeriod 60\n\
         HiddenServiceDir {hs}\n\
         HiddenServicePort 8448 127.0.0.1:{fedproxy}\n\
         HiddenServicePort 8008 127.0.0.1:{hsport}\n\
         HiddenServicePort 80 127.0.0.1:{hsport}\n\
         HiddenServicePort 3478 127.0.0.1:{turn}\n\
         HiddenServicePort 5349 127.0.0.1:{turn}\n",
        socks = socks + o,
        fedproxy = fedproxy + o,
        hsport = hsport + o,
        turn = TURN_PORT + o,
    );
    // Tor cannot wildcard-map a port RANGE, so the coturn TCP relay range
    // needs one explicit HiddenServicePort line per port. Same constants the
    // turnserver.conf min/max-port derive from, so the two files always agree.
    for port in (TURN_RELAY_PORT_MIN + o)..=(TURN_RELAY_PORT_MAX + o) {
        let _ = writeln!(torrc, "HiddenServicePort {port} 127.0.0.1:{port}");
    }
    // Group-call (Element Call / LiveKit) onion port map. Only published when the
    // LiveKit + lk-jwt sidecars are present; harmless to omit otherwise. Mirrors
    // the v0.1 appliance torrc: wss-terminated SFU signaling (Caddy wss site),
    // the TCP media relay, and the lk-jwt token service. Onion ports stay standard;
    // only the loopback targets shift by the offset.
    if voice {
        let _ = writeln!(
            torrc,
            "HiddenServicePort {wss} 127.0.0.1:{caddy_wss}\n\
             HiddenServicePort {media} 127.0.0.1:{media}\n\
             HiddenServicePort {jwt} 127.0.0.1:{jwt_loop}\n\
             HiddenServicePort {jwt_tls} 127.0.0.1:{caddy_jwt}\n\
             HiddenServicePort {hs_tls} 127.0.0.1:{caddy_hs}\n\
             HiddenServicePort {ec_tls} 127.0.0.1:{caddy_ec}",
            wss = LIVEKIT_WSS_ONION_PORT,
            caddy_wss = CADDY_WSS_PORT + o,
            media = LIVEKIT_TCP_PORT + o,
            jwt = LKJWT_PORT,
            jwt_loop = LKJWT_PORT + o,
            jwt_tls = LKJWT_TLS_ONION_PORT,
            caddy_jwt = CADDY_JWT_PORT + o,
            hs_tls = HS_TLS_ONION_PORT,
            caddy_hs = CADDY_HS_PORT + o,
            ec_tls = EC_TLS_ONION_PORT,
            caddy_ec = CADDY_EC_PORT + o,
        );
    }
    // The agent WebUI, on its OWN hidden service (see Paths::agent_hs_dir). Always mapped:
    // if the agents add-on isn't installed nothing listens on the loopback target, so the
    // port simply refuses — no surface, no branch to get wrong. Plain HTTP is fine here
    // because an onion service already authenticates and encrypts end to end; the phone
    // tunnels raw TCP to it over SOCKS.
    let _ = write!(
        torrc,
        "\nHiddenServiceDir {agent_hs}\n\
         HiddenServicePort {port} 127.0.0.1:{target}\n",
        port = AGENT_WEBUI_ONION_PORT,
        target = AGENT_WEBUI_PORT + o,
    );
    torrc
}

pub fn render_torrc(app: &AppHandle, voice: bool) -> Result<(), String> {
    let p = paths(app)?;
    let torrc = torrc_string(
        SOCKS_PORT,
        &p.tor_data.display().to_string(),
        &p.hs_dir.display().to_string(),
        HOMESERVER_PORT,
        FEDPROXY_PORT,
        voice,
        &p.agent_hs_dir.display().to_string(),
    );
    std::fs::write(&p.torrc, torrc).map_err(|e| format!("couldn't write torrc: {e}"))
}

/// Render tuwunel.toml with the given server_name (the onion, or a
/// placeholder before tor has minted one). When `turn_secret` is non-empty and
/// the server_name is the real onion, advertise the 1:1 voice TURN server —
/// tuwunel signs short-lived credentials with the shared secret and hands them
/// to clients via /_matrix/client/v3/voip/turnServer (spike-verified wired).
/// Pure builder for tuwunel.toml — unit-tested.
fn tuwunel_toml_string(
    server_name: &str,
    db: &str,
    port: u16,
    socks: u16,
    turn_secret: &str,
    join_token: &str,
    voice: bool,
) -> String {
    let port = port + off(); // loopback bind shifts per instance; onion 8008 maps here
    let mut toml = format!(
        "[global]\n\
         server_name = \"{server_name}\"\n\
         database_path = \"{db}\"\n\
         port = {port}\n\
         address = \"127.0.0.1\"\n\
         # The Backup app (feature F) syncs photos + large files through this homeserver's\n\
         # media repo, so the ~20 MB default ceiling is far too small. 256 MB covers photos\n\
         # and short videos while staying sane on a small box (a request is buffered in\n\
         # memory, so this is deliberately not unbounded).\n\
         max_request_size = 268435456\n\
         allow_federation = true\n\
         allow_invalid_tls_certificates = true\n\
         trusted_servers = []\n\
         query_trusted_key_servers_first = false\n\
         # Cold onion circuits legitimately take tens of seconds on first\n\
         # contact — G1-proven values (2026-06-13).\n\
         request_conn_timeout = 90\n\
         request_total_timeout = 320\n\
         sender_timeout = 300\n\
         well_known_conn_timeout = 30\n\
         well_known_timeout = 60\n\
         # Federation self-recovery over flaky Tor. tuwunel marks a timed-out\n\
         # destination Failed and backs off min(sender_timeout*tries^2, limit) — a\n\
         # SILENT, timer-less window (300s on the FIRST failure) that does NOT\n\
         # re-probe when the Tor circuit heals, so messages + Megolm key-shares\n\
         # stall for minutes until a manual restart. Cap the window so a healed\n\
         # circuit retries within ~a minute. (Bounds retry SCHEDULING, not the\n\
         # cold-start request timeouts above — first contact is unaffected.)\n\
         sender_retry_backoff_limit = 60\n\
         # Evict idle pooled sockets fast so a retry dials a FRESH Tor stream\n\
         # instead of reusing a keep-alive socket wedged on a dead circuit.\n\
         sender_idle_timeout = 15\n\
         federation_idle_timeout = 5\n\
         # Re-attempt every queued event on (re)start without trimming (lossless).\n\
         startup_netburst_keep = -1\n"
    );
    if !join_token.is_empty() {
        // Registration is token-gated, never open. The app creates the admin
        // (first user => auto-admin) with this token, and the owner shares it
        // to add more people.
        let _ = write!(
            toml,
            "allow_registration = true\n\
             registration_token = \"{join_token}\"\n"
        );
    }
    if !turn_secret.is_empty() && !server_name.ends_with("placeholder.onion") {
        // turn:<onion>:3478 — tor maps that onion port to the loopback coturn.
        // TCP transport only: Tor carries no UDP. (onion-purist: 1:1 voice
        // rides Tor, best-effort. Same-box calls only; cross-install voice is
        // the Element Call / LiveKit path.)
        let _ = write!(
            toml,
            "turn_uris = [\"turn:{server_name}:3478?transport=tcp\"]\n\
             turn_secret = \"{turn_secret}\"\n\
             turn_ttl = 86400\n"
        );
    }
    if voice && !server_name.ends_with("placeholder.onion") {
        // Group calls (Element Call / LiveKit). Advertising livekit_url makes
        // tuwunel publish org.matrix.msc4143.rtc_foci, which clients read to
        // discover the SFU + the lk-jwt token endpoint. client must be set
        // alongside it (tuwunel only emits the rtc_foci block when both are
        // present). The client (phone WebView) reaches lk-jwt over Tor's HTTP CONNECT
        // tunnel, which only tunnels TLS — so advertise the TLS lk-jwt onion port
        // (Caddy site below), NOT the plain-http 8082.
        let _ = write!(
            toml,
            "\n[global.well_known]\n\
             client = \"http://{server_name}\"\n\
             livekit_url = \"https://{server_name}:{jwt_tls}\"\n",
            jwt_tls = LKJWT_TLS_ONION_PORT,
        );
    }
    let _ = write!(
        toml,
        "\n[global.proxy.global]\n\
         url = \"socks5h://127.0.0.1:{socks}\"\n",
        socks = socks + off(),
    );
    toml
}

/// Render tuwunel.toml with the given server_name (the onion, or a
/// placeholder before tor has minted one). When `turn_secret` is non-empty and
/// the server_name is the real onion, advertise the 1:1 voice TURN server —
/// tuwunel signs short-lived credentials with the shared secret and hands them
/// to clients via /_matrix/client/v3/voip/turnServer (spike-verified wired).
pub fn render_tuwunel(
    app: &AppHandle,
    server_name: &str,
    turn_secret: &str,
    join_token: &str,
    voice: bool,
) -> Result<(), String> {
    let p = paths(app)?;
    let toml = tuwunel_toml_string(
        server_name,
        &p.tuwunel_data.display().to_string(),
        HOMESERVER_PORT,
        SOCKS_PORT,
        turn_secret,
        join_token,
        voice,
    );
    std::fs::write(&p.tuwunel_toml, toml)
        .map_err(|e| format!("couldn't write tuwunel.toml: {e}"))?;
    // Carries the registration_token + turn_secret — owner-only.
    set_0600(&p.tuwunel_toml);
    Ok(())
}

/// Render turnserver.conf for the 1:1-voice coturn sidecar, mirroring the
/// v0.1 appliance's TCP-only-over-Tor config. Needs the minted onion (for
/// `realm`/`external-ip`) and the shared auth secret.
///
/// `min-port`/`max-port` MUST equal the relay range published in torrc — both
/// derive from the same constants, so they agree by construction.
/// Pure builder for turnserver.conf — unit-tested.
fn turnserver_conf_string(onion: &str, secret: &str) -> String {
    format!(
        "# Privacy Lodge coturn — TCP-only relay over Tor (generated, do not edit).\n\
         listening-port={turn}\n\
         # Bind the STUN/TURN control listener to loopback ONLY. tor maps onion:3478/5349\n\
         # here, so nothing legitimate needs a non-loopback address — and without this coturn\n\
         # listens on ALL interfaces (LAN + public IPv6), reachable off-Tor. Worse, coturn\n\
         # returns `realm` (= the .onion) in its UNAUTHENTICATED 401 nonce, so a clearnet probe\n\
         # would tie the box's real IP to its onion identity. (Security review, 2026-07-24.)\n\
         listening-ip=127.0.0.1\n\
         min-port={min}\n\
         max-port={max}\n\
         # The relay address must be reachable by the CO-LOCATED SFU (loopback), NOT\n\
         # the .onion — the client reaches coturn via the Tor control channel\n\
         # (turn:<onion>:3478, advertised by LiveKit), and coturn forwards the relayed\n\
         # media to the SFU locally. Advertising the onion here makes the SFU unable to\n\
         # reach the relay (udp send: Invalid argument) once media is forced to relay.\n\
         external-ip=127.0.0.1\n\
         relay-ip=127.0.0.1\n\
         realm={onion}\n\
         use-auth-secret\n\
         static-auth-secret={secret}\n\
         # Tor carries no UDP, so refuse UDP on the client leg. Same-box calls\n\
         # forward relay->relay on loopback and never ask Tor to carry UDP.\n\
         no-udp\n\
         # The co-located LiveKit SFU is a loopback peer (127.0.0.1): the relayed\n\
         # group-call media is forwarded to it locally. coturn forbids loopback\n\
         # peers by default (verified: 403 Forbidden IP), so allow them — safe on a\n\
         # single-user appliance where the only loopback peer is our own SFU and\n\
         # the relay still requires a valid auth-secret credential.\n\
         allow-loopback-peers\n\
         no-multicast-peers\n\
         no-cli\n\
         no-stdout-log\n\
         fingerprint\n\
         total-quota=200\n\
         user-quota=20\n\
         log-file=stdout\n",
        turn = TURN_PORT + off(),
        min = TURN_RELAY_PORT_MIN + off(),
        max = TURN_RELAY_PORT_MAX + off(),
    )
}

pub fn render_turnserver(app: &AppHandle, onion: &str, turn_secret: &str) -> Result<(), String> {
    let p = paths(app)?;
    let conf = turnserver_conf_string(onion, turn_secret);
    std::fs::write(&p.turnserver_conf, conf)
        .map_err(|e| format!("couldn't write turnserver.conf: {e}"))?;
    // Carries the static-auth-secret — owner-only.
    set_0600(&p.turnserver_conf);
    Ok(())
}

/// Pure builder for the fed-proxy Caddyfile — unit-tested. Enforces the
/// paired-peer allowlist: key-exchange/discovery is open (peers need it before
/// they can be allowlisted); the authenticated federation API is allowed only
/// when the X-Matrix `Authorization` origin is a paired peer, enforced by a
/// loopback `forward_auth` validator (see [`crate::fedauth`]); everything else is
/// 403. With no peers the validator's allowlist is empty, so all authenticated
/// federation is refused.
fn caddyfile_string(
    caddy_port: u16,
    hs_port: u16,
    cert: &str,
    key: &str,
    _peers: &[String],
    voice: bool,
    // Path to the unpacked Element Call bundle, if we have one to serve.
    ec_root: Option<&str>,
) -> String {
    // Loopback listeners shift per instance; onion ports (8448/7443) map to these.
    let caddy_port = caddy_port + off();
    let hs_port = hs_port + off();
    let fedauth_port = FEDAUTH_PORT + off();
    // Admin endpoint shifts too, so co-hosted boxes don't clobber each other's
    // running config through the shared default :2019 (see CADDY_ADMIN_PORT).
    let admin_port = CADDY_ADMIN_PORT + off();
    // Every site binds 127.0.0.1 (below): the box is reached ONLY via tor, which maps
    // each onion port to the loopback listener. Without an explicit bind Caddy listens
    // on all interfaces (0.0.0.0 + public IPv6), which would (a) reach these services
    // off-Tor over the LAN/internet and (b) LEAK the onion↔clearnet link — the fed cert's
    // SAN is the .onion, so a clearnet TLS probe would tie the box's real IP to its
    // hidden-service identity. bind 127.0.0.1 keeps it Tor-only. (Security review, 2026-07-24.)
    // Authenticated federation is gated by a loopback validator (fedauth.rs) via
    // forward_auth: it parses the X-Matrix Authorization header and exact-matches
    // the canonical `origin` param against the live pairings allowlist, returning
    // 200 (proxy on to tuwunel) or 403. This REPLACES the old `@paired
    // header_regexp` matcher, which substring-matched the whole header and let an
    // unpaired box bury `origin=<paired>` in a junk/sig param to bypass the gate
    // (and couldn't be anchored — tuwunel doesn't emit origin first). The validator
    // re-reads pairings.json per request, so the allowlist is live with no Caddy
    // reload, and the empty-allowlist case denies everything by construction. The
    // `_peers` arg is no longer needed in the Caddyfile but kept on the signature
    // for callers. The `route` block forces forward_auth to run before the proxy.
    let mut s = format!(
        "{{\n\
         \tadmin 127.0.0.1:{admin_port}\n\
         \tauto_https off\n\
         }}\n\
         https://:{caddy_port} {{\n\
         \tbind 127.0.0.1\n\
         \ttls {cert} {key}\n\
         \t@open path /_matrix/key/* /_matrix/federation/v1/version /_matrix/federation/v1/openid/* /.well-known/*\n\
         \thandle @open {{\n\
         \t\treverse_proxy http://127.0.0.1:{hs_port}\n\
         \t}}\n\
         \thandle {{\n\
         \t\troute {{\n\
         \t\t\tforward_auth http://127.0.0.1:{fedauth_port} {{\n\
         \t\t\t\turi /check\n\
         \t\t\t}}\n\
         \t\t\treverse_proxy http://127.0.0.1:{hs_port}\n\
         \t\t}}\n\
         \t}}\n\
         }}\n"
    );
    if voice {
        // Second site: TLS-terminate the wss SFU signaling path and reverse-proxy
        // the WS upgrade to LiveKit. NOT allowlist-gated — call participants are
        // authed by the LiveKit JWT (minted by lk-jwt after validating their
        // Matrix OpenID token), not by federation origin. Caddy's reverse_proxy
        // handles the WebSocket upgrade automatically. Reuses the same onion
        // self-signed cert as the federation site. tor maps onion 7443 here.
        let _ = write!(
            s,
            "https://:{wss_port} {{\n\
             \tbind 127.0.0.1\n\
             \ttls {cert} {key}\n\
             \treverse_proxy http://127.0.0.1:{livekit}\n\
             }}\n",
            wss_port = CADDY_WSS_PORT + off(),
            cert = cert,
            key = key,
            livekit = LIVEKIT_WS_PORT + off(),
        );
        // TLS lk-jwt + client-API sites: Element Call in the phone WebView reaches
        // the box over Tor's HTTP CONNECT tunnel (TLS-only), so these onion services
        // must be TLS, not plain http. Same self-signed onion cert as above. lk-jwt
        // is authed by the Matrix OpenID token it validates; the client API by access
        // tokens — so neither is allowlist-gated. tor maps onion 8443/8009 here.
        let _ = write!(
            s,
            "https://:{jwt_port} {{\n\
             \tbind 127.0.0.1\n\
             \ttls {cert} {key}\n\
             \treverse_proxy http://127.0.0.1:{lkjwt}\n\
             }}\n\
             https://:{hs_tls_port} {{\n\
             \tbind 127.0.0.1\n\
             \ttls {cert} {key}\n\
             \treverse_proxy http://127.0.0.1:{hs}\n\
             }}\n",
            jwt_port = CADDY_JWT_PORT + off(),
            hs_tls_port = CADDY_HS_PORT + off(),
            cert = cert,
            key = key,
            lkjwt = LKJWT_PORT + off(),
            hs = HOMESERVER_PORT + off(),
        );
        // Self-hosted Element Call (feature J): serve the static bundle off the box so a call
        // pulls NOTHING from the clearnet. Only published when the bundle is actually present,
        // so a box without it still starts (it just can't do group calls).
        if let Some(root) = ec_root {
            let _ = write!(
                s,
                "https://:{ec_port} {{\n\
                 \tbind 127.0.0.1\n\
                 \ttls {cert} {key}\n\
                 \troot * {root}\n\
                 \tfile_server\n\
                 }}\n",
                ec_port = CADDY_EC_PORT + off(),
                cert = cert,
                key = key,
                root = root,
            );
        }
    }
    s
}

/// Render the fed-proxy Caddyfile from the current pairings.
pub fn render_caddyfile(app: &AppHandle, peers: &[String], voice: bool) -> Result<(), String> {
    let p = paths(app)?;
    // Serve Element Call from the box when the bundle is present (installer-bundled or fetched).
    let ec = crate::supervisor::bin_dir(app).ok().map(|d| d.join(EC_DIR_NAME));
    let ec_root = ec
        .as_ref()
        .filter(|d| d.join("index.html").is_file())
        .map(|d| d.display().to_string());
    let conf = caddyfile_string(
        FEDPROXY_PORT,
        HOMESERVER_PORT,
        &p.fed_cert.display().to_string(),
        &p.fed_key.display().to_string(),
        peers,
        voice,
        ec_root.as_deref(),
    );
    std::fs::write(&p.caddyfile, conf).map_err(|e| format!("couldn't write Caddyfile: {e}"))
}

/// Mint a long-lived coturn REST credential (the `use-auth-secret` scheme):
/// `username = <unix-expiry>`, `credential = base64(HMAC-SHA1(secret, username))`.
/// coturn validates this without any stored user, so the LiveKit SFU can
/// authenticate to coturn with a *static* username/credential pair (LiveKit's
/// `turn_servers` config can't compute time-limited REST creds itself).
///
/// The expiry MUST fit in 32 bits: coturn parses the REST timestamp into a 32-bit
/// time and a value past 2^31 overflows → it silently treats the request as a
/// long-term-cred lookup, fails to find the user, and 401s. (Verified live: a
/// year-3000 expiry "Cannot complete Allocation"; 2147483647 authenticates.) So
/// we use the 32-bit max, 2147483647 = 2038-01-19 — ~12 years, far longer than
/// any appliance lifecycle, and never needs rotation in practice.
fn turn_rest_credential(secret: &str) -> (String, String) {
    use base64::Engine as _;
    use hmac::{Hmac, Mac};
    use sha1::Sha1;
    // 32-bit-max unix time (2038-01-19). A larger value overflows coturn's REST
    // parser and the credential is rejected — see the doc comment above.
    let username = "2147483647".to_string();
    let mut mac =
        Hmac::<Sha1>::new_from_slice(secret.as_bytes()).expect("HMAC accepts any key length");
    mac.update(username.as_bytes());
    let credential = base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes());
    (username, credential)
}

/// Pure builder for livekit.yaml — unit-tested. Tor-only: TCP-only because Tor
/// carries no UDP; loopback/host ICE candidates leak + are unreachable over the
/// onion. The SFU's own embedded TURN stays off — instead we **force-relay all
/// media through the coturn-at-onion** by advertising it in `rtc.turn_servers`.
/// That's the proven media-over-Tor path: clients reach coturn over Tor (the
/// only Tor leg), and the SFU↔coturn leg is local. See
/// `docs/redesign/2026-06-media-over-tor.md` (TURN-relay-over-onion, 0% loss).
/// The api_key/secret pair is shared with lk-jwt so its JWTs are accepted; the
/// turn_secret is the same one coturn enforces (use-auth-secret).
fn livekit_yaml_string(
    ws_port: u16,
    tcp_port: u16,
    api_key: &str,
    api_secret: &str,
    onion: &str,
    turn_secret: &str,
) -> String {
    // Loopback ports shift per instance (the wss onion 7443 / turn onion 3478
    // stay standard). UDP port is the local coturn-relay -> SFU hop (not over Tor).
    let ws_port = ws_port + off();
    let tcp_port = tcp_port + off();
    let udp_port = tcp_port + 1;
    let mut s = format!(
        "# Privacy Lodge LiveKit SFU config (generated, do not edit).\n\
         # Tor-only mode: TCP fallback only, since UDP cannot traverse a hidden service.\n\
         port: {ws_port}\n\
         bind_addresses:\n\
         \x20 - 127.0.0.1\n\
         \n\
         rtc:\n\
         \x20 tcp_port: {tcp_port}\n\
         \x20 # A UDP port + loopback candidate so the coturn relay's UDP leg has a\n\
         \x20 # local SFU port to deliver the relayed media to. The client leg still\n\
         \x20 # rides Tor (TCP) to coturn; this is only the local relay->SFU hop.\n\
         \x20 udp_port: {udp_port}\n\
         \x20 use_external_ip: false\n\
         \x20 # Announce 127.0.0.1 as the SFU's ICE candidate. The SFU BINDS to\n\
         \x20 # loopback (bind_addresses), but node-IP auto-detection would otherwise\n\
         \x20 # advertise a docker/LAN IP it isn't listening on — so coturn relays the\n\
         \x20 # client's media to a dead address and ICE never completes (verified:\n\
         \x20 # 'removing participant without connection'). Forcing the announced IP to\n\
         \x20 # match the loopback bind makes the coturn-relay->SFU hop land correctly.\n\
         \x20 node_ip: 127.0.0.1\n\
         \x20 enable_loopback_candidate: true\n\
         \x20 # Gather ONLY the loopback candidate. Without this the SFU also offers\n\
         \x20 # docker/LAN/IPv6 host candidates; the relay-only phone wastes its ICE\n\
         \x20 # check budget on those unreachable pairs and times out before it\n\
         \x20 # nominates the working loopback pair (call connects, then drops). This\n\
         \x20 # also stops the box leaking its LAN/public IP as an ICE candidate.\n\
         \x20 ips:\n\
         \x20   includes:\n\
         \x20     - 127.0.0.1/32\n"
    );
    // Advertise the coturn-at-onion to clients so they gather a *relay* candidate
    // (the only ICE candidate type that survives Tor). Plaintext `turn:` over TCP
    // is fine — the onion is the encryption layer. Only rendered with a real
    // onion + secret; a placeholder onion would hand clients a dead TURN URI.
    if !onion.ends_with("placeholder.onion") && !turn_secret.is_empty() {
        let (user, cred) = turn_rest_credential(turn_secret);
        let _ = write!(
            s,
            "\x20 turn_servers:\n\
             \x20   - host: {onion}\n\
             \x20     port: 3478\n\
             \x20     protocol: tcp\n\
             \x20     username: \"{user}\"\n\
             \x20     credential: \"{cred}\"\n"
        );
    }
    let _ = write!(
        s,
        "\n\
         keys:\n\
         \x20 {api_key}: {api_secret}\n\
         \n\
         logging:\n\
         \x20 level: info\n\
         \x20 json: false\n\
         \n\
         turn:\n\
         \x20 enabled: false\n"
    );
    s
}

/// Render livekit.yaml for the group-call SFU sidecar. Needs the shared
/// api_key/secret (generated at setup, persisted with the other secrets), plus
/// the onion + turn_secret so the SFU force-relays media through coturn-at-onion.
pub fn render_livekit_yaml(
    app: &AppHandle,
    api_key: &str,
    api_secret: &str,
    onion: &str,
    turn_secret: &str,
) -> Result<(), String> {
    let p = paths(app)?;
    let conf = livekit_yaml_string(
        LIVEKIT_WS_PORT,
        LIVEKIT_TCP_PORT,
        api_key,
        api_secret,
        onion,
        turn_secret,
    );
    std::fs::write(&p.livekit_yaml, conf)
        .map_err(|e| format!("couldn't write livekit.yaml: {e}"))?;
    // Carries the LiveKit api secret + the coturn REST credential — owner-only.
    set_0600(&p.livekit_yaml);
    Ok(())
}

/// Mint the fed-proxy's self-signed TLS cert (CN = the onion). Peers accept it
/// because they federate with `allow_invalid_tls_certificates` (onion-only).
/// Idempotent: only generates if the cert is missing.
pub fn ensure_fed_cert(app: &AppHandle, onion: &str) -> Result<(), String> {
    let p = paths(app)?;
    if p.fed_cert.is_file() && p.fed_key.is_file() {
        return Ok(());
    }
    let certified = rcgen::generate_simple_self_signed(vec![onion.to_string()])
        .map_err(|e| format!("couldn't mint federation cert: {e}"))?;
    std::fs::write(&p.fed_cert, certified.cert.pem())
        .map_err(|e| format!("couldn't write fed cert: {e}"))?;
    std::fs::write(&p.fed_key, certified.key_pair.serialize_pem())
        .map_err(|e| format!("couldn't write fed key: {e}"))?;
    set_0600(&p.fed_key);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn relay_count() -> usize {
        (TURN_RELAY_PORT_MAX - TURN_RELAY_PORT_MIN + 1) as usize
    }

    #[test]
    fn torrc_publishes_every_relay_port_plus_the_fixed_ones() {
        let torrc = torrc_string(9150, "/d", "/d/hs", 8118, 8449, false, "/d/hs-agent");
        let lines = torrc.matches("HiddenServicePort").count();
        // 8448 + 8008 + 80 + 3478 + 5349 fixed, the agent WebUI, plus one per relay port.
        assert_eq!(lines, 6 + relay_count());
        // The agent WebUI is mapped on its OWN hidden service, never on the main onion —
        // that address is shared with every paired peer box, and this port fronts an agent
        // control plane that can run shell commands.
        assert!(torrc.contains("HiddenServiceDir /d/hs-agent"));
        assert!(torrc.contains(&format!(
            "HiddenServicePort {AGENT_WEBUI_ONION_PORT} 127.0.0.1:{AGENT_WEBUI_PORT}"
        )));
        assert!(torrc.contains("SocksPort 9150 NoIsolateClientAddr"));
        // Federation (8448) goes to the fed-proxy; client API (8008 and 80) to tuwunel.
        // Port 80 too: matrix-rust-sdk derives some calls (account data) from the bare
        // server_name, hitting the onion on the default http port.
        assert!(torrc.contains("HiddenServicePort 8448 127.0.0.1:8449"));
        assert!(torrc.contains("HiddenServicePort 8008 127.0.0.1:8118"));
        assert!(torrc.contains("HiddenServicePort 80 127.0.0.1:8118"));
        // The coturn relay range and torrc MUST agree port-for-port.
        for p in [TURN_RELAY_PORT_MIN, TURN_RELAY_PORT_MAX] {
            assert!(torrc.contains(&format!("HiddenServicePort {p} 127.0.0.1:{p}")));
        }
    }

    #[test]
    fn torrc_maps_group_call_ports_only_when_voice_enabled() {
        // voice=true publishes the wss (7443→caddy 7444), media (7881), and
        // lk-jwt (8082) onion ports on top of the base map.
        let with = torrc_string(9150, "/d", "/d/hs", 8118, 8449, true, "/d/hs-agent");
        assert!(with.contains(&format!(
            "HiddenServicePort {LIVEKIT_WSS_ONION_PORT} 127.0.0.1:{CADDY_WSS_PORT}"
        )));
        assert!(with.contains(&format!(
            "HiddenServicePort {LIVEKIT_TCP_PORT} 127.0.0.1:{LIVEKIT_TCP_PORT}"
        )));
        assert!(with.contains(&format!("HiddenServicePort {LKJWT_PORT} 127.0.0.1:{LKJWT_PORT}")));

        // voice=false omits all three.
        let without = torrc_string(9150, "/d", "/d/hs", 8118, 8449, false, "/d/hs-agent");
        assert!(!without.contains(&format!("127.0.0.1:{CADDY_WSS_PORT}")));
        assert!(!without.contains(&format!("HiddenServicePort {LIVEKIT_TCP_PORT}")));
        assert!(!without.contains(&format!("HiddenServicePort {LKJWT_PORT}")));
    }

    #[test]
    fn caddyfile_gates_federation_via_forward_auth() {
        let cf = caddyfile_string(8449, 8118, "/c.pem", "/k.pem", &[], false, None);
        // Open (unauthenticated) federation paths bypass the allowlist.
        assert!(cf.contains("@open path /_matrix/key/*"));
        // openid MUST be open — lk-jwt's cross-box validation hits it with no X-Matrix
        // origin header, so the auth gate can't match it.
        assert!(cf.contains("/_matrix/federation/v1/openid/*"));
        // Authenticated federation is gated by the loopback forward_auth validator
        // (fedauth.rs), which parses the canonical `origin` and 200/403s it against the
        // live pairings allowlist — replacing the substring-bypassable header_regexp.
        assert!(cf.contains(&format!("forward_auth http://127.0.0.1:{}", FEDAUTH_PORT + off())));
        assert!(cf.contains("uri /check"));
        assert!(cf.contains("reverse_proxy http://127.0.0.1:8118"));
        // Per-instance admin port so co-hosted boxes don't clobber each other's config.
        assert!(cf.contains(&format!("admin 127.0.0.1:{}", CADDY_ADMIN_PORT + off())));
        // The old bypassable matcher and its static 403 handler are gone.
        assert!(!cf.contains("@paired header_regexp"));
        // Security review: the federation site binds loopback (tor-only), not all
        // interfaces — else the fed cert (SAN = the .onion) would be served on the
        // clearnet LAN/IPv6 and leak the onion<->IP link. Exactly one site w/o voice.
        assert!(cf.contains("bind 127.0.0.1"));
        assert_eq!(cf.matches("bind 127.0.0.1").count(), 1);

        // The Caddyfile no longer varies by peers — the validator reads pairings.json
        // live, so allowlist changes need no Caddy reload and deny-by-default (empty
        // allowlist) lives in fedauth.rs. Same config with or without peers.
        let with_peers =
            caddyfile_string(8449, 8118, "/c.pem", "/k.pem", &["aaa.onion".into()], false, None);
        assert_eq!(cf, with_peers);
    }

    /// Feature J: the box serves Element Call itself, so a call makes no clearnet request.
    /// If this regresses, the phone silently goes back to fetching the bundle from
    /// call.element.io — contradicting the privacy policy — so it is pinned here.
    #[test]
    fn caddyfile_serves_element_call_from_the_box_when_the_bundle_is_present() {
        let with = caddyfile_string(8449, 8118, "/c.pem", "/k.pem", &[], true, Some("/opt/ec"));
        assert!(with.contains(&format!("https://:{} {{", CADDY_EC_PORT + off())));
        assert!(with.contains("root * /opt/ec"));
        assert!(with.contains("file_server"));
        // Loopback-only like every other site (security review): tor reaches it, the LAN can't.
        assert_eq!(with.matches("bind 127.0.0.1").count(), 5);

        // No bundle -> no site, and the box still starts (it just can't do group calls).
        let without = caddyfile_string(8449, 8118, "/c.pem", "/k.pem", &[], true, None);
        assert!(!without.contains("file_server"));
        assert!(!without.contains(&format!("https://:{} {{", CADDY_EC_PORT + off())));
    }

    #[test]
    fn torrc_publishes_the_element_call_onion_port_with_voice() {
        let with = torrc_string(9150, "/d", "/hs", 8118, 8449, true, "/d/hs-agent");
        assert!(with.contains(&format!(
            "HiddenServicePort {EC_TLS_ONION_PORT} 127.0.0.1:{}", CADDY_EC_PORT
        )));
        let without = torrc_string(9150, "/d", "/hs", 8118, 8449, false, "/d/hs-agent");
        assert!(!without.contains(&format!("HiddenServicePort {EC_TLS_ONION_PORT}")));
    }

    #[test]
    fn caddyfile_adds_wss_sfu_site_only_when_voice_enabled() {
        // voice=true appends a SECOND site on :7444 reverse-proxying LiveKit's
        // signaling WS (:7880). It is NOT allowlist-gated (JWT-authed).
        let cf = caddyfile_string(8449, 8118, "/c.pem", "/k.pem", &[], true, None);
        assert!(cf.contains(&format!("https://:{CADDY_WSS_PORT} {{")));
        assert!(cf.contains(&format!("reverse_proxy http://127.0.0.1:{LIVEKIT_WS_PORT}")));
        // The wss site still keeps the federation site intact below it.
        assert!(cf.contains(&format!("https://:{FEDPROXY_PORT} {{")));
        // All four voice-mode sites (fed + wss + lk-jwt + client-API) bind loopback.
        assert_eq!(cf.matches("bind 127.0.0.1").count(), 4);

        // voice=false → no wss site at all.
        let without = caddyfile_string(8449, 8118, "/c.pem", "/k.pem", &[], false, None);
        assert!(!without.contains(&format!("https://:{CADDY_WSS_PORT}")));
        assert!(!without.contains(&format!("reverse_proxy http://127.0.0.1:{LIVEKIT_WS_PORT}")));
    }

    #[test]
    fn livekit_yaml_is_tcp_only_with_the_shared_keys() {
        let yaml = livekit_yaml_string(7880, 7881, "lkkey", "lksecret", "abc123.onion", "deadbeef");
        // TCP client leg (Tor carries no UDP) + a local UDP port & loopback candidate
        // so the coturn relay's UDP hop can reach the co-located SFU.
        assert!(yaml.contains("tcp_port: 7881"));
        assert!(yaml.contains("udp_port: 7882"));
        assert!(yaml.contains("use_external_ip: false"));
        // Announce loopback (match the bind) so coturn relays media to a live port,
        // and gather ONLY the loopback candidate (no docker/LAN/public-IP leak).
        assert!(yaml.contains("node_ip: 127.0.0.1"));
        assert!(yaml.contains("enable_loopback_candidate: true"));
        assert!(yaml.contains("127.0.0.1/32"));
        // The shared api_key: api_secret pair lk-jwt also signs with.
        assert!(yaml.contains("lkkey: lksecret"));
        // Built-in TURN stays off — we relay over Tor, not LiveKit's TURN.
        assert!(yaml.contains("enabled: false"));
        assert!(yaml.contains("port: 7880"));
        // Force-relay: the onion coturn is advertised so clients gather a relay
        // candidate (the only ICE type that survives Tor).
        assert!(yaml.contains("turn_servers:"));
        assert!(yaml.contains("host: abc123.onion"));
        assert!(yaml.contains("protocol: tcp"));
    }

    #[test]
    fn livekit_turn_servers_omitted_without_real_onion_or_secret() {
        // Placeholder onion (pre-mint) → no dead TURN URI handed to clients.
        let pre = livekit_yaml_string(7880, 7881, "k", "s", "placeholder.onion", "deadbeef");
        assert!(!pre.contains("turn_servers:"));
        // No secret yet → no TURN block.
        let nosec = livekit_yaml_string(7880, 7881, "k", "s", "abc123.onion", "");
        assert!(!nosec.contains("turn_servers:"));
    }

    #[test]
    fn turn_rest_credential_is_deterministic_hmac() {
        // Same secret → same long-lived credential (so reboots don't churn it),
        // and the username is the far-future expiry coturn validates against.
        let (u1, c1) = turn_rest_credential("deadbeef");
        let (u2, c2) = turn_rest_credential("deadbeef");
        // 32-bit-max expiry — a larger value overflows coturn's REST parser.
        assert_eq!(u1, "2147483647");
        assert_eq!((u1, c1.clone()), (u2, c2));
        // Different secret → different credential.
        let (_, c3) = turn_rest_credential("other");
        assert_ne!(c1, c3);
        assert!(!c1.is_empty());
    }

    #[test]
    fn tuwunel_advertises_turn_only_with_secret_and_real_onion() {
        let onion = "abc123.onion";
        let with = tuwunel_toml_string(onion, "/db", 8118, 9150, "deadbeef", "jointok", false);
        assert!(with.contains("turn_uris = [\"turn:abc123.onion:3478?transport=tcp\"]"));
        assert!(with.contains("turn_secret = \"deadbeef\""));
        assert!(with.contains("socks5h://127.0.0.1:9150"));

        // No secret yet → no turn block.
        let without = tuwunel_toml_string(onion, "/db", 8118, 9150, "", "jointok", false);
        assert!(!without.contains("turn_uris"));

        // Placeholder server_name (pre-mint) → never advertise turn.
        let placeholder = tuwunel_toml_string("placeholder.onion", "/db", 8118, 9150, "deadbeef", "jointok", false);
        assert!(!placeholder.contains("turn_uris"));
    }

    #[test]
    fn tuwunel_advertises_well_known_livekit_only_with_voice_and_real_onion() {
        let onion = "abc123.onion";
        // voice=true + real onion → well_known with client + livekit_url.
        let with = tuwunel_toml_string(onion, "/db", 8118, 9150, "deadbeef", "jointok", true);
        assert!(with.contains("[global.well_known]"));
        assert!(with.contains("client = \"http://abc123.onion\""));
        assert!(with.contains(&format!("livekit_url = \"https://abc123.onion:{LKJWT_TLS_ONION_PORT}\"")));

        // voice=false → no well_known block.
        let no_voice = tuwunel_toml_string(onion, "/db", 8118, 9150, "deadbeef", "jointok", false);
        assert!(!no_voice.contains("[global.well_known]"));
        assert!(!no_voice.contains("livekit_url"));

        // Placeholder onion (pre-mint) → never advertise even with voice=true.
        let placeholder =
            tuwunel_toml_string("placeholder.onion", "/db", 8118, 9150, "deadbeef", "jointok", true);
        assert!(!placeholder.contains("[global.well_known]"));
        assert!(!placeholder.contains("livekit_url"));
    }

    #[test]
    fn tuwunel_gates_registration_on_a_token_never_open() {
        let with = tuwunel_toml_string("abc.onion", "/db", 8118, 9150, "", "jointok123", false);
        assert!(with.contains("allow_registration = true"));
        assert!(with.contains("registration_token = \"jointok123\""));

        // No token (e.g. pre-setup placeholder) → registration stays absent
        // (tuwunel defaults registration OFF), never an open-reg server.
        let without = tuwunel_toml_string("placeholder.onion", "/db", 8118, 9150, "", "", false);
        assert!(!without.contains("allow_registration"));
        assert!(!without.contains("registration_token"));
    }

    #[test]
    fn turnserver_conf_scopes_to_the_onion_and_refuses_udp() {
        let conf = turnserver_conf_string("abc123.onion", "s3cr3t");
        assert!(conf.contains("realm=abc123.onion"));
        // relay address is loopback (reachable by the co-located SFU), not the onion.
        assert!(conf.contains("external-ip=127.0.0.1"));
        assert!(conf.contains("relay-ip=127.0.0.1"));
        // Security review: the STUN/TURN control listener binds loopback only (tor-only),
        // not all interfaces — else the realm (= the .onion) leaks to clearnet probes.
        assert!(conf.contains("listening-ip=127.0.0.1"));
        assert!(conf.contains("static-auth-secret=s3cr3t"));
        assert!(conf.contains("no-udp"));
        // Co-located SFU is a loopback peer — must be permitted.
        assert!(conf.contains("allow-loopback-peers"));
        assert!(conf.contains(&format!("min-port={TURN_RELAY_PORT_MIN}")));
        assert!(conf.contains(&format!("max-port={TURN_RELAY_PORT_MAX}")));
    }
}
