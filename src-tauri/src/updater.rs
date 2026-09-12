//! Box auto-update (feature H): check, verify, install — always owner-approved.
//!
//! The box fetches a small `update.json` manifest (+ detached ed25519 signature) over **Tor**,
//! verifies the signature against [`UPDATE_PUBKEY_HEX`] — a key compiled INTO this binary — and
//! only then believes a single field of it. Nothing is downloaded or executed on the strength of
//! an unsigned or badly-signed manifest.
//!
//! What it will NOT do:
//!   * install anything without an explicit approval from the owner's phone (guarded command),
//!   * "update" to a version ≤ the running one (a signed-but-old manifest can't roll a box back
//!     onto a patched bug),
//!   * self-update a Docker box — a container can't replace its own image without host Docker
//!     socket access, which is root-equivalent on the host. Docker boxes are told the command
//!     to run instead (see [`InstallKind`]).

use base64::Engine;
use sha2::{Digest, Sha256};

/// Raw ed25519 public key (32 bytes, hex) of the Privacy Lodge release signing key. The private
/// half lives ONLY in `_special-project/pureprivacy/pp-update-key.pem` — never in this repo, a
/// box, or a backup. Rotating it is deliberately a code change + a release.
pub const UPDATE_PUBKEY_HEX: &str =
    "3fe188d4120f10d5455bf6bed74b668af477959c3a9376701df2436c8f98ce7d";

/// Post-quantum half of the update trust anchor (feature K): SLH-DSA-SHA2-128s public key, hex.
/// Shor's algorithm breaks Ed25519 outright, and an adversary who forges an update signature
/// doesn't read one conversation — they push a malicious box to every user. So a manifest must
/// carry BOTH signatures and satisfy BOTH: we only lose if ed25519 AND a hash-based scheme fall
/// together. Filled in by `pl-sign keygen`; empty disables the PQ requirement (see below).
pub const UPDATE_PQ_PUBKEY_HEX: &str =
    "b8bf68eb03c2418e93d363e8ce6cb08bfabf3988a19d338fa5be80043c539b1d";

/// Whether a manifest MUST carry a valid post-quantum signature.
///
/// Kept as an explicit switch because it is a hard cutover: with this on, a box refuses any
/// release that isn't hybrid-signed — which fails CLOSED (the box simply doesn't update), never
/// open. Turn it on once every published release carries a PQ signature. Note that leaving it
/// off is not merely "less secure later": an attacker who breaks ed25519 could strip the PQ
/// signature and present an ed25519-only manifest, so the PQ half only actually protects you
/// once this is enforced.
pub const REQUIRE_PQ_SIGNATURE: bool = !UPDATE_PQ_PUBKEY_HEX.is_empty();

/// Where the signed manifest lives. `latest/download/<asset>` always resolves to the newest
/// published release, so the box needs no API token and no release enumeration.
const MANIFEST_URL: &str =
    "https://github.com/jaimetournesol/privacy-lodge/releases/latest/download/update.json";
const SIGNATURE_URL: &str =
    "https://github.com/jaimetournesol/privacy-lodge/releases/latest/download/update.json.sig";
/// SLH-DSA signature over the same bytes (feature K). Required when [`REQUIRE_PQ_SIGNATURE`].
const PQ_SIGNATURE_URL: &str =
    "https://github.com/jaimetournesol/privacy-lodge/releases/latest/download/update.json.pqsig";

/// Where a user goes when their platform has no self-installable build in the manifest.
pub const RELEASES_PAGE: &str = "https://github.com/jaimetournesol/privacy-lodge/releases/latest";

/// We publish installers for Linux only, because the box's essential sidecars (tuwunel, the
/// homeserver, and lk-jwt) have linux-gnu builds ONLY. Anywhere else, the supported way to run
/// a box is Docker — so an unsupported-platform box is diverted there rather than sent to a
/// releases page that has nothing it can use.
pub const DOCKER_IMAGE: &str = "jaimemelon/privacy-lodge-box:latest";

/// The command that moves a box onto the supported (Docker) path on an OS we don't build for.
pub fn docker_migrate_command() -> String {
    format!("docker pull {DOCKER_IMAGE}")
}

/// Refuse absurdly large downloads outright (manifest is ~1 KB; a box binary is tens of MB).
const MAX_MANIFEST_BYTES: usize = 64 * 1024;
const MAX_BINARY_BYTES: u64 = 512 * 1024 * 1024;

