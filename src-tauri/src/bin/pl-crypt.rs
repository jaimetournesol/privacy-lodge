//! `pl-crypt` — passphrase encryption for `pl-box` backup bundles.
//!
//! A backup bundle IS the box: onion key, secrets, agent API keys, transcripts, and the
//! `PL_SECRETS_KEY` that unlocks them. `pl-box backup --encrypt` pipes the bundle through
//! this tool so what lands on disk is useless without the passphrase.
//!
//! Same cryptography as the box's feature-D identity backup — PBKDF2-HMAC-SHA256 (200k
//! rounds, random salt) into AES-256-GCM — and not merely the same *choice*: `seal`/`open`
//! call the box's own `crypto` module, so the two paths cannot drift apart. Wrong
//! passphrase fails closed via GCM authentication; there is no "decrypts to garbage".
//!
//!   pl-crypt seal < bundle.tgz > bundle.tgz.enc     # passphrase in $PL_PASSPHRASE
//!   pl-crypt open < bundle.tgz.enc > bundle.tgz     # same
//!
//! File format (sniff it by grepping LINE 1 for `"ppcrypt":1` — serde_json writes the keys
//! alphabetised, so the file starts with `{"created":`, not `{"ppcrypt":`):
//!   line 1: JSON header — version, KDF params, salt, payload type, creation stamp
//!           ($PL_META_CREATED). Deliberately NOT the onion: the filename already carries
//!           12 chars of it, which is all an owner needs to tell files apart, and the full
//!           address in the clear would let anyone who finds the .enc on a cloud drive tie
//!           that account to the box — the one link this product exists to prevent.
//!   rest:   raw nonce ‖ ciphertext‖tag bytes.
//! The passphrase rides an environment variable, not argv: argv is world-readable in
//! /proc/*/cmdline for the process's lifetime; the env of a short-lived process is not.

use privacy_lodge_lib::crypto;
use std::io::{Read, Write};

const KDF_ITERS: u32 = 200_000; // matches backup.rs (feature D)
const MIN_PASSPHRASE: usize = 8;

fn derive_key(passphrase: &str, salt: &[u8]) -> [u8; 32] {
    let mut key = [0u8; 32];
    pbkdf2::pbkdf2_hmac::<sha2::Sha256>(passphrase.as_bytes(), salt, KDF_ITERS, &mut key);
    key
}

fn b64(data: &[u8]) -> String {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.encode(data)
}

fn unb64(s: &str) -> Result<Vec<u8>, String> {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD
        .decode(s)
        .map_err(|e| format!("bad base64: {e}"))
}

fn passphrase() -> Result<String, String> {
    let p = std::env::var("PL_PASSPHRASE").map_err(|_| "PL_PASSPHRASE is not set".to_string())?;
    if p.chars().count() < MIN_PASSPHRASE {
        return Err(format!("passphrase must be at least {MIN_PASSPHRASE} characters"));
    }
    Ok(p)
}

fn seal() -> Result<(), String> {
    let pass = passphrase()?;
    let mut plain = Vec::new();
    std::io::stdin().read_to_end(&mut plain).map_err(|e| format!("reading stdin: {e}"))?;
    if plain.is_empty() {
        return Err("refusing to seal an empty input (is the bundle really on stdin?)".into());
    }

    let mut salt = [0u8; 16];
    use rand::RngCore as _;
    rand::thread_rng().fill_bytes(&mut salt);
    let sealed = crypto::seal_bytes(&plain, &derive_key(&pass, &salt))?;

    let header = serde_json::json!({
        "ppcrypt": 1,
        "kdf": "pbkdf2-hmac-sha256",
        "iters": KDF_ITERS,
        "salt": b64(&salt),
        "payload": "tar+gzip",
        "created": std::env::var("PL_META_CREATED").unwrap_or_default(),
    });
    let out = std::io::stdout();
    let mut out = out.lock();
    writeln!(out, "{header}").map_err(|e| format!("writing header: {e}"))?;
    out.write_all(&sealed).map_err(|e| format!("writing ciphertext: {e}"))?;
    Ok(())
}

fn open() -> Result<(), String> {
    let pass = passphrase()?;
    let mut input = Vec::new();
    std::io::stdin().read_to_end(&mut input).map_err(|e| format!("reading stdin: {e}"))?;

    let nl = input
        .iter()
        .position(|&b| b == b'\n')
        .ok_or("not a pl-crypt file (no header line)")?;
    let header: serde_json::Value = serde_json::from_slice(&input[..nl])
        .map_err(|_| "not a pl-crypt file (header is not JSON)")?;
    if header.get("ppcrypt").and_then(|v| v.as_u64()) != Some(1) {
        return Err("unsupported pl-crypt version".into());
    }
    let iters = header.get("iters").and_then(|v| v.as_u64()).unwrap_or(0);
    if iters != u64::from(KDF_ITERS) {
        // Fail loud rather than silently deriving with foreign params: a mismatch means a
        // newer tool wrote this file, and the caller should upgrade, not guess.
        return Err(format!("unsupported KDF iteration count {iters}"));
    }
    let salt = unb64(header.get("salt").and_then(|v| v.as_str()).ok_or("header missing salt")?)?;

    let plain = crypto::open_bytes(&input[nl + 1..], &derive_key(&pass, &salt))
        .map_err(|_| "wrong passphrase, or the file is damaged".to_string())?;
    std::io::stdout()
        .write_all(&plain)
        .map_err(|e| format!("writing plaintext: {e}"))?;
    Ok(())
}

fn main() {
    let mode = std::env::args().nth(1).unwrap_or_default();
    let result = match mode.as_str() {
        "seal" => seal(),
        "open" => open(),
        _ => Err("usage: pl-crypt seal|open  (passphrase in $PL_PASSPHRASE, data on stdin)".into()),
    };
    if let Err(e) = result {
        eprintln!("pl-crypt: {e}");
        std::process::exit(1);
    }
}
