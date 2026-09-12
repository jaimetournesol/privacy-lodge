//! Environment-variable compatibility for the rename (Privacy Lodge → Privacy Lodge, 0.2.0).
//!
//! Every runtime knob used to be `PUREPRIVACY_*`; it is now `PRIVACY_LODGE_*`. Boxes in the
//! field still export the old names — a Docker entrypoint from an older image, a testbed
//! script, a hand-written `.env` — so each read prefers the new name and falls back to the old
//! one, warning ONCE per process per variable so the owner learns the new name without the log
//! filling up. The fallback is scheduled for removal one release after 0.2.0.

use std::collections::HashSet;
use std::sync::Mutex;

const OLD_PREFIX: &str = "PUREPRIVACY_";
const NEW_PREFIX: &str = "PRIVACY_LODGE_";

static WARNED: Mutex<Option<HashSet<String>>> = Mutex::new(None);

/// Read `PRIVACY_LODGE_<suffix>`; if unset, read `PUREPRIVACY_<suffix>` (and warn once).
/// Same contract as `std::env::var`: `NotPresent` only when NEITHER name is set.
pub fn var(suffix: &str) -> Result<String, std::env::VarError> {
    let new = format!("{NEW_PREFIX}{suffix}");
    match std::env::var(&new) {
        Ok(v) => Ok(v),
        Err(std::env::VarError::NotPresent) => {
            let old = format!("{OLD_PREFIX}{suffix}");
            let v = std::env::var(&old)?;
            warn_once(&old, &new);
            Ok(v)
        }
        Err(e) => Err(e),
    }
}

fn warn_once(old: &str, new: &str) {
    let mut g = WARNED.lock().unwrap_or_else(|p| p.into_inner());
    let set = g.get_or_insert_with(HashSet::new);
    if set.insert(old.to_string()) {
        eprintln!(
            "[privacy-lodge] {old} is the pre-rename name — set {new} instead \
             (the old name still works in this release)"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Each test uses its own suffix: the process environment is shared across tests.
    #[test]
    fn old_name_alone_is_honoured() {
        std::env::set_var("PUREPRIVACY_ENVCOMPAT_T1", "old");
        std::env::remove_var("PRIVACY_LODGE_ENVCOMPAT_T1");
        assert_eq!(var("ENVCOMPAT_T1").as_deref(), Ok("old"));
    }

    #[test]
    fn new_name_wins_when_both_are_set() {
        std::env::set_var("PUREPRIVACY_ENVCOMPAT_T2", "old");
        std::env::set_var("PRIVACY_LODGE_ENVCOMPAT_T2", "new");
        assert_eq!(var("ENVCOMPAT_T2").as_deref(), Ok("new"));
    }

    #[test]
    fn neither_set_is_not_present() {
        std::env::remove_var("PUREPRIVACY_ENVCOMPAT_T3");
        std::env::remove_var("PRIVACY_LODGE_ENVCOMPAT_T3");
        assert!(matches!(var("ENVCOMPAT_T3"), Err(std::env::VarError::NotPresent)));
    }
}
