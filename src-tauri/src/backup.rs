//! Encrypted identity backup (appliance-UX feature D).
//!
//! Backs up the part of a box that CANNOT be recreated — its **onion private key**, admin
//! credentials, and pairings (~1.5 KB total). The 346 MB homeserver DB (rooms + message
//! history) is deliberately NOT included: it's bulk, it's already replicated on the owner's
//! phones, and `pl-box backup` tars the whole volume for anyone who wants it.
//!
//! SECURITY: whoever holds an unencrypted backup can *impersonate the box* to all of the
//! owner's contacts. So the blob is only ever produced encrypted: AES-256-GCM under a key
//! derived from a user-chosen passphrase (PBKDF2-HMAC-SHA256, random per-backup salt). The
//! passphrase is never stored. Lose it and the backup is unrecoverable — by design.
//!
//! The payload carries the secrets in the CLEAR *inside* the encrypted envelope (rather than
//! copying the already-encrypted `secrets.json`), so a restore doesn't also need the original
//! box's `PL_SECRETS_KEY` — the restoring box re-encrypts them under its own key. That makes a
//! backup self-contained, which is the whole point when the original machine is gone.

use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use rand::RngCore;
use zeroize::Zeroize;
use tauri::AppHandle;

use crate::{config, crypto, pairing, state};

/// PBKDF2 rounds. High enough to make an offline guess of a weak passphrase expensive.
const KDF_ITERS: u32 = 200_000;
/// Envelope format version, so a future restore can tell what it's looking at.
const FORMAT_VERSION: u32 = 1;
pub const MAX_ENVELOPE_BYTES: usize = 1024 * 1024;

/// Derive the 32-byte AES key from the passphrase + per-backup salt.
fn derive_key(passphrase: &str, salt: &[u8]) -> [u8; 32] {
    let mut key = [0u8; 32];
    pbkdf2::pbkdf2_hmac::<sha2::Sha256>(passphrase.as_bytes(), salt, KDF_ITERS, &mut key);
    key
}

/// Build a passphrase-encrypted backup of this box's identity.
///
/// Returns the envelope as a JSON string (safe to store anywhere — it's encrypted). The
/// envelope keeps `onion` and `created` in the clear ONLY so a user with several backups can
/// tell which box a file belongs to; the onion address is public information anyway.
pub fn create(app: &AppHandle, passphrase: &str) -> Result<String, String> {
    if passphrase.chars().count() < 8 {
        return Err("Backup passphrase must be at least 8 characters.".into());
    }
    let paths = config::paths(app)?;
    let hs_dir = paths
        .hostname_file
        .parent()
        .ok_or_else(|| "couldn't locate the hidden-service directory".to_string())?
        .to_path_buf();
    let read = |p: std::path::PathBuf| -> Result<Vec<u8>, String> {
        std::fs::read(&p).map_err(|e| format!("couldn't read {}: {e}", p.display()))
    };

    let hostname = String::from_utf8_lossy(&read(hs_dir.join("hostname"))?)
        .trim()
        .to_string();
    let hs_secret = read(hs_dir.join("hs_ed25519_secret_key"))?;
    let hs_public = read(hs_dir.join("hs_ed25519_public_key"))?;

    let (box_name, username, created, onion, phrase, token, turn_secret, join_token, lk_key, lk_secret, admin_password) =
        state::read(app, |i| {
            (
                i.box_name.clone(),
                i.username.clone(),
                i.created.clone(),
                i.onion.clone().unwrap_or_default(),
                i.phrase.clone(),
                i.token.clone(),
                i.turn_secret.clone(),
                i.join_token.clone(),
                i.livekit_api_key.clone(),
                i.livekit_api_secret.clone(),
                i.admin_password.clone(),
            )
        });
    let pairings = pairing::onions(&paths.data_root);

    let payload = serde_json::json!({
        "hostname": hostname,
        "hs_secret": B64.encode(&hs_secret),
        "hs_public": B64.encode(&hs_public),
        "box_name": box_name,
        "username": username,
        "created": created,
        "onion": onion,
        "phrase": phrase,
        "token": token,
        "turn_secret": turn_secret,
        "join_token": join_token,
        "livekit_api_key": lk_key,
        "livekit_api_secret": lk_secret,
        "admin_password": admin_password,
        "pairings": pairings,
    })
    .to_string();

    let mut salt = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut salt);
    let mut key = derive_key(passphrase, &salt);
    // crypto::encrypt is AES-256-GCM and prepends a fresh random nonce (same primitive that
    // protects secrets.json at rest).
    let sealed = crypto::encrypt(&payload, &key);
    key.zeroize();
    let sealed = sealed?;

    Ok(serde_json::json!({
        "v": FORMAT_VERSION,
        "kdf": "pbkdf2-hmac-sha256",
        "iters": KDF_ITERS,
        "salt": B64.encode(salt),
        "onion": onion,          // public; lets a user identify which box a file is for
        "created": created,
        "blob": sealed,
    })
    .to_string())
}

