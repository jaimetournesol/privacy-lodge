//! Validate the authenticated payload before touching a fresh box's files or state.
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use ed25519_dalek::{hazmat::ExpandedSecretKey, VerifyingKey};
use sha3::{Digest, Sha3_256};
use zeroize::Zeroizing;
use crate::{pairing, state};

const SECRET_HEADER: &[u8; 32] = b"== ed25519v1-secret: type0 ==\0\0\0";
const PUBLIC_HEADER: &[u8; 32] = b"== ed25519v1-public: type0 ==\0\0\0";

pub(crate) struct Identity {
    pub inner: state::Inner,
    pub secret: Zeroizing<Vec<u8>>,
    pub public: Vec<u8>,
    pub peers: Vec<String>,
}

fn onion(public: &[u8]) -> String {
    let checksum = Sha3_256::new().chain_update(b".onion checksum").chain_update(public)
        .chain_update([3]).finalize();
    let mut bytes = public.to_vec(); bytes.extend_from_slice(&checksum[..2]); bytes.push(3);
    let alphabet = b"abcdefghijklmnopqrstuvwxyz234567";
    let (mut accumulator, mut bits) = (0u32, 0u32);
    let mut encoded = String::with_capacity(62);
    for byte in bytes {
        accumulator = (accumulator << 8) | byte as u32; bits += 8;
        while bits >= 5 { bits -= 5; encoded.push(alphabet[((accumulator >> bits) & 31) as usize] as char); }
    }
    encoded.push_str(".onion"); encoded
}

fn text(p: &serde_json::Value, name: &str, limit: usize) -> Result<String, String> {
    let value = p.get(name).and_then(|v| v.as_str()).ok_or_else(|| format!("Backup is missing {name}."))?;
    if value.trim().is_empty() || value.len() > limit {
        return Err(format!("Backup has an invalid {name}."));
    }
    Ok(value.to_owned())
}

fn hex_secret(p: &serde_json::Value, name: &str, length: usize) -> Result<String, String> {
    let value = text(p, name, length)?;
    if value.len() != length || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!("Backup has an invalid {name}."));
    }
    Ok(value)
}

pub(crate) fn validate(p: &serde_json::Value) -> Result<Identity, String> {
    let hostname = text(p, "hostname", 62)?;
    if !pairing::is_valid_onion(&hostname) { return Err("Backup has an invalid onion address.".into()); }
    if let Some(other) = p.get("onion").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
        if other != hostname { return Err("Backup contains conflicting onion addresses.".into()); }
    }
    let secret = Zeroizing::new(B64.decode(text(p, "hs_secret", 128)?).map_err(|_| "Backup onion secret key is corrupt.")?);
    let public = B64.decode(text(p, "hs_public", 88)?).map_err(|_| "Backup onion public key is corrupt.")?;
    if secret.len() != 96 || public.len() != 64 || &secret[..32] != SECRET_HEADER || &public[..32] != PUBLIC_HEADER {
        return Err("Backup does not contain complete Tor v3 identity keys.".into());
    }
    // Tor stores the expanded Ed25519 secret, not a seed/public-key concatenation.
    if secret[32] & 7 != 0 || secret[63] & 0xc0 != 0x40 {
        return Err("Backup contains an invalid expanded Tor secret key.".into());
    }
    let expanded = ExpandedSecretKey::from_bytes(secret[32..].try_into().unwrap());
    let derived = VerifyingKey::from(&expanded);
    if derived.as_bytes() != &public[32..] || onion(derived.as_bytes()) != hostname {
        return Err("Backup onion address and identity keys do not match.".into());
    }
    let username = text(p, "username", 180)?;
    if !username.bytes().all(|b| matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'.' | b'_' | b'=' | b'/' | b'+' | b'-')) {
        return Err("Backup has an invalid username.".into());
    }
    let phrase = match p.get("phrase") {
        None => Vec::new(),
        Some(serde_json::Value::Array(words)) if words.len() <= 24 => words.iter().map(|w| {
            w.as_str().filter(|s| s.len() <= 64).map(str::to_owned).ok_or("Backup recovery metadata is corrupt.")
        }).collect::<Result<Vec<_>, _>>()?,
        _ => return Err("Backup recovery metadata is corrupt.".into()),
    };
    let peers = p.get("pairings").and_then(|v| v.as_array()).ok_or("Backup is missing its pairing list.")?;
    if peers.len() > 4096 { return Err("Backup has too many pairings.".into()); }
    let mut unique = std::collections::BTreeSet::new();
    for peer in peers {
        let peer = peer.as_str().filter(|s| pairing::is_valid_onion(s)).ok_or("Backup has an invalid paired address.")?;
        if peer != hostname { unique.insert(peer.to_owned()); }
    }
    Ok(Identity {
        inner: state::Inner {
            phase: state::Phase::SettingUp, onion: Some(hostname), username, restore_pairings_pending: true,
            box_name: text(p, "box_name", 1024)?, created: text(p, "created", 100)?, phrase,
            token: hex_secret(p, "token", 32)?, turn_secret: hex_secret(p, "turn_secret", 64)?,
            join_token: hex_secret(p, "join_token", 32)?, livekit_api_key: hex_secret(p, "livekit_api_key", 32)?,
            livekit_api_secret: hex_secret(p, "livekit_api_secret", 64)?,
            admin_password: text(p, "admin_password", 1024)?, ..state::Inner::default()
        },
        secret, public, peers: unique.into_iter().collect(),
    })
}

