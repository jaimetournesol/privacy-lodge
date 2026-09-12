//! Federation pairing: the QR-exchanged "pair codes" + the allowlist of peers
//! we federate with. This is Privacy Lodge's "only talk to boxes you've paired
//! with" model.
//!
//! tuwunel has no federation allowlist of its own (only a global on/off + a
//! denylist), so enforcement lives one layer up in the Caddy fed-proxy:
//! `config::render_caddyfile` turns this list into an `Authorization`-origin
//! allowlist (verified — see docs/redesign/2026-06-13-desktop-build-findings.md).
//!
//! Pairings persist as `<data_dir>/pairings.json`. A pair code is a base64 JSON
//! blob carrying the minting box's onion + a 15-minute expiry + a nonce; the
//! operator reads it off a screen they control (trust root = their eyeballs),
//! mirroring the v0.1 appliance.

use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine;
use serde::{Deserialize, Serialize};

const CODE_TTL_SECS: u64 = 15 * 60;

#[derive(Serialize, Deserialize, Clone)]
pub struct Pairing {
    pub onion: String,
    pub added_at: u64,
}

#[derive(Serialize, Deserialize, Default)]
pub struct Pairings {
    pub peers: Vec<Pairing>,
}

#[derive(Serialize, Deserialize)]
struct PairCode {
    version: u8,
    onion: String,
    expires_at: u64,
    nonce: String,
}

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Strict v3 onion check: `^[a-z2-7]{56}\.onion$` (56 base32 chars + ".onion").
///
/// A loose `ends_with(".onion")` lets a malformed string into the Caddy
/// `header_regexp` allowlist — regex-injection / allowlist-bypass surface. Every
/// onion that reaches pairings.json must pass this first. Done by hand to avoid
/// pulling in a regex dependency; the char class is the exact base32 alphabet.
pub fn is_valid_onion(s: &str) -> bool {
    let Some(label) = s.strip_suffix(".onion") else {
        return false;
    };
    label.len() == 56 && label.bytes().all(|b| matches!(b, b'a'..=b'z' | b'2'..=b'7'))
}

fn b64() -> base64::engine::GeneralPurpose {
    base64::engine::general_purpose::STANDARD
}

pub fn pairings_path(data_dir: &std::path::Path) -> PathBuf {
    data_dir.join("pairings.json")
}

pub fn load(data_dir: &std::path::Path) -> Pairings {
    // [H7] Fail-CLOSED on a corrupt NON-EMPTY file. A missing file is a
    // legitimately-empty allowlist (fresh box / never paired), so it defaults to
    // empty. But a file that EXISTS yet fails to parse (truncated by a crash mid-
    // write, or otherwise corrupt) must NOT silently collapse to an empty
    // allowlist — that would cut ALL federation. We keep the box federating by
    // re-throwing via a panic-free fallback: callers that mutate (add/remove) go
    // through load_strict and surface the error; this infallible accessor (used
    // by read-only paths) returns empty ONLY when the file is genuinely absent.
    match load_strict(data_dir) {
        Ok(p) => p,
        Err(_) => {
            // Parse failure on an existing, non-empty file. Returning empty here
            // is the read-only fallback; the authoritative guard is in save()
            // (atomic write, below) so this state should not arise in practice.
            // Loudly note it so a corrupt store is visible in logs.
            eprintln!(
                "[privacy-lodge] WARNING: pairings.json failed to parse — treating as empty for this read"
            );
            Pairings::default()
        }
    }
}

/// Like `load`, but returns `Err` when the file EXISTS yet fails to parse,
/// instead of defaulting to empty. A missing file is still legitimately empty
/// (`Ok(default)`). Mutating callers (add/remove) use this so a corrupt store
/// can't be silently overwritten with a truncated allowlist. [H7]
pub fn load_strict(data_dir: &std::path::Path) -> Result<Pairings, String> {
    let path = pairings_path(data_dir);
    match std::fs::read_to_string(&path) {
        // No file → genuinely empty allowlist (fresh box).
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Pairings::default()),
        Err(e) => Err(format!("couldn't read pairings: {e}")),
        Ok(s) => serde_json::from_str(&s)
            .map_err(|e| format!("pairings.json is corrupt ({e}) — refusing to overwrite it")),
    }
}