/// How this box was installed — decides whether "install" is even possible in-process.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstallKind {
    /// Native binary (GUI app): the box can swap its own binary and restart.
    Native,
    /// Docker container: self-install is deliberately NOT supported (no Docker socket).
    Docker,
}

impl InstallKind {
    pub fn detect() -> Self {
        // Both are set by our own image; /.dockerenv is the generic container marker.
        if std::path::Path::new("/.dockerenv").exists()
            || matches!(crate::envcompat::var("BIN_DIR").as_deref(), Ok("/opt/privacy-lodge/bin") | Ok("/opt/pureprivacy/bin"))
        {
            InstallKind::Docker
        } else {
            InstallKind::Native
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            InstallKind::Native => "native",
            InstallKind::Docker => "docker",
        }
    }
}

/// A verified update manifest. Only ever constructed AFTER the signature checks out.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct Manifest {
    pub version: String,
    #[serde(default)]
    pub released: String,
    #[serde(default)]
    pub notes: Vec<String>,
    #[serde(default)]
    pub docker: Option<DockerRelease>,
    #[serde(default)]
    pub native: std::collections::HashMap<String, NativeRelease>,
}

#[derive(Debug, Clone, serde::Deserialize)]
pub struct DockerRelease {
    pub image: String,
    /// The agents add-on image for this release. Optional: manifests before 0.1.11 don't
    /// carry it, and a plain box doesn't need it. When present it's the only SIGNED
    /// statement of which agent image belongs with this box version.
    #[serde(default)]
    pub agent_image: Option<String>,
}

#[derive(Debug, Clone, serde::Deserialize)]
pub struct NativeRelease {
    pub url: String,
    pub sha256: String,
    #[serde(default)]
    pub size: u64,
}

/// The build target key used in `manifest.native` for this box.
pub fn native_target() -> String {
    format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH)
}

/// Verify the post-quantum (SLH-DSA) signature over `payload`. Fails CLOSED.
pub fn verify_pq_signature(payload: &[u8], sig_b64: &str) -> Result<(), String> {
    use fips205::slh_dsa_sha2_128s;
    use fips205::traits::{SerDes, Verifier};

    if UPDATE_PQ_PUBKEY_HEX.is_empty() {
        return Err("no post-quantum public key is compiled in".into());
    }
    let pk_bytes = hex_to_vec(UPDATE_PQ_PUBKEY_HEX).ok_or("bad baked-in PQ public key")?;
    let pk_arr: [u8; slh_dsa_sha2_128s::PK_LEN] = pk_bytes
        .as_slice()
        .try_into()
        .map_err(|_| "baked-in PQ public key is the wrong length")?;
    let pk = slh_dsa_sha2_128s::PublicKey::try_from_bytes(&pk_arr)
        .map_err(|_| "baked-in PQ public key is malformed")?;

    let raw = base64::engine::general_purpose::STANDARD
        .decode(sig_b64.trim())
        .map_err(|_| "PQ signature isn't valid base64")?;
    let sig: [u8; slh_dsa_sha2_128s::SIG_LEN] = raw
        .as_slice()
        .try_into()
        .map_err(|_| "PQ signature is the wrong length")?;
    if pk.verify(payload, &sig, &[]) {
        Ok(())
    } else {
        Err("post-quantum signature does not match the Privacy Lodge release key".into())
    }
}

fn hex_to_vec(s: &str) -> Option<Vec<u8>> {
    let s = s.trim();
    if s.len() % 2 != 0 { return None; }
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(s.get(i * 2..i * 2 + 2)?, 16).ok())
        .collect()
}

/// Verify a detached ed25519 signature (base64, 64 raw bytes) over `payload` using the
/// compile-time public key. Fails CLOSED on any malformed input.
pub fn verify_signature(payload: &[u8], sig_b64: &str) -> Result<(), String> {
    use ed25519_dalek::{Signature, Verifier, VerifyingKey};

    let key_bytes = hex_to_32(UPDATE_PUBKEY_HEX).ok_or("bad baked-in public key")?;
    let key = VerifyingKey::from_bytes(&key_bytes).map_err(|_| "bad baked-in public key")?;

    let raw = base64::engine::general_purpose::STANDARD
        .decode(sig_b64.trim())
        .map_err(|_| "signature isn't valid base64")?;
    let sig_bytes: [u8; 64] = raw
        .as_slice()
        .try_into()
        .map_err(|_| "signature isn't 64 bytes")?;
    key.verify(payload, &Signature::from_bytes(&sig_bytes))
        .map_err(|_| "signature does not match the Privacy Lodge release key".to_string())
}

