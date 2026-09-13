//! Claims precede side effects; outcomes precede publication. Never replay interrupted work.
use std::{collections::{HashMap, HashSet}, path::Path, sync::Mutex};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
static LOCK: Mutex<()> = Mutex::new(());
const RETAIN_MS: u64 = 24 * 60 * 60 * 1000;

#[derive(Clone, Deserialize, Serialize)]
pub struct Receipt {
    expires: u64,
    #[serde(default)]
    outcome: Option<Value>,
    #[serde(default)]
    published: bool,
}
#[derive(Deserialize)]
#[serde(untagged)]
enum Stored { Legacy(u64), Current(Receipt) }

fn load(path: &Path, now: u64) -> Result<HashMap<String, Receipt>, String> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(HashMap::new()),
        Err(e) => return Err(format!("Cannot read command receipts: {e}")),
    };
    let entries: HashMap<String, Stored> = serde_json::from_str(&raw)
        .map_err(|_| "Command receipts are corrupt; refusing to replay commands.".to_string())?;
    Ok(entries.into_iter().filter_map(|(id, entry)| {
        let receipt = match entry {
            Stored::Legacy(expires) => Receipt { expires, outcome: None, published: false },
            Stored::Current(receipt) => receipt,
        };
        (receipt.expires.saturating_add(RETAIN_MS) >= now).then_some((id, receipt))
    }).collect())
}
fn save(path: &Path, entries: &HashMap<String, Receipt>) -> Result<(), String> {
    crate::state::write_private(path, &serde_json::to_string(entries).map_err(|e| e.to_string())?)
}
pub fn claim(path: &Path, id: &str, expires: u64, now: u64) -> Result<bool, String> {
    let _guard = LOCK.lock().map_err(|_| "Command journal is unavailable")?;
    let mut entries = load(path, now)?;
    if entries.contains_key(id) { return Ok(false); }
    if id.is_empty() || id.len() > 128 || expires <= now { return Err("Invalid command receipt".into()); }
    if entries.len() >= 10000 { return Err("Too many retained command receipts.".into()); }
    entries.insert(id.to_owned(), Receipt { expires, outcome: None, published: false });
    save(path, &entries)?;
    Ok(true)
}