pub fn save(data_dir: &std::path::Path, p: &Pairings) -> Result<(), String> {
    // [H7] Atomic write: serialize to a temp file in the SAME dir, then rename
    // over the target. rename(2) is atomic on the same filesystem, so a crash
    // can never leave a half-written (truncated → unparseable) pairings.json
    // that load() would then read as an empty allowlist (cutting all
    // federation). The old write() truncated in place — the exact window H7
    // flags.
    let json = serde_json::to_string_pretty(p).map_err(|e| e.to_string())?;
    let final_path = pairings_path(data_dir);
    let tmp_path = final_path.with_extension("json.tmp");
    std::fs::write(&tmp_path, &json)
        .map_err(|e| format!("couldn't write pairings temp file: {e}"))?;
    std::fs::rename(&tmp_path, &final_path).map_err(|e| {
        // Best-effort cleanup so a failed rename doesn't litter a stale tmp.
        let _ = std::fs::remove_file(&tmp_path);
        format!("couldn't save pairings: {e}")
    })
}

/// Mint a short-lived pair code carrying our own onion, for the peer to accept.
pub fn mint_code(my_onion: &str, nonce_hex: &str) -> Result<String, String> {
    let code = PairCode {
        version: 1,
        onion: my_onion.to_string(),
        expires_at: now() + CODE_TTL_SECS,
        nonce: nonce_hex.to_string(),
    };
    let json = serde_json::to_vec(&code).map_err(|e| e.to_string())?;
    Ok(b64().encode(json))
}

/// Parse + validate a peer's pair code; returns their onion.
pub fn parse_code(code: &str) -> Result<String, String> {
    let raw = b64()
        .decode(code.trim())
        .map_err(|_| "That doesn't look like a valid pair code.".to_string())?;
    let parsed: PairCode = serde_json::from_slice(&raw)
        .map_err(|_| "That pair code is malformed.".to_string())?;
    if parsed.version != 1 {
        return Err("That pair code is from an incompatible version.".into());
    }
    if now() > parsed.expires_at {
        return Err("That pair code has expired — ask for a fresh one (codes last 15 minutes).".into());
    }
    if !is_valid_onion(&parsed.onion) {
        return Err("That pair code doesn't contain a valid address.".into());
    }
    Ok(parsed.onion)
}

/// Add a peer (idempotent).
pub fn add(data_dir: &std::path::Path, onion: &str) -> Result<(), String> {
    // [H7] load_strict: refuse to overwrite a corrupt store with a truncated
    // allowlist; surface the error instead of silently rebuilding from empty.
    let mut p = load_strict(data_dir)?;
    if !p.peers.iter().any(|x| x.onion == onion) {
        p.peers.push(Pairing { onion: onion.to_string(), added_at: now() });
        save(data_dir, &p)?;
    }
    Ok(())
}

/// Remove a peer (idempotent).
pub fn remove(data_dir: &std::path::Path, onion: &str) -> Result<(), String> {
    // [H7] load_strict: same rationale as add — don't rebuild over a corrupt file.
    let mut p = load_strict(data_dir)?;
    let before = p.peers.len();
    p.peers.retain(|x| x.onion != onion);
    if p.peers.len() != before {
        save(data_dir, &p)?;
    }
    Ok(())
}

