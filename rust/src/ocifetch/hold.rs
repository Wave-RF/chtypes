//! The in-use signal (docs/guides/fetch-v1.md §1, "In use"; public issue
//! #494). A process that is about to load an installed build, or that a
//! registry's fetch handed one to, takes a SHARED advisory lock (flock(2)) on
//! the entry's `verified.json` and keeps it until the process exits. `chtypes
//! prune` removes an entry only after it has taken the EXCLUSIVE lock without
//! waiting, so it never removes a build a live process holds. The kernel drops
//! a dead process's locks, so a crash never pins a build. flock belongs to the
//! open file description on Linux and darwin, so a hold taken by this very
//! process counts too, and so does one taken by any binding: all four lock the
//! same file the same way. It is flock, never fcntl or lockf, whose locks
//! belong to the process and would not be seen by a prune in it.
//!
//! Only the registry (the public API) holds: the bare seam functions, the CLI
//! and the conformance runner's ordinary calls never do, so they accumulate
//! nothing.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::ErrorKind;
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use super::constants;
use super::error::Error;

/// What [`hold`] found.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HoldState {
    /// This process holds the entry, shared, for its life.
    Held,
    /// The record cannot be locked here (a filesystem without flock, or an
    /// error other than the entry being gone). The caller goes on: a prune on
    /// the same filesystem cannot take its exclusive lock either, so it keeps
    /// the entry.
    Unheld,
    /// The entry is gone, or another entry has replaced it: a prune removed it
    /// after the lookup chose it. The caller looks again.
    Vanished,
}

/// This process's shared locks: one open record per entry directory, kept
/// until the process exits. The table is a static and is never dropped, so a
/// held file is never closed.
static HOLDS: Mutex<BTreeMap<PathBuf, File>> = Mutex::new(BTreeMap::new());

/// Take this process's shared hold on the installed entry `dir`
/// (`<root>/unpacked/sha256/<manifest hex>`) and keep it for the life of the
/// process. It waits only while a prune holds the entry exclusively, which a
/// prune does across one rename; then the entry is gone, and this says so.
pub fn hold(dir: &Path) -> HoldState {
    let record = dir.join(constants::CACHE_VERIFIED_RECORD);
    let mut holds = HOLDS.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(held) = holds.get(dir) {
        if same_file(held, &record) {
            return HoldState::Held;
        }
        // The entry this process held was removed and installed again: the
        // old hold protects nothing now. Removing it closes it.
        holds.remove(dir);
    }
    let file = match File::open(&record) {
        Ok(file) => file,
        Err(e) if gone(&e) => return HoldState::Vanished,
        Err(_) => return HoldState::Unheld,
    };
    if lock_shared(&file).is_err() {
        return HoldState::Unheld;
    }
    if !same_file(&file, &record) {
        return HoldState::Vanished;
    }
    holds.insert(dir.to_path_buf(), file);
    HoldState::Held
}

/// What [`claim`] found.
#[derive(Debug)]
pub enum Claim {
    /// Locked exclusively: no process holds the entry. Dropping the file
    /// releases the lock.
    Owned(File),
    /// A process holds the entry, or it cannot be locked at all: kept.
    InUse,
    /// Gone, or replaced since it was listed: not this prune's to report.
    Gone,
}

/// Take the exclusive lock a prune needs on the entry `dir`, without waiting.
/// It is owned only when no process holds the entry and the record it locked
/// is still the one at the path.
pub fn claim(dir: &Path) -> Claim {
    let record = dir.join(constants::CACHE_VERIFIED_RECORD);
    let file = match File::open(&record) {
        Ok(file) => file,
        Err(e) if gone(&e) => return Claim::Gone,
        Err(_) => return Claim::InUse,
    };
    if try_lock_exclusive(&file).is_err() {
        return Claim::InUse;
    }
    if !same_file(&file, &record) {
        return Claim::Gone;
    }
    Claim::Owned(file)
}