/// Seed a restored account only while BOTH current and legacy records are
/// absent. If publication succeeded before a crash, an existing record wins,
/// including an explicit empty list: retry must not re-add revoked peers.
pub(crate) async fn initialize_pairings(
    client: &reqwest::Client, base: &str, user_id: &str, token: &str, peers: &[String],
) -> Result<(), String> {
    let mut url = reqwest::Url::parse(base).map_err(|_| "Invalid local homeserver address.")?;
    url.path_segments_mut().map_err(|_| "Invalid local homeserver address.")?
        .extend(["_matrix", "client", "v3", "user", user_id, "account_data"]);
    let prefix = url.as_str().trim_end_matches('/').to_owned();
    let current = format!("{prefix}/ai.tournesol.privacylodge.pairings");
    for address in [&current, &format!("{prefix}/ai.tournesol.pureprivacy.pairings")] {
        let response = client.get(address).bearer_auth(token).send().await
            .map_err(|_| "Could not read restored pairing state. Restart to retry.")?;
        if response.status() == reqwest::StatusCode::NOT_FOUND { continue; }
        if !response.status().is_success() { return Err("Could not read restored pairing state. Restart to retry.".into()); }
        let record: serde_json::Value = response.json().await.map_err(|_| "Invalid restored pairing state.")?;
        let valid = record.get("onions").and_then(|v| v.as_array()).is_some_and(|onions|
            onions.iter().all(|onion| onion.as_str().is_some_and(pairing::is_valid_onion)));
        if !valid { return Err("Invalid restored pairing state. Resolve it before starting synchronization.".into()); }
        return Ok(());
    }
    let response = client.put(current).bearer_auth(token).json(&serde_json::json!({"onions": peers}))
        .send().await.map_err(|_| "Could not initialize restored pairings. Restart to retry.")?;
    if !response.status().is_success() { return Err("Could not initialize restored pairings. Restart to retry.".into()); }
    Ok(())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use sha2::Sha512;
    pub(crate) fn payload() -> serde_json::Value {
        let seed = [42u8; 32];
        let mut expanded = Sha512::digest(seed).to_vec(); expanded[0] &= 248; expanded[31] &= 63; expanded[31] |= 64;
        let public = ed25519_dalek::SigningKey::from_bytes(&seed).verifying_key();
        let hostname = onion(public.as_bytes());
        serde_json::json!({"hostname":hostname,"onion":hostname,
            "hs_secret":B64.encode([SECRET_HEADER.as_slice(), expanded.as_slice()].concat()),
            "hs_public":B64.encode([PUBLIC_HEADER.as_slice(), public.as_bytes()].concat()),
            "username":"alice","box_name":"Alice's box","created":"2026-09-13","phrase":[],
            "token":"a".repeat(32),"turn_secret":"b".repeat(64),"join_token":"c".repeat(32),
            "livekit_api_key":"d".repeat(32),"livekit_api_secret":"e".repeat(64),
            "admin_password":"fixture password","pairings":[]})
    }
    #[test] fn validates_complete_identity_and_resets_admin_creation_for_a_new_database() {
        let identity = validate(&payload()).unwrap(); assert!(!identity.inner.admin_created);
        assert_eq!(identity.inner.username, "alice"); assert_eq!(identity.secret.len(), 96);
    }
    #[test] fn rejects_mismatched_address_public_key_and_secret() {
        for field in ["hostname", "hs_secret", "hs_public"] {
            let mut p = payload();
            if field == "hostname" { p[field] = format!("{}.onion", "a".repeat(56)).into(); }
            else { let mut bytes = B64.decode(p[field].as_str().unwrap()).unwrap(); bytes[40] ^= 1; p[field] = B64.encode(bytes).into(); }
            assert!(validate(&p).is_err(), "accepted changed {field}");
        }
    }
    #[test] fn rejects_missing_credentials_and_malformed_pairings() {
        for field in ["username", "admin_password", "token", "turn_secret", "join_token", "livekit_api_key", "livekit_api_secret"] {
            let mut p = payload(); p.as_object_mut().unwrap().remove(field); assert!(validate(&p).is_err(), "accepted missing {field}");
        }
        let mut p = payload(); p["pairings"] = serde_json::json!(["attacker.onion|.*"]); assert!(validate(&p).is_err());
        let mut p = payload(); p["username"] = "bad:name".into(); assert!(validate(&p).is_err());
    }
    #[test] fn rejects_short_keys_even_if_the_backup_is_authenticated() {
        let mut p = payload(); p["hs_secret"] = B64.encode(b"secret").into(); assert!(validate(&p).is_err());
    }
}