/// Called once by the new supervisor. A crash may have occurred after a side effect;
/// an unknown outcome must be shown as interrupted, never retried or called successful.
pub fn handled(path: &Path, now: u64) -> Result<HashSet<String>, String> {
    let _guard = LOCK.lock().map_err(|_| "Command journal is unavailable")?;
    let mut entries = load(path, now)?;
    for (id, receipt) in &mut entries {
        if receipt.outcome.is_none() || receipt.outcome.as_ref().and_then(|outcome| outcome.get("phase")).and_then(Value::as_str) == Some("accepted") {
            receipt.outcome = Some(json!({"id":id,"ok":false,"done":true,"phase":"interrupted","done_ts":now,
                "error":"The box restarted before recording an outcome. Check its state before trying again."}));
            receipt.published = false;
        }
    }
    save(path, &entries)?;
    Ok(entries.into_keys().collect())
}
pub fn finish(path: &Path, id: &str, outcome: &Value, now: u64) -> Result<(), String> {
    let _guard = LOCK.lock().map_err(|_| "Command journal is unavailable")?;
    if outcome.get("id").and_then(Value::as_str) != Some(id) || outcome.get("ok").and_then(Value::as_bool).is_none() {
        return Err("Invalid command outcome".into());
    }
    let mut entries = load(path, now)?;
    let receipt = entries.get_mut(id).ok_or("Command receipt is missing")?;
    receipt.outcome = Some(outcome.clone()); receipt.published = false;
    save(path, &entries)
}
pub fn pending(path: &Path, now: u64) -> Result<Vec<(String, Value)>, String> {
    let _guard = LOCK.lock().map_err(|_| "Command journal is unavailable")?;
    let mut outcomes: Vec<_> = load(path, now)?.into_iter().filter_map(|(id, receipt)| {
        if receipt.published { None } else { receipt.outcome.map(|value| (id, value)) }
    }).collect();
    outcomes.sort_by_key(|(_, value)| value.get("done_ts").and_then(Value::as_u64).unwrap_or(0));
    Ok(outcomes)
}
pub fn published(path: &Path, id: &str, outcome: &Value, now: u64) -> Result<(), String> {
    let _guard = LOCK.lock().map_err(|_| "Command journal is unavailable")?;
    let mut entries = load(path, now)?;
    if let Some(receipt) = entries.get_mut(id) {
        // A delayed acknowledgement cannot mark a newer result as delivered.
        if receipt.outcome.as_ref() == Some(outcome) { receipt.published = true; }
    }
    save(path, &entries)
}
pub fn result_key(id: &str) -> String {
    use sha2::Digest;
    format!("ai.tournesol.privacylodge.command_result.{:x}", sha2::Sha256::digest(id.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Temp(std::path::PathBuf);
    impl Temp { fn new() -> Self { Self(std::env::temp_dir().join(format!("lodge-receipts-{}.json", rand::random::<u64>()))) } }
    impl Drop for Temp { fn drop(&mut self) { let _ = std::fs::remove_file(&self.0); } }
    #[test]
    fn receipt_survives_restart_and_expires_only_after_command() {
        let path = Temp::new();
        assert_eq!(claim(&path.0, "request", 200, 100), Ok(true));
        assert!(handled(&path.0, 150).unwrap().contains("request"));
        assert_eq!(claim(&path.0, "request", 200, 150), Ok(false));
        assert!(handled(&path.0, 201 + RETAIN_MS).unwrap().is_empty());
        std::fs::write(&path.0, "corrupt").unwrap();
        assert!(claim(&path.0, "another", 300, 201).is_err());
    }
    #[test]
    fn crash_after_claim_reports_uncertainty_without_reexecution() {
        let path = Temp::new(); claim(&path.0, "request", 200, 100).unwrap();
        assert!(pending(&path.0, 110).unwrap().is_empty());
        handled(&path.0, 120).unwrap();
        let outcome = pending(&path.0, 120).unwrap().pop().unwrap().1;
        assert_eq!(outcome["phase"], "interrupted"); assert_eq!(outcome["ok"], false);
        assert!(!claim(&path.0, "request", 220, 121).unwrap());
    }
    #[test]
    fn completed_outcome_retries_publication_and_stale_ack_cannot_hide_new_result() {
        let path = Temp::new(); claim(&path.0, "request", 200, 100).unwrap();
        let first = json!({"id":"request","ok":true,"done_ts":110,"phase":"completed"});
        finish(&path.0,"request",&first,110).unwrap(); handled(&path.0,120).unwrap();
        assert_eq!(pending(&path.0,120).unwrap()[0].1, first);
        let final_result = json!({"id":"request","ok":false,"done_ts":130,"error":"could not restart"});
        finish(&path.0,"request",&final_result,130).unwrap();
        published(&path.0,"request",&first,140).unwrap();
        assert_eq!(pending(&path.0,140).unwrap()[0].1, final_result);
        published(&path.0,"request",&final_result,150).unwrap();
        assert!(pending(&path.0,160).unwrap().is_empty());
        assert!(!claim(&path.0,"request",300,170).unwrap());
    }
    #[test]
    fn legacy_receipt_migrates_without_replay_and_results_are_separate() {
        let path = Temp::new(); std::fs::write(&path.0,r#"{"old":200}"#).unwrap();
        handled(&path.0,100).unwrap(); assert_eq!(pending(&path.0,100).unwrap()[0].1["phase"],"interrupted");
        assert_ne!(result_key("old"),result_key("other"));
        assert!(!result_key("a/b?secret").contains('/'));
    }
    #[test]
    fn acceptance_alone_is_not_completion_after_a_crash() {
        let path = Temp::new(); claim(&path.0,"reset",200,100).unwrap();
        let accepted = json!({"id":"reset","ok":true,"done_ts":110,"phase":"accepted"});
        finish(&path.0,"reset",&accepted,110).unwrap();
        published(&path.0,"reset",&accepted,111).unwrap();
        handled(&path.0,120).unwrap();
        assert_eq!(pending(&path.0,120).unwrap()[0].1["phase"],"interrupted");
        assert!(!claim(&path.0,"reset",220,120).unwrap());
    }
}
