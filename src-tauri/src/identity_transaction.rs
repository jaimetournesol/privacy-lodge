//! Publish a fresh identity as a recoverable transaction. The journal is resolved
//! before startup reads box.json; no existing identity file is overwritten.
use std::{fs, io::Write, path::{Path, PathBuf}, sync::{Mutex, MutexGuard}};
use serde::{Deserialize, Serialize};

static WRITER: Mutex<()> = Mutex::new(());
const JOURNAL: &str = ".identity-transaction.json";
const ALLOWED: [&str; 6] = ["secrets.json", "pairings.json", "data/tor/hs/hostname",
    "data/tor/hs/hs_ed25519_secret_key", "data/tor/hs/hs_ed25519_public_key", "box.json"];

pub(crate) fn lock() -> MutexGuard<'static, ()> { WRITER.lock().unwrap_or_else(|p| p.into_inner()) }

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Journal { version: u8, stage: String, files: Vec<String>, committed: bool }

fn sync_dir(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    fs::File::open(path).and_then(|f| f.sync_all()).map_err(|e| format!("Could not sync identity directory: {e}"))?;
    Ok(())
}

fn private_dir(path: &Path) -> Result<(), String> {
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)] { use std::os::unix::fs::DirBuilderExt; builder.mode(0o700); }
    builder.create(path).map_err(|e| format!("Could not prepare identity directory: {e}"))
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut options = fs::OpenOptions::new(); options.write(true).create_new(true);
    #[cfg(unix)] { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
    let mut file = options.open(path).map_err(|e| format!("Could not prepare identity file: {e}"))?;
    file.write_all(bytes).and_then(|_| file.sync_all()).map_err(|e| format!("Could not save identity file: {e}"))
}

fn target(root: &Path, name: &str, create_parents: bool) -> Result<PathBuf, String> {
    if !ALLOWED.contains(&name) { return Err("Unrecognized identity transaction file.".into()); }
    let mut current = root.to_path_buf();
    for component in Path::new(name).parent().unwrap().components() {
        current.push(component);
        match fs::symlink_metadata(&current) {
            Ok(meta) if meta.is_dir() && !meta.file_type().is_symlink() => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                if create_parents { private_dir(&current)?; sync_dir(current.parent().unwrap())?; }
            },
            _ => return Err("Identity directories must not be symlinks or ordinary files.".into()),
        }
    }
    Ok(root.join(name))
}

fn save_journal(root: &Path, journal: &Journal) -> Result<(), String> {
    let temporary = root.join(&journal.stage).join("journal.next");
    write_new(&temporary, &serde_json::to_vec(journal).map_err(|e| e.to_string())?)?;
    fs::rename(&temporary, root.join(JOURNAL)).map_err(|e| format!("Could not publish identity journal: {e}"))?;
    sync_dir(root)
}

fn prepare(root: &Path, files: &[(&str, &[u8])]) -> Result<Journal, String> {
    recover(root)?;
    if files.len() > ALLOWED.len() || !files.iter().any(|(n, _)| *n == "box.json")
        || !files.iter().any(|(n, _)| *n == "secrets.json") {
        return Err("Incomplete identity transaction.".into());
    }
    let mut names = std::collections::HashSet::new();
    for (name, _) in files {
        let path = target(root, name, false)?;
        if !names.insert(*name) || fs::symlink_metadata(path).is_ok() {
            return Err("Identity data already exists. Restore onto a fresh box.".into());
        }
    }
    let journal = Journal { version: 1, stage: format!(".identity-stage-{:032x}", rand::random::<u128>()),
        files: files.iter().map(|(n, _)| n.to_string()).collect(), committed: false };
    let stage = root.join(&journal.stage); private_dir(&stage)?;
    let prepared = (|| {
        for (index, (_, bytes)) in files.iter().enumerate() { write_new(&stage.join(index.to_string()), bytes)?; }
        sync_dir(&stage)?; save_journal(root, &journal)
    })();
    if prepared.is_err() && !root.join(JOURNAL).exists() { let _ = fs::remove_dir_all(&stage); }
    prepared?; Ok(journal)
}

fn install_one(root: &Path, journal: &Journal, index: usize) -> Result<(), String> {
    let path = target(root, &journal.files[index], true)?;
    // Atomic no-clobber publication on the same filesystem. The staged bytes
    // are already private and fsynced. ext4/APFS/NTFS support these links.
    fs::hard_link(root.join(&journal.stage).join(index.to_string()), &path)
        .map_err(|e| format!("Could not install identity file without overwriting existing data: {e}"))?;
    sync_dir(path.parent().unwrap())
}

/// Caller holds the identity writer lock through publication of in-memory state.
pub(crate) fn commit(root: &Path, files: &[(&str, &[u8])]) -> Result<(), String> {
    let mut journal = prepare(root, files)?;
    let installed = (|| {
        for index in 0..journal.files.len() { install_one(root, &journal, index)?; }
        journal.committed = true; save_journal(root, &journal)
    })();
    if let Err(error) = installed {
        // If the durable commit marker was published, retain the complete identity.
        // A directory-sync failure remains an error; startup resolves its marker.
        return match recover(root) { Ok(()) => Err(error), Err(_) => Err(format!("{error} Identity recovery is pending; restart before trying setup again.")) };
    }
    // Cleanup cannot turn a committed identity into a reported failed restore.
    let _ = recover(root);
    Ok(())
}