/// `CHTYPES_ARTIFACT_MISSING` for a request whose build a concurrent prune
/// removed twice, each time between its install and this process's hold.
pub fn removed_while_held(request: &str, dir: &Path) -> Error {
    Error::ArtifactMissing(format!(
        "{} was removed by a concurrent prune before this process could hold it; ask for {request} again",
        dir.display()
    ))
}

/// Whether the open `file` is still the file at `path` (the same device and
/// inode); `false` when either cannot be read.
fn same_file(file: &File, path: &Path) -> bool {
    let (Ok(held), Ok(now)) = (file.metadata(), std::fs::metadata(path)) else {
        return false;
    };
    held.dev() == now.dev() && held.ino() == now.ino()
}

/// The entry is gone: the record or a directory above it does not exist.
fn gone(e: &std::io::Error) -> bool {
    matches!(e.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory)
}

/// flock(LOCK_SH) on `file`, waiting while an exclusive lock is held, and
/// retrying a wait a signal interrupted.
fn lock_shared(file: &File) -> std::io::Result<()> {
    loop {
        // SAFETY: `file` is borrowed for the whole call, so its descriptor is
        // open and valid; flock(2) reads nothing but the descriptor and the
        // operation, and touches no memory of this process.
        let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_SH) };
        if rc == 0 {
            return Ok(());
        }
        let e = std::io::Error::last_os_error();
        if e.kind() != ErrorKind::Interrupted {
            return Err(e);
        }
    }
}

/// flock(LOCK_EX | LOCK_NB) on `file`, or fail at once.
fn try_lock_exclusive(file: &File) -> std::io::Result<()> {
    // SAFETY: as in `lock_shared`: a valid, borrowed descriptor and a plain
    // integer operation, and no memory of this process is touched.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

/// The hold within one process: flock belongs to the open file description,
/// so a prune's claim in this process sees this process's own hold.
#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        use std::time::{SystemTime, UNIX_EPOCH};
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = std::env::temp_dir().join(format!(
            "ocifetch-hold-{name}-{}-{nanos:x}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A hold of an entry that is gone is `Vanished`; one whose record was
    /// replaced takes a new hold on the new record; and a claim of an entry
    /// this process holds is `InUse`.
    #[test]
    fn a_hold_sees_an_entry_gone_or_replaced_and_a_claim_sees_the_hold() {
        let base = scratch("replaced");
        assert_eq!(hold(&base.join("absent")), HoldState::Vanished);
        let dir = base.join("entry");
        std::fs::create_dir_all(&dir).unwrap();
        let record = dir.join(constants::CACHE_VERIFIED_RECORD);
        std::fs::write(&record, b"{}").unwrap();
        assert_eq!(hold(&dir), HoldState::Held);
        assert_eq!(hold(&dir), HoldState::Held, "a second hold is the first");
        // Removed and installed again: the old hold protects nothing, and a
        // new one is taken on the new record.
        std::fs::remove_file(&record).unwrap();
        std::fs::write(&record, b"{}").unwrap();
        assert_eq!(hold(&dir), HoldState::Held);
        assert!(
            matches!(claim(&dir), Claim::InUse),
            "a claim of an entry this process holds must be in use"
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    /// An entry nobody holds is owned by one claim at a time, and its release
    /// lets the next claim own it.
    #[test]
    fn a_claim_owns_an_entry_nobody_holds_until_it_is_released() {
        let base = scratch("claim");
        let dir = base.join("entry");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(constants::CACHE_VERIFIED_RECORD), b"{}").unwrap();
        let Claim::Owned(owned) = claim(&dir) else {
            panic!("an entry nobody holds is owned");
        };
        assert!(matches!(claim(&dir), Claim::InUse), "one owner at a time");
        drop(owned);
        assert!(
            matches!(claim(&dir), Claim::Owned(_)),
            "released, it is owned again"
        );
        assert!(matches!(claim(&base.join("absent")), Claim::Gone));
        let _ = std::fs::remove_dir_all(&base);
    }
}
