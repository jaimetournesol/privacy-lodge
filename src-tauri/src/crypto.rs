//! At-rest encryption for `secrets.json` (review finding H2).
//!
//! The box keeps its admin password (and the service secrets) so the owner's
//! phone can password-login and so `run_pairing_sync` / the federation keepalive
//! can re-authenticate every few seconds. The password must therefore survive a
//! restart in REVERSIBLE form — we can't hash it. Instead we AES-256-GCM the
//! whole secrets envelope with a 32-byte master key held OUTSIDE the data dir, so
//! a stolen/snapshotted data dir no longer yields the cleartext password, TURN
//! secret, registration token or LiveKit keys.
//!
//! Key sources, in priority order (the chosen one is recorded in the file so we
//! decrypt with the same one):
//!   1. `PRIVACY_LODGE_SECRETS_KEY` env var (base64 of 32 bytes) — for headless /
//!      CI / container boxes with no desktop keychain session.
//!   2. OS keychain via `keyring` (macOS Keychain, Windows Credential Manager,
//!      Linux secret-service) — the default for a desktop install.
//!   3. A constant-derived fallback key — obfuscation only (anyone with the
//!      binary can derive it), used solely so a box with neither of the above
//!      still boots. Emits a loud warning; real protection needs (1) or (2).

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Key, Nonce};
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use rand::RngCore;
use sha2::{Digest, Sha256};
use zeroize::Zeroize;

const KEYRING_SERVICE: &str = "ai.tournesol.privacylodge";
/// Pre-rename keychain service. A native box that stored its master key under the old name
/// must keep finding it: reads fall back to this, and the key is re-stored under the new name.
const KEYRING_SERVICE_LEGACY: &str = "ai.tournesol.pureprivacy";
const KEYRING_ACCOUNT: &str = "secrets-master-key";
const ENV_KEY: &str = "PRIVACY_LODGE_SECRETS_KEY";
/// FROZEN across the rename ON PURPOSE. This is not a name — it is the SHA-256 *input* that
/// derives the constant fallback key. Changing a single byte changes the key, and every box
/// whose secrets.json was sealed with the fallback (no env key, no keychain) would become
/// undecryptable. It may only ever change together with a key-migration step.
const FALLBACK_MATERIAL: &[u8] = b"ai.tournesol.pureprivacy/secrets-fallback-v2";
const NONCE_LEN: usize = 12;
const TAG_LEN: usize = 16;

/// Which master-key source encrypted a given `secrets.json`. Recorded in the
/// file so we resolve the *same* source when decrypting.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum KeySource {
    Env,
    Keychain,
    Fallback,
}

impl KeySource {
    pub fn as_str(self) -> &'static str {
        match self {
            KeySource::Env => "env",
            KeySource::Keychain => "keychain",
            KeySource::Fallback => "fallback",
        }
    }
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "env" => Some(KeySource::Env),
            "keychain" => Some(KeySource::Keychain),
            "fallback" => Some(KeySource::Fallback),
            _ => None,
        }
    }
}

fn decode_key(s: &str) -> Option<[u8; 32]> {
    let b = B64.decode(s.trim()).ok()?;
    if b.len() != 32 {
        return None;
    }
    let mut k = [0u8; 32];
    k.copy_from_slice(&b);
    Some(k)
}

fn env_key() -> Option<[u8; 32]> {
    // PRIVACY_LODGE_SECRETS_KEY, falling back to the pre-rename PRIVACY_LODGE_SECRETS_KEY.
    decode_key(&crate::envcompat::var("SECRETS_KEY").ok()?)
}

fn keychain_entry() -> Option<keyring::Entry> {
    keyring::Entry::new(KEYRING_SERVICE, KEYRING_ACCOUNT).ok()
}

fn legacy_keychain_entry() -> Option<keyring::Entry> {
    keyring::Entry::new(KEYRING_SERVICE_LEGACY, KEYRING_ACCOUNT).ok()
}

/// Read the keychain-held master key, if one exists — under the new service name first, then
/// the pre-rename one (a box that ran before 0.2.0 stored it there).
fn keychain_get() -> Option<[u8; 32]> {
    if let Some(k) = decode_key(&keychain_entry()?.get_password().ok()?) {
        return Some(k);
    }
    decode_key(&legacy_keychain_entry()?.get_password().ok()?)
}

/// Read the keychain master key, creating + storing a fresh random one the first
/// time. Returns `None` if the platform keychain is unavailable.
fn keychain_get_or_create() -> Option<[u8; 32]> {
    let entry = keychain_entry()?;
    match entry.get_password() {
        Ok(s) => decode_key(&s),
        Err(keyring::Error::NoEntry) => {
            // Rename (0.2.0): a key stored under the old service name is adopted — copied to
            // the new name (the old entry is left in place) so the box keeps its secrets.
            if let Some(s) = legacy_keychain_entry().and_then(|e| e.get_password().ok()) {
                if let Some(k) = decode_key(&s) {
                    let _ = entry.set_password(&s);
                    return Some(k);
                }
            }
            let mut k = [0u8; 32];
            rand::thread_rng().fill_bytes(&mut k);
            let stored = entry.set_password(&B64.encode(k));
            if stored.is_err() {
                k.zeroize();
                return None;
            }
            Some(k)
        }
        Err(_) => None,
    }
}

