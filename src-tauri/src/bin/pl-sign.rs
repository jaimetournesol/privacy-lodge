//! `pl-sign` — post-quantum half of the release-signing flow (feature K).
//!
//! Ed25519 signing stays with `openssl` in `scripts/sign-release.sh`; this tool adds the
//! SLH-DSA (FIPS-205) signature that makes an update manifest hybrid-signed. A box requires
//! BOTH, so forging one is not enough — an attacker needs to break ed25519 *and* a hash-based
//! scheme, which is the whole point.
//!
//!   pl-sign keygen <secret.bin> <public.hex>   # once; secret goes to _special-project
//!   pl-sign sign <secret.bin> <file>           # prints the base64 signature
//!   pl-sign verify <public.hex> <file> <sig.b64>
//!
//! The secret key NEVER belongs in this repo, in a box, or in a backup.

use fips205::slh_dsa_sha2_128s as slh;
use fips205::traits::{SerDes, Signer, Verifier};
use std::io::Write;

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(s: &str) -> Option<Vec<u8>> {
    let s = s.trim();
    if s.len() % 2 != 0 {
        return None;
    }
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(s.get(i * 2..i * 2 + 2)?, 16).ok())
        .collect()
}

fn b64(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let usage = "usage: pl-sign keygen <secret.bin> <public.hex>\n\
                        pl-sign sign <secret.bin> <file>\n\
                        pl-sign verify <public.hex> <file> <sig.b64>";
    match args.get(1).map(String::as_str) {
        Some("keygen") => {
            let (sk_path, pk_path) = (args.get(2).ok_or(usage)?, args.get(3).ok_or(usage)?);
            let (pk, sk) = slh::try_keygen().map_err(|e| format!("keygen failed: {e}"))?;
            // Secret first, owner-only, before anything else can race it.
            let mut f = std::fs::File::create(sk_path)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                f.set_permissions(std::fs::Permissions::from_mode(0o600))?;
            }
            f.write_all(&sk.into_bytes())?;
            std::fs::write(pk_path, format!("{}\n", hex(&pk.into_bytes())))?;
            eprintln!("wrote secret -> {sk_path} (0600)\nwrote public -> {pk_path}");
            eprintln!("Bake the public hex into updater::UPDATE_PQ_PUBKEY_HEX.");
        }
        Some("sign") => {
            let (sk_path, file) = (args.get(2).ok_or(usage)?, args.get(3).ok_or(usage)?);
            let raw = std::fs::read(sk_path)?;
            let arr: [u8; slh::SK_LEN] = raw
                .as_slice()
                .try_into()
                .map_err(|_| "secret key file is the wrong length")?;
            let sk = slh::PrivateKey::try_from_bytes(&arr).map_err(|e| format!("bad key: {e}"))?;
            let msg = std::fs::read(file)?;
            // hedged = true: fresh randomness per signature, the recommended mode.
            let sig = sk
                .try_sign(&msg, &[], true)
                .map_err(|e| format!("sign failed: {e}"))?;
            println!("{}", b64(&sig));
        }
        Some("verify") => {
            let pk_hex = std::fs::read_to_string(args.get(2).ok_or(usage)?)?;
            let file = args.get(3).ok_or(usage)?;
            let sig_b64 = std::fs::read_to_string(args.get(4).ok_or(usage)?)?;
            let pk_arr: [u8; slh::PK_LEN] = unhex(&pk_hex)
                .ok_or("public key isn't hex")?
                .as_slice()
                .try_into()
                .map_err(|_| "public key is the wrong length")?;
            let pk = slh::PublicKey::try_from_bytes(&pk_arr).map_err(|e| format!("bad key: {e}"))?;
            use base64::Engine;
            let sig_raw = base64::engine::general_purpose::STANDARD.decode(sig_b64.trim())?;
            let sig: [u8; slh::SIG_LEN] = sig_raw
                .as_slice()
                .try_into()
                .map_err(|_| "signature is the wrong length")?;
            if pk.verify(&std::fs::read(file)?, &sig, &[]) {
                println!("OK");
            } else {
                eprintln!("SIGNATURE VERIFICATION FAILED");
                std::process::exit(1);
            }
        }
        _ => {
            eprintln!("{usage}");
            std::process::exit(2);
        }
    }
    Ok(())
}