fn hex_to_32(s: &str) -> Option<[u8; 32]> {
    let s = s.trim();
    if s.len() != 64 {
        return None;
    }
    let mut out = [0u8; 32];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(s.get(i * 2..i * 2 + 2)?, 16).ok()?;
    }
    Some(out)
}

/// Compare dotted numeric versions ("0.1.10" > "0.1.9"). Non-numeric parts sort as 0, so a
/// malformed version can never appear NEWER than a well-formed one.
pub fn is_newer(candidate: &str, current: &str) -> bool {
    fn parts(v: &str) -> Vec<u64> {
        v.trim()
            .trim_start_matches('v')
            .split(['.', '-', '+'])
            .map(|p| p.parse::<u64>().unwrap_or(0))
            .collect()
    }
    let (a, b) = (parts(candidate), parts(current));
    for i in 0..a.len().max(b.len()) {
        let (x, y) = (a.get(i).copied().unwrap_or(0), b.get(i).copied().unwrap_or(0));
        if x != y {
            return x > y;
        }
    }
    false
}

/// Parse + verify a manifest from raw bytes and its base64 signature. This is the ONLY way a
/// [`Manifest`] comes into existence, so an unverified manifest can't reach the rest of the box.
pub fn verify_manifest(payload: &[u8], sig_b64: &str) -> Result<Manifest, String> {
    verify_manifest_hybrid(payload, sig_b64, None)
}

/// Verify a manifest against BOTH trust anchors. `pq_sig_b64` is the SLH-DSA signature; when
/// [`REQUIRE_PQ_SIGNATURE`] is on, its absence or invalidity rejects the manifest outright.
pub fn verify_manifest_hybrid(
    payload: &[u8],
    sig_b64: &str,
    pq_sig_b64: Option<&str>,
) -> Result<Manifest, String> {
    // Classical signature is ALWAYS required — a new PQ scheme never replaces a proven one.
    verify_signature(payload, sig_b64)?;
    if REQUIRE_PQ_SIGNATURE {
        let pq = pq_sig_b64
            .filter(|s| !s.trim().is_empty())
            .ok_or("this release carries no post-quantum signature — refusing")?;
        verify_pq_signature(payload, pq)?;
    }
    let m: Manifest = serde_json::from_slice(payload)
        .map_err(|e| format!("update manifest isn't valid JSON: {e}"))?;
    if m.version.trim().is_empty() {
        return Err("update manifest has no version".into());
    }
    Ok(m)
}

/// The running box's version.
pub fn current_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Fetch the signed manifest over Tor and return it only if it is (a) properly signed and
/// (b) strictly newer than what's running. `Ok(None)` = no update / already current.
pub async fn check(socks_port: u16) -> Result<Option<Manifest>, String> {
    let proxy = reqwest::Proxy::all(format!("socks5h://127.0.0.1:{socks_port}"))
        .map_err(|e| format!("tor proxy unavailable: {e}"))?;
    let client = reqwest::Client::builder()
        .proxy(proxy)
        .timeout(std::time::Duration::from_secs(120))
        .build()
        .map_err(|e| format!("http client: {e}"))?;

    let payload = fetch_bounded(&client, MANIFEST_URL, MAX_MANIFEST_BYTES).await?;
    let sig = fetch_bounded(&client, SIGNATURE_URL, MAX_MANIFEST_BYTES).await?;
    let sig = String::from_utf8(sig).map_err(|_| "signature isn't text")?;

    // Hybrid: the PQ signature is fetched too. Missing/short is treated as absent, and
    // verify_manifest_hybrid decides whether that's fatal (it is, once PQ is required).
    let pq = fetch_bounded(&client, PQ_SIGNATURE_URL, MAX_MANIFEST_BYTES)
        .await
        .ok()
        .and_then(|b| String::from_utf8(b).ok());
    let m = verify_manifest_hybrid(&payload, &sig, pq.as_deref())?;
    if !is_newer(&m.version, current_version()) {
        return Ok(None); // already current (or a stale/downgrade manifest — ignore)
    }
    Ok(Some(m))
}

async fn fetch_bounded(
    client: &reqwest::Client,
    url: &str,
    max: usize,
) -> Result<Vec<u8>, String> {
    let r = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("couldn't reach the update server over Tor: {e}"))?;
    if !r.status().is_success() {
        return Err(format!("update server returned {}", r.status()));
    }
    let bytes = r.bytes().await.map_err(|e| format!("download failed: {e}"))?;
    if bytes.len() > max {
        return Err("update response is implausibly large — refusing".into());
    }
    Ok(bytes.to_vec())
}