/// Decrypt a backup envelope produced by [`create`]. Returns the inner payload as JSON.
/// A wrong passphrase fails closed here (GCM authentication), never half-applied.
#[allow(dead_code)] // used by the restore path (setup page)
pub fn open(envelope_json: &str, passphrase: &str) -> Result<serde_json::Value, String> {
    if envelope_json.len() > MAX_ENVELOPE_BYTES {
        return Err("Identity backup exceeds the 1 MiB size limit.".into());
    }
    let env: serde_json::Value =
        serde_json::from_str(envelope_json).map_err(|_| "That doesn't look like a Privacy Lodge backup file.".to_string())?;
    let v = env.get("v").and_then(|x| x.as_u64()).unwrap_or(0);
    if v != FORMAT_VERSION as u64 {
        return Err(format!("Unsupported backup format (v{v})."));
    }
    let salt_b64 = env.get("salt").and_then(|x| x.as_str()).ok_or("backup is missing its salt")?;
    // v1 has one fixed KDF contract. Never execute attacker-chosen work or
    // truncate a u64 iteration count into a different u32 value.
    if env.get("kdf").and_then(|x| x.as_str()) != Some("pbkdf2-hmac-sha256")
        || env.get("iters").and_then(|x| x.as_u64()) != Some(KDF_ITERS as u64) {
        return Err("Unsupported backup key derivation parameters.".into());
    }
    let sealed = env.get("blob").and_then(|x| x.as_str()).ok_or("backup is missing its payload")?;
    let salt = B64.decode(salt_b64).map_err(|_| "backup salt is corrupt".to_string())?;

    if salt.len() != 16 {
        return Err("Backup salt must be exactly 16 bytes.".into());
    }
    let mut key = derive_key(passphrase, &salt);
    let plain = crypto::decrypt(sealed, &key);
    key.zeroize();
    let mut plain = plain.map_err(|_| "Wrong passphrase, or this backup file is damaged.".to_string())?;
    let result = serde_json::from_str(&plain).map_err(|_| "Backup contents are corrupt.".to_string());
    plain.zeroize();
    result
}