/// On interruption, remove only links this transaction actually installed.
/// Never traverse arbitrary journal paths or remove a conflicting pre-existing file.
pub(crate) fn recover(root: &Path) -> Result<(), String> {
    let path = root.join(JOURNAL);
    let meta = match fs::symlink_metadata(&path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Ok(meta) if meta.is_file() && !meta.file_type().is_symlink() && meta.len() <= 4096 => meta,
        _ => return Err("Identity recovery journal is invalid.".into()),
    };
    let _ = meta;
    let journal: Journal = serde_json::from_slice(&fs::read(&path).map_err(|_| "Cannot read identity recovery journal.")?)
        .map_err(|_| "Identity recovery journal is invalid.")?;
    let suffix = journal.stage.strip_prefix(".identity-stage-").ok_or("Invalid identity staging path.")?;
    if journal.version != 1 || suffix.len() != 32 || !suffix.bytes().all(|b| b.is_ascii_hexdigit())
        || journal.files.len() > ALLOWED.len() || journal.files.iter().any(|n| !ALLOWED.contains(&n.as_str())) {
        return Err("Identity recovery journal is invalid.".into());
    }
    let stage = root.join(&journal.stage);
    if let Ok(meta) = fs::symlink_metadata(&stage) {
        if !meta.is_dir() || meta.file_type().is_symlink() { return Err("Invalid identity staging directory.".into()); }
    }
    if !journal.committed {
        for (index, name) in journal.files.iter().enumerate() {
            let destination = target(root, name, false)?;
            let installed = match fs::symlink_metadata(&destination) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Ok(m) if m.is_file() && !m.file_type().is_symlink() => m,
                _ => return Err("Conflicting data prevents identity recovery.".into()),
            };
            let staged = stage.join(index.to_string());
            let source = fs::symlink_metadata(&staged).map_err(|_| "Identity staging data is missing.")?;
            if !source.is_file() || source.file_type().is_symlink() { return Err("Invalid identity staging data.".into()); }
            #[cfg(unix)]
            let ours = { use std::os::unix::fs::MetadataExt; installed.ino() == source.ino() && installed.dev() == source.dev() };
            #[cfg(not(unix))]
            let ours = installed.len() <= 1024 * 1024 && installed.len() == source.len()
                && fs::read(&destination).ok() == fs::read(&staged).ok();
            if !ours { return Err("Conflicting data prevents identity recovery.".into()); }
            fs::remove_file(&destination).map_err(|_| "Could not remove incomplete identity data.")?;
            sync_dir(destination.parent().unwrap())?;
        }
    }
    if stage.exists() { fs::remove_dir_all(&stage).map_err(|_| "Could not clean identity staging data.")?; }
    fs::remove_file(&path).map_err(|_| "Could not finish identity recovery.")?;
    sync_dir(root)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn files() -> Vec<(&'static str, &'static [u8])> {
        vec![("secrets.json", b"encrypted fixture"), ("pairings.json", b"paired fixture"),
            ("data/tor/hs/hostname", b"address fixture"), ("box.json", b"identity fixture")]
    }
    #[test] fn interruption_at_every_publication_point_recovers_a_fresh_box() {
        for count in 0..=files().len() {
            let root = tempfile::tempdir().unwrap(); let entries = files();
            fs::write(root.path().join("unrelated"), b"keep").unwrap();
            let journal = prepare(root.path(), &entries).unwrap();
            for index in 0..count { install_one(root.path(), &journal, index).unwrap(); }
            recover(root.path()).unwrap();
            for (name, _) in entries { assert!(!root.path().join(name).exists(), "left {name} at step {count}"); }
            assert_eq!(fs::read(root.path().join("unrelated")).unwrap(), b"keep");
            commit(root.path(), &files()).unwrap(); // failure never prevents a retry
        }
    }
    #[test] fn committed_identity_survives_a_crash_before_staging_cleanup() {
        let root = tempfile::tempdir().unwrap(); let entries = files();
        let mut journal = prepare(root.path(), &entries).unwrap();
        for index in 0..entries.len() { install_one(root.path(), &journal, index).unwrap(); }
        journal.committed = true; save_journal(root.path(), &journal).unwrap();
        recover(root.path()).unwrap();
        for (name, bytes) in entries { assert_eq!(fs::read(root.path().join(name)).unwrap(), bytes); }
    }
    #[test] fn refuses_to_overwrite_existing_identity_files() {
        let root = tempfile::tempdir().unwrap(); fs::write(root.path().join("secrets.json"), b"original").unwrap();
        assert!(commit(root.path(), &files()).is_err());
        assert_eq!(fs::read(root.path().join("secrets.json")).unwrap(), b"original");
        assert!(!root.path().join("box.json").exists());
    }
    #[test] fn journal_cannot_escape_the_data_directory() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join(JOURNAL), br#"{"version":1,"stage":"../outside","files":["box.json"],"committed":false}"#).unwrap();
        assert!(recover(root.path()).is_err());
    }
    #[cfg(unix)]
    #[test] fn rejects_symlink_parents_and_creates_private_files() {
        use std::os::unix::{fs::symlink, fs::PermissionsExt};
        let root = tempfile::tempdir().unwrap(); let outside = tempfile::tempdir().unwrap();
        symlink(outside.path(), root.path().join("data")).unwrap();
        assert!(commit(root.path(), &files()).is_err()); assert_eq!(fs::read_dir(outside.path()).unwrap().count(), 0);
        fs::remove_file(root.path().join("data")).unwrap(); commit(root.path(), &files()).unwrap();
        for (name, _) in files() { assert_eq!(fs::metadata(root.path().join(name)).unwrap().permissions().mode() & 0o777, 0o600); }
    }
}