/// Download the native binary for this platform, verify its SHA-256 against the (already
/// signature-verified) manifest, and atomically swap it in. Returns the path that was replaced.
///
/// Ordering matters: every check happens BEFORE the rename, so a failure leaves the running
/// box completely untouched.
pub async fn install_native(m: &Manifest, socks_port: u16) -> Result<std::path::PathBuf, String> {
    let target = native_target();
    let rel = m
        .native
        .get(&target)
        .ok_or_else(|| format!("this release has no build for {target}"))?;
    if rel.size > MAX_BINARY_BYTES {
        return Err("update binary is implausibly large — refusing".into());
    }

    let exe = std::env::current_exe().map_err(|e| format!("can't locate my own binary: {e}"))?;
    let dir = exe
        .parent()
        .ok_or("can't locate the install directory")?
        .to_path_buf();

    // Download over Tor.
    let proxy = reqwest::Proxy::all(format!("socks5h://127.0.0.1:{socks_port}"))
        .map_err(|e| format!("tor proxy unavailable: {e}"))?;
    let client = reqwest::Client::builder()
        .proxy(proxy)
        .timeout(std::time::Duration::from_secs(1800))
        .build()
        .map_err(|e| format!("http client: {e}"))?;
    let r = client
        .get(&rel.url)
        .send()
        .await
        .map_err(|e| format!("couldn't download the update over Tor: {e}"))?;
    if !r.status().is_success() {
        return Err(format!("download returned {}", r.status()));
    }
    let bytes = r.bytes().await.map_err(|e| format!("download failed: {e}"))?;

    // Verify size + hash against the SIGNED manifest before anything touches disk permanently.
    if rel.size > 0 && bytes.len() as u64 != rel.size {
        return Err("downloaded update has the wrong size — refusing".into());
    }
    let got = hex_lower(&Sha256::digest(&bytes));
    if !got.eq_ignore_ascii_case(rel.sha256.trim()) {
        return Err("downloaded update failed its checksum — refusing".into());
    }

    // Stage next to the current binary (same filesystem ⇒ rename is atomic), then swap.
    // These two filenames deliberately keep the pre-rename spelling: the 0.1.x binary that
    // installs 0.2.0 writes `pureprivacy.prev`, and the rollback path must find it afterwards.
    let staged = dir.join("pureprivacy.update-staged");
    std::fs::write(&staged, &bytes).map_err(|e| format!("couldn't write the update: {e}"))?;
    set_executable(&staged)?;
    // Keep the outgoing binary so a bad update can be undone by hand.
    let prev = dir.join("pureprivacy.prev");
    let _ = std::fs::remove_file(&prev);
    let _ = std::fs::copy(&exe, &prev);
    std::fs::rename(&staged, &exe).map_err(|e| {
        let _ = std::fs::remove_file(&staged);
        format!("couldn't replace the box binary: {e}")
    })?;
    Ok(exe)
}

#[cfg(unix)]
fn set_executable(p: &std::path::Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o700))
        .map_err(|e| format!("couldn't make the update executable: {e}"))
}
#[cfg(not(unix))]
fn set_executable(_p: &std::path::Path) -> Result<(), String> {
    Ok(())
}

