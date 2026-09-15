//! The one sha256 in this crate.
//!
//! It is UNCONDITIONAL — not behind the `fetch` feature — because two callers
//! need it and only one of them is the downloader. The other is the loader:
//! `RegistryOptions::verify_checksums` re-hashes a library against its
//! `manifest.json` before `dlopen` (`docs/reference/artifact.md`
//! §Verification), and a consumer who builds `--no-default-features` (no
//! downloader; artifacts arriving by a path this crate never sees) is exactly
//! the one who most needs that check. One implementation, so the loader and
//! the fetch chain can never hash differently; `crate::fetch::trust`
//! re-exports these two names, which is where the published
//! `fetch::sha256_file` / `fetch::sha256_hex` paths still resolve.

use std::io::Read;
use std::path::Path;

use sha2::{Digest, Sha256};

/// Lowercase hex sha256 of `bytes`.
///
/// Only the fetch chain hashes a buffer (the loader streams a file), so
/// without that feature this is reachable from the tests alone.
#[cfg_attr(not(feature = "fetch"), allow(dead_code))]
pub fn sha256_hex(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}

/// Lowercase hex sha256 of a file, streamed — a library is hundreds of
/// megabytes and is never read into memory to be hashed.
pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex(&hasher.finalize()))
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write;
        let _ = write!(s, "{b:02x}");
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_digest_is_the_one_every_sha256sums_line_carries() {
        // `printf 'hello\n' | sha256sum` — the value a release file is
        // compared against, so a change in this module is a test failure here
        // rather than a silent disagreement with the published sums.
        assert_eq!(
            sha256_hex(b"hello\n"),
            "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
        );
    }

    #[test]
    fn a_file_hashes_to_the_same_value_as_its_bytes() {
        let path = std::env::temp_dir().join(format!("chtypes-digest-{}", std::process::id()));
        std::fs::write(&path, b"hello\n").unwrap();
        assert_eq!(sha256_file(&path).unwrap(), sha256_hex(b"hello\n"));
        std::fs::remove_file(&path).ok();
    }
}
