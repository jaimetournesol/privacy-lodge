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
use tauri::AppHandle;

use crate::{config, crypto, pairing, state};

/// PBKDF2 rounds. High enough to make an offline guess of a weak passphrase expensive.
const KDF_ITERS: u32 = 200_000;
/// Envelope format version, so a future restore can tell what it's looking at.
const FORMAT_VERSION: u32 = 1;

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
    let key = derive_key(passphrase, &salt);
    // crypto::encrypt is AES-256-GCM and prepends a fresh random nonce (same primitive that
    // protects secrets.json at rest).
    let sealed = crypto::encrypt(&payload, &key)?;

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
    let env: serde_json::Value =
        serde_json::from_str(envelope_json).map_err(|_| "That doesn't look like a Privacy Lodge backup file.".to_string())?;
    let v = env.get("v").and_then(|x| x.as_u64()).unwrap_or(0);
    if v != FORMAT_VERSION as u64 {
        return Err(format!("Unsupported backup format (v{v})."));
    }
    let salt_b64 = env.get("salt").and_then(|x| x.as_str()).ok_or("backup is missing its salt")?;
    let iters = env.get("iters").and_then(|x| x.as_u64()).unwrap_or(KDF_ITERS as u64) as u32;
    let sealed = env.get("blob").and_then(|x| x.as_str()).ok_or("backup is missing its payload")?;
    let salt = B64.decode(salt_b64).map_err(|_| "backup salt is corrupt".to_string())?;

    let mut key = [0u8; 32];
    pbkdf2::pbkdf2_hmac::<sha2::Sha256>(passphrase.as_bytes(), &salt, iters, &mut key);
    let plain = crypto::decrypt(sealed, &key)
        .map_err(|_| "Wrong passphrase, or this backup file is damaged.".to_string())?;
    serde_json::from_str(&plain).map_err(|_| "Backup contents are corrupt.".to_string())
}

/// Restore a backup onto a **fresh** box: writes the onion key back, re-instates the admin
/// credentials + pairings, and leaves the box ready for `start_lifecycle` to boot it on the
/// SAME .onion. The homeserver DB is not part of a backup, so rooms/history start empty and
/// contacts are re-paired — the address and login are what can't be recreated.
///
/// Refuses to run on a box that already has an identity: restore is a takeover primitive and
/// must never silently overwrite a live box.
pub fn restore(app: &AppHandle, envelope_json: &str, passphrase: &str) -> Result<(), String> {
    if state::read(app, |i| i.onion.is_some()) {
        return Err("This box already has an identity — restore onto a fresh box instead.".into());
    }
    let p = open(envelope_json, passphrase)?;
    let s = |k: &str| p.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();

    let hostname = s("hostname");
    let hs_secret = B64
        .decode(s("hs_secret"))
        .map_err(|_| "This backup's key material is corrupt.".to_string())?;
    let hs_public = B64
        .decode(s("hs_public"))
        .map_err(|_| "This backup's key material is corrupt.".to_string())?;
    if hostname.is_empty() || hs_secret.is_empty() {
        return Err("This backup is missing its onion key.".into());
    }

    let paths = config::ensure_dirs(app)?;
    std::fs::create_dir_all(&paths.hs_dir)
        .map_err(|e| format!("couldn't create the hidden-service dir: {e}"))?;
    let write = |name: &str, bytes: &[u8]| -> Result<(), String> {
        let path = paths.hs_dir.join(name);
        std::fs::write(&path, bytes).map_err(|e| format!("couldn't write {name}: {e}"))?;
        // tor REFUSES to use a hidden-service dir with loose permissions.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        }
        Ok(())
    };
    write("hostname", format!("{hostname}\n").as_bytes())?;
    write("hs_ed25519_secret_key", &hs_secret)?;
    write("hs_ed25519_public_key", &hs_public)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&paths.hs_dir, std::fs::Permissions::from_mode(0o700));
    }

    let phrase: Vec<String> = p
        .get("phrase")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();

    // Secrets go back into state and are re-encrypted under THIS box's key by persist(), so the
    // original machine's PL_SECRETS_KEY is never needed.
    state::update(app, |i| {
        i.box_name = s("box_name");
        i.username = s("username");
        i.created = s("created");
        i.onion = Some(hostname.clone());
        i.phrase = phrase;
        i.token = s("token");
        i.turn_secret = s("turn_secret");
        i.join_token = s("join_token");
        i.livekit_api_key = s("livekit_api_key");
        i.livekit_api_secret = s("livekit_api_secret");
        i.admin_password = s("admin_password");
    });
    state::persist(app)?;

    // Re-instate the federation allowlist so previously paired peers can reach us again.
    for o in p.get("pairings").and_then(|v| v.as_array()).cloned().unwrap_or_default() {
        if let Some(o) = o.as_str() {
            if pairing::is_valid_onion(o) && o != hostname {
                let _ = pairing::add(&paths.data_root, o);
            }
        }
    }
    eprintln!("[privacy-lodge] restored identity for {hostname}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build an envelope exactly the way `create` does, without needing a live AppHandle.
    fn seal(payload: &serde_json::Value, passphrase: &str) -> String {
        let mut salt = [0u8; 16];
        rand::thread_rng().fill_bytes(&mut salt);
        let key = derive_key(passphrase, &salt);
        let sealed = crypto::encrypt(&payload.to_string(), &key).unwrap();
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
    fn rejects_a_non_backup_file() {
        assert!(open("{\"hello\":true}", "x").is_err());
        assert!(open("not json at all", "x").is_err());
    }
}