/// Restore a backup onto a **fresh** box: writes the onion key back, re-instates the admin
/// credentials + pairings, and leaves the box ready for `start_lifecycle` to boot it on the
/// SAME .onion. The homeserver DB is not part of a backup, so rooms/history start empty and
/// contacts are re-paired — the address and login are what can't be recreated.
///
/// Refuses to run on a box that already has an identity: restore is a takeover primitive and
/// must never silently overwrite a live box.
pub fn restore(app: &AppHandle, envelope_json: &str, passphrase: &str) -> Result<(), String> {
    let _identity = crate::identity_transaction::lock();
    if state::read(app, |i| i.phase != state::Phase::Fresh || i.onion.is_some()) {
        return Err("This box already has an identity or setup in progress. Restore onto a fresh box.".into());
    }
    let payload = open(envelope_json, passphrase)?;
    let identity = crate::identity_restore::validate(&payload)?;
    // Validate and encrypt everything before publishing any identity file. The
    // journal rolls back interrupted publication before startup can read state.
    let dir = state::app_data_dir(app)?;
    let (boxed, secrets) = state::identity_snapshot(&identity.inner).documents()?;
    let added_at = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs()).unwrap_or(0);
    let peers = pairing::Pairings { peers: identity.peers.into_iter()
        .map(|onion| pairing::Pairing { onion, added_at }).collect() };
    let pairings = serde_json::to_vec(&peers).map_err(|e| e.to_string())?;
    let hostname = format!("{}\n", identity.inner.onion.as_deref().unwrap());
    let result = crate::identity_transaction::commit(&dir, &[
        ("secrets.json", secrets.as_bytes()), ("pairings.json", &pairings),
        ("data/tor/hs/hostname", hostname.as_bytes()),
        ("data/tor/hs/hs_ed25519_secret_key", &identity.secret),
        ("data/tor/hs/hs_ed25519_public_key", &identity.public),
        ("box.json", boxed.as_bytes()),
    ]);
    if let Err(error) = result {
        // A failed fsync/recovery may leave durable data pending. Do not offer a
        // fresh setup against it; restart will resolve the journal first.
        if dir.join(".identity-transaction.json").exists() || dir.join("box.json").exists() {
            state::update(app, |i| { i.phase = state::Phase::Error; i.error = Some(error.clone()); });
        }
        return Err(error);
    }
    state::update(app, |i| *i = identity.inner);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build an envelope exactly the way `create` does, without needing a live AppHandle.
    fn seal(payload: &serde_json::Value, passphrase: &str) -> String {
        let mut salt = [0u8; 16];
        rand::thread_rng().fill_bytes(&mut salt);
        let mut key = derive_key(passphrase, &salt);
        let sealed = crypto::encrypt(&payload.to_string(), &key).unwrap();
        key.zeroize();
        serde_json::json!({
            "v": FORMAT_VERSION,
            "kdf": "pbkdf2-hmac-sha256",
            "iters": KDF_ITERS,
            "salt": B64.encode(salt),
            "onion": "example.onion",
            "created": "2026-07-23",
            "blob": sealed,
        })
        .to_string()
    }

    #[test]
    fn round_trips_with_the_right_passphrase() {
        let payload = serde_json::json!({ "hs_secret": "c3VwZXItc2VjcmV0", "username": "alex" });
        let env = seal(&payload, "correct horse battery");
        let out = open(&env, "correct horse battery").expect("should decrypt");
        assert_eq!(out["hs_secret"], "c3VwZXItc2VjcmV0");
        assert_eq!(out["username"], "alex");
    }

    #[test]
    fn authenticated_identity_round_trip_preserves_keys_and_rejects_inconsistent_payloads() {
        let payload = crate::identity_restore::tests::payload();
        let envelope = seal(&payload, "identity round trip");
        let restored = crate::identity_restore::validate(&open(&envelope, "identity round trip").unwrap()).unwrap();
        assert_eq!(restored.inner.onion.as_deref(), payload["hostname"].as_str());
        assert_eq!(B64.encode(restored.secret.as_slice()), payload["hs_secret"]);
        let mut inconsistent = payload;
        inconsistent["hostname"] = format!("{}.onion", "a".repeat(56)).into();
        let envelope = seal(&inconsistent, "identity round trip");
        let decrypted = open(&envelope, "identity round trip").unwrap();
        assert!(crate::identity_restore::validate(&decrypted).is_err());
    }

    #[test]
    fn wrong_passphrase_fails_closed() {
        let payload = serde_json::json!({ "hs_secret": "c3VwZXItc2VjcmV0" });
        let env = seal(&payload, "correct horse battery");
        // GCM authentication must reject it — never a partial/garbage decrypt.
        assert!(open(&env, "wrong passphrase").is_err());
    }

    #[test]
    fn secret_material_is_not_left_in_the_clear() {
        let payload = serde_json::json!({ "hs_secret": "TOPSECRETKEYMATERIAL" });
        let env = seal(&payload, "a good passphrase");
        // The envelope may expose the (public) onion, but never the key material.
        assert!(!env.contains("TOPSECRETKEYMATERIAL"));
        assert!(env.contains("example.onion"));
    }

    #[test]
    fn rejects_unbounded_kdf_and_oversized_envelopes_before_deriving() {
        let base = serde_json::json!({"v":1,"kdf":"pbkdf2-hmac-sha256","iters":KDF_ITERS,"salt":B64.encode([0u8;16]),"blob":"invalid"});
        for iterations in [0u64, 1, u32::MAX as u64 + 1, u64::MAX] {
            let mut envelope = base.clone(); envelope["iters"] = iterations.into();
            assert!(open(&envelope.to_string(), "passphrase").unwrap_err().contains("parameters"));
        }
        let mut envelope = base; envelope["salt"] = B64.encode([0u8;17]).into();
        assert!(open(&envelope.to_string(), "passphrase").unwrap_err().contains("16 bytes"));
        assert!(open(&"x".repeat(MAX_ENVELOPE_BYTES+1), "passphrase").unwrap_err().contains("size limit"));
    }

    #[test]
    fn rejects_a_non_backup_file() {
        assert!(open("{\"hello\":true}", "x").is_err());
        assert!(open("not json at all", "x").is_err());
    }
}