fn hex_lower(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// The exact command a Docker box's owner runs on the HOST to update. The container can't do
/// this itself by design (no Docker socket), so we hand over a copyable command instead.
///
/// It names the VERSION and nothing else. The install — not the box — knows which registry
/// image and which tag it runs (`PL_IMAGE` in `.env`), whether the agents add-on is on, and
/// therefore what to pull: `pl-box update <ver>` pulls exactly those tags, pins them in
/// `.env`, and recreates on the same volume. The earlier shape, `docker pull <image> && … &&
/// ./pl-box update`, never updated a Docker-Hub box at all: compose kept running the tag
/// `.env` named (a versioned pull doesn't retag `:latest`; `up` doesn't re-pull an image it
/// has), and `pl-box update` then died in `build.sh` looking for a Tauri build. The manifest's
/// `docker.image` / `docker.agent_image` remain the signed statement of which images belong
/// to this release; the tags they carry equal `version` by construction (sign-release.sh).
pub fn docker_command(m: &Manifest) -> String {
    format!("cd docker && ./pl-box update {}", m.version)
}

#[cfg(test)]
mod tests {
    use super::*;

    // A manifest signed by the REAL release key (generated 2026-07-25). Proves the baked-in
    // public key matches the private key in _special-project, so a release signed per
    // README-signing.md will actually verify on a box.
    const SIGNED_JSON: &str = r#"{"version":"0.1.3","notes":["test"]}"#;
    const SIGNED_SIG: &str =
        "IcBLWYnPZ5ivWrQjtGVZJFl6j18rcg/7zS0l2fXyTs1sbeySRPT3Vmmm7cTSkN+FzudW/3L/q9qPq+5vYL+UDA==";

    /// End-to-end trust check: a manifest signed by the REAL private key
    /// (`_special-project/pureprivacy/pp-update-key.pem`) verifies against the public key baked
    /// into this binary. If someone rotates one half without the other, this test fails loudly
    /// instead of every box silently refusing updates.
    #[test]
    fn manifest_signed_by_the_real_release_key_verifies() {
        // The ed25519 layer still checks out against the real release key (the hybrid wrapper
        // additionally demands the PQ signature — see the test below).
        verify_signature(SIGNED_JSON.as_bytes(), SIGNED_SIG)
            .expect("release-key signature must verify against the baked-in public key");
    }

    /// ...and the SAME signature must NOT verify once a byte of the payload changes — the
    /// attack this whole module exists to stop (swap the URL/hash, keep the signature).
    #[test]
    fn tampering_with_a_signed_manifest_is_rejected() {
        let tampered = SIGNED_JSON.replace("0.1.3", "9.9.9");
        assert!(verify_manifest(tampered.as_bytes(), SIGNED_SIG).is_err());
        let evil = r#"{"version":"0.1.3","notes":["test"],"native":{"linux-x86_64":{"url":"https://evil/x","sha256":"00","size":1}}}"#;
        assert!(verify_manifest(evil.as_bytes(), SIGNED_SIG).is_err());
    }

    #[test]
    fn version_compare_is_numeric_not_lexical() {
        assert!(is_newer("0.1.10", "0.1.9")); // lexical would say 0.1.10 < 0.1.9
        assert!(is_newer("0.2.0", "0.1.99"));
        assert!(is_newer("1.0.0", "0.9.9"));
        assert!(is_newer("v0.1.3", "0.1.2")); // tolerate a leading v
        // Not newer: equal, older, or garbage.
        assert!(!is_newer("0.1.2", "0.1.2"));
        assert!(!is_newer("0.1.1", "0.1.2"));
        assert!(!is_newer("garbage", "0.1.2")); // unparsable must never look NEWER
        assert!(!is_newer("", "0.1.2"));
    }

    #[test]
    fn unsigned_or_tampered_manifests_are_rejected() {
        let payload = br#"{"version":"9.9.9"}"#;
        // Garbage signature.
        assert!(verify_manifest(payload, "not-base64!!").is_err());
        // Well-formed base64 that isn't a valid signature.
        let fake = base64::engine::general_purpose::STANDARD.encode([7u8; 64]);
        assert!(verify_manifest(payload, &fake).is_err());
        // Right length, wrong content -> still rejected.
        assert!(verify_signature(payload, &fake).is_err());
        // Empty signature.
        assert!(verify_manifest(payload, "").is_err());
    }

    #[test]
    fn baked_pq_public_key_is_well_formed_and_required() {
        use fips205::slh_dsa_sha2_128s as slh;
        use fips205::traits::SerDes;
        // A PQ key is compiled in, so hybrid verification is enforced.
        assert!(!UPDATE_PQ_PUBKEY_HEX.is_empty());
        assert!(REQUIRE_PQ_SIGNATURE, "PQ key present but not enforced — hybrid is a no-op");
        let raw = hex_to_vec(UPDATE_PQ_PUBKEY_HEX).expect("PQ key must be hex");
        assert_eq!(raw.len(), slh::PK_LEN);
        let arr: [u8; slh::PK_LEN] = raw.as_slice().try_into().unwrap();
        assert!(slh::PublicKey::try_from_bytes(&arr).is_ok());
    }

    /// With PQ enforced, an ed25519-only manifest must be REFUSED — otherwise an attacker who
    /// broke ed25519 could simply strip the PQ signature and be believed.
    #[test]
    fn ed25519_only_manifest_is_refused_once_pq_is_required() {
        let r = verify_manifest_hybrid(SIGNED_JSON.as_bytes(), SIGNED_SIG, None);
        assert!(r.is_err(), "a manifest with no PQ signature must not verify");
        assert!(r.unwrap_err().contains("post-quantum"));
        // ...and a garbage PQ signature is no better than none.
        let junk = base64::engine::general_purpose::STANDARD.encode([0u8; 64]);
        assert!(verify_manifest_hybrid(SIGNED_JSON.as_bytes(), SIGNED_SIG, Some(&junk)).is_err());
    }

    #[test]
    fn baked_public_key_is_well_formed() {
        assert_eq!(UPDATE_PUBKEY_HEX.len(), 64);
        assert!(hex_to_32(UPDATE_PUBKEY_HEX).is_some());
        use ed25519_dalek::VerifyingKey;
        assert!(VerifyingKey::from_bytes(&hex_to_32(UPDATE_PUBKEY_HEX).unwrap()).is_ok());
    }

    #[test]
    fn docker_boxes_get_a_command_not_an_install() {
        let m: Manifest = serde_json::from_str(
            r#"{"version":"0.1.3","docker":{"image":"jaimemelon/pureprivacy-box:0.1.3"}}"#,
        )
        .unwrap();
        let cmd = docker_command(&m);
        // The version is the whole payload: pl-box resolves image + tag from its own .env.
        assert!(cmd.contains("pl-box update 0.1.3"), "{cmd}");
        // No bare `docker pull`: it never reached a compose-run box and dies in build.sh.
        assert!(!cmd.contains("docker pull"), "{cmd}");
    }

    #[test]
    fn manifests_name_the_agent_image_and_the_command_names_only_the_version() {
        // docker.agent_image is the signed statement of which agent image belongs to the
        // release. The command does NOT spell out images: pl-box pulls the agent only when
        // the add-on is on, so a plain box never downloads it, and updating the box while the
        // agent stays behind (skew the owner never chose) can't happen — same version, both.
        let m: Manifest = serde_json::from_str(
            r#"{"version":"0.1.11","docker":{"image":"jaimemelon/pureprivacy-box:0.1.11","agent_image":"jaimemelon/pureprivacy-agent:0.1.11"}}"#,
        )
        .unwrap();
        assert_eq!(
            m.docker.as_ref().unwrap().agent_image.as_deref(),
            Some("jaimemelon/pureprivacy-agent:0.1.11")
        );
        assert_eq!(docker_command(&m), "cd docker && ./pl-box update 0.1.11");
    }

    #[test]
    fn manifests_without_an_agent_image_still_parse() {
        // Every manifest before 0.1.11 lacks agent_image — they must keep working, and the
        // command is the same shape (the version alone).
        let m: Manifest = serde_json::from_str(
            r#"{"version":"0.1.10","docker":{"image":"jaimemelon/pureprivacy-box:0.1.10"}}"#,
        )
        .unwrap();
        assert!(m.docker.as_ref().unwrap().agent_image.is_none());
        assert_eq!(docker_command(&m), "cd docker && ./pl-box update 0.1.10");
    }

    /// A NATIVE box whose platform has no build in the release must not be treated as Docker.
    /// Before this, `self_install == false` was read as "must be Docker", so a Windows box was
    /// told "your box runs in Docker" and handed a `docker pull` command.
    #[test]
    fn native_box_without_a_build_for_its_platform_is_not_docker() {
        // Manifest carrying ONLY a linux build (exactly what we publish today).
        let m: Manifest = serde_json::from_str(
            r#"{"version":"0.1.4","native":{"linux-x86_64":{"url":"u","sha256":"a","size":1}}}"#,
        )
        .unwrap();
        // A platform not in the manifest (stand-in for windows-x86_64 / macos-aarch64).
        assert!(!m.native.contains_key("windows-x86_64"));
        // ...so self-install is impossible there, yet the box is still Native, and the owner
        // must be pointed at a download — never at a docker command.
        assert!(RELEASES_PAGE.starts_with("https://"));
        // Our own platform IS covered, so a Linux box self-installs.
        assert!(m.native.contains_key("linux-x86_64"));
        assert_eq!(native_target(), format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH));
    }

    #[test]
    fn manifest_parses_native_targets() {
        let m: Manifest = serde_json::from_str(
            r#"{"version":"0.1.3","native":{"linux-x86_64":{"url":"https://x/b","sha256":"ab","size":10}}}"#,
        )
        .unwrap();
        assert_eq!(m.version, "0.1.3");
        assert!(m.native.contains_key("linux-x86_64"));
        assert_eq!(m.native["linux-x86_64"].size, 10);
    }
}