/// Constant fallback key: SHA-256 of a build constant. Deterministic and
/// onion-independent (the first `persist()` runs before the onion is minted), so
/// a box always boots — but it offers only obfuscation, never real at-rest
/// protection. Always paired with a warning when used.
fn fallback_key() -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(FALLBACK_MATERIAL);
    let d = h.finalize();
    let mut k = [0u8; 32];
    k.copy_from_slice(&d);
    k
}

/// Resolve the master key to ENCRYPT with (get-or-create), reporting which
/// source was used so the file records it for later decryption.
pub fn key_for_encrypt() -> ([u8; 32], KeySource) {
    if let Some(k) = env_key() {
        return (k, KeySource::Env);
    }
    if let Some(k) = keychain_get_or_create() {
        return (k, KeySource::Keychain);
    }
    eprintln!(
        "[privacy-lodge][crypto] WARNING: no {ENV_KEY} and no OS keychain — secrets.json is \
         encrypted with a constant fallback key (obfuscation only, NOT secure at \
         rest). Set {ENV_KEY} (base64 of 32 bytes) or run with a desktop keychain."
    );
    (fallback_key(), KeySource::Fallback)
}

/// Resolve the master key to DECRYPT with, given the source recorded in the file.
pub fn key_for_decrypt(source: KeySource) -> Result<[u8; 32], String> {
    match source {
        KeySource::Env => env_key()
            .ok_or_else(|| format!("{ENV_KEY} is missing or not base64 of 32 bytes — cannot decrypt secrets.json")),
        KeySource::Keychain => keychain_get()
            .ok_or_else(|| "OS keychain entry for the secrets key is missing — cannot decrypt secrets.json".to_string()),
        KeySource::Fallback => Ok(fallback_key()),
    }
}

/// AES-256-GCM seal raw bytes; returns nonce ‖ ciphertext‖tag. The byte-level primitive
/// under [`encrypt`]/[`decrypt`] — public so `pl-crypt` (the CLI that encrypts `pl-box`
/// backup bundles) is running THIS code, not a re-implementation that could drift.
pub fn seal_bytes(plaintext: &[u8], key: &[u8; 32]) -> Result<Vec<u8>, String> {
    let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(key));
    let mut nonce_bytes = [0u8; NONCE_LEN];
    rand::thread_rng().fill_bytes(&mut nonce_bytes);
    let nonce = Nonce::from_slice(&nonce_bytes);
    let ct = cipher
        .encrypt(nonce, plaintext)
        .map_err(|e| format!("encrypt failed: {e}"))?;
    let mut out = nonce_bytes.to_vec();
    out.extend_from_slice(&ct);
    Ok(out)
}

/// Open a nonce ‖ ciphertext‖tag blob produced by [`seal_bytes`]. Fails closed (GCM
/// authentication) on a wrong key or tampered data — never returns garbage bytes.
pub fn open_bytes(data: &[u8], key: &[u8; 32]) -> Result<Vec<u8>, String> {
    if data.len() < NONCE_LEN + TAG_LEN {
        return Err("ciphertext too short".into());
    }
    let (nonce_bytes, ct) = data.split_at(NONCE_LEN);
    let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(key));
    cipher
        .decrypt(Nonce::from_slice(nonce_bytes), ct)
        .map_err(|_| "decryption failed — wrong key or tampered data".to_string())
}

/// AES-256-GCM encrypt `plaintext`; returns base64(nonce ‖ ciphertext‖tag).
pub fn encrypt(plaintext: &str, key: &[u8; 32]) -> Result<String, String> {
    let mut out = seal_bytes(plaintext.as_bytes(), key)?;
    let b64 = B64.encode(&out);
    out.zeroize();
    Ok(b64)
}

/// Decrypt a base64(nonce ‖ ciphertext‖tag) blob produced by [`encrypt`].
pub fn decrypt(blob_b64: &str, key: &[u8; 32]) -> Result<String, String> {
    let data = B64.decode(blob_b64.trim()).map_err(|e| format!("bad base64: {e}"))?;
    let pt = open_bytes(&data, key)
        .map_err(|_| "decryption failed — wrong key or tampered secrets.json".to_string())?;
    String::from_utf8(pt).map_err(|e| format!("decrypted bytes not UTF-8: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let key = [7u8; 32];
        let ct = encrypt("hunter2-box-2026", &key).unwrap();
        assert_ne!(ct, "hunter2-box-2026");
        assert!(!ct.contains("hunter2"));
        assert_eq!(decrypt(&ct, &key).unwrap(), "hunter2-box-2026");
    }

    #[test]
    fn distinct_nonces_distinct_ciphertexts() {
        let key = [3u8; 32];
        assert_ne!(encrypt("same", &key).unwrap(), encrypt("same", &key).unwrap());
    }

    #[test]
    fn wrong_key_fails() {
        let ct = encrypt("secret", &[1u8; 32]).unwrap();
        assert!(decrypt(&ct, &[2u8; 32]).is_err());
    }

    #[test]
    fn tamper_fails() {
        let key = [9u8; 32];
        let ct = encrypt("secret", &key).unwrap();
        let mut raw = B64.decode(&ct).unwrap();
        let last = raw.len() - 1;
        raw[last] ^= 0x01; // flip a tag bit
        assert!(decrypt(&B64.encode(raw), &key).is_err());
    }

    #[test]
    fn fallback_key_is_stable() {
        assert_eq!(fallback_key(), fallback_key());
    }
}