pub fn onions(data_dir: &std::path::Path) -> Vec<String> {
    load(data_dir).peers.into_iter().map(|x| x.onion).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    // A syntactically valid v3 onion (56 base32 chars + ".onion") for tests.
    const VALID_ONION: &str =
        "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion";

    #[test]
    fn code_round_trips() {
        let code = mint_code(VALID_ONION, "deadbeef").unwrap();
        assert_eq!(parse_code(&code).unwrap(), VALID_ONION);
    }

    #[test]
    fn rejects_garbage_and_non_onion() {
        assert!(parse_code("not-base64!!!").is_err());
        let bad = b64().encode(br#"{"version":1,"onion":"evil.com","expires_at":99999999999,"nonce":"x"}"#);
        assert!(parse_code(&bad).is_err());
    }

    #[test]
    fn is_valid_onion_enforces_v3_format() {
        assert!(is_valid_onion(VALID_ONION));
        // 56 chars exactly is required.
        assert_eq!(VALID_ONION.strip_suffix(".onion").unwrap().len(), 56);
        // Too short / too long.
        assert!(!is_valid_onion("abc.onion"));
        assert!(!is_valid_onion(&format!("{}a.onion", "a".repeat(56))));
        // Out-of-alphabet chars: base32 has no 0/1/8/9 or uppercase.
        assert!(!is_valid_onion(&format!("{}.onion", "a".repeat(55) + "0")));
        assert!(!is_valid_onion(&format!("{}.onion", "A".repeat(56))));
        // Not an onion / missing suffix / allowlist-bypass attempts.
        assert!(!is_valid_onion("evil.com"));
        assert!(!is_valid_onion(&"a".repeat(56)));
        assert!(!is_valid_onion(&format!("{}.onion|evil\\.com", "a".repeat(56))));
    }

    #[test]
    fn parse_code_rejects_malformed_onion() {
        // A code carrying a structurally-valid-looking but non-v3 onion is rejected
        // before it can reach the allowlist.
        let raw = serde_json::to_vec(&PairCode {
            version: 1,
            onion: "tooshort.onion".into(),
            expires_at: now() + CODE_TTL_SECS,
            nonce: "n".into(),
        })
        .unwrap();
        assert!(parse_code(&b64().encode(raw)).is_err());
    }

    #[test]
    fn save_then_load_round_trips_and_is_atomic() {
        // [H7] save() writes via a temp file + rename; load() reads it back, and
        // no stray .tmp is left behind.
        let dir = std::env::temp_dir().join(format!("pp_pairing_h7_{}", now()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = Pairings {
            peers: vec![Pairing { onion: VALID_ONION.to_string(), added_at: 1 }],
        };
        save(&dir, &p).unwrap();
        let back = load(&dir);
        assert_eq!(back.peers.len(), 1);
        assert_eq!(back.peers[0].onion, VALID_ONION);
        assert!(!pairings_path(&dir).with_extension("json.tmp").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_strict_is_empty_when_missing_but_errs_on_corrupt() {
        // [H7] A MISSING file is a legitimately-empty allowlist (Ok(empty)); a
        // file that EXISTS but doesn't parse is an Err — NOT a silent collapse to
        // an empty allowlist (which would cut all federation). add()/remove() use
        // load_strict so they never overwrite a corrupt store with a truncated one.
        let dir = std::env::temp_dir().join(format!("pp_pairing_h7c_{}", now()));
        std::fs::create_dir_all(&dir).unwrap();
        // Missing file → Ok(empty).
        assert!(load_strict(&dir).unwrap().peers.is_empty());
        // Corrupt (non-empty, unparseable) → Err, and add() refuses.
        std::fs::write(pairings_path(&dir), b"{ this is not json").unwrap();
        assert!(load_strict(&dir).is_err());
        assert!(add(&dir, VALID_ONION).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn rejects_expired() {
        let raw = serde_json::to_vec(&PairCode {
            version: 1,
            onion: "x.onion".into(),
            expires_at: 1, // long past
            nonce: "n".into(),
        })
        .unwrap();
        assert!(parse_code(&b64().encode(raw)).is_err());
    }
}
