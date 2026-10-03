//! Bytes: verify-then-decode, the zstd window cap, and the tar safety rules
//! (plan §1.1.4 "Bytes", §4 "Lane Rust / zstd"). The caller (`ensure.rs`)
//! verifies the layer's digest and size **before** anything here runs —
//! this module never sees unverified bytes.

use std::collections::HashSet;
use std::io::Read as _;
use std::path::{Component, Path, PathBuf};

use super::constants;
use super::error::{Error, Result};

/// Decompress a (possibly multi-frame) zstd stream, enforcing
/// `constants::ZSTD_WINDOW_LOG_MAX` on every frame and
/// `constants::MAX_UNPACKED_BYTES` on the total output.
///
/// `ruzstd::decoding::StreamingDecoder` only ever decodes a single frame
/// (its own documented limitation), so this loops, re-creating the decoder
/// for each frame, reading from the same cursor so each frame picks up
/// exactly where the last one's bytes ended.
pub fn decompress_zstd(compressed: &[u8]) -> Result<Vec<u8>> {
    let total = compressed.len() as u64;
    let mut cursor = std::io::Cursor::new(compressed);
    let mut out = Vec::new();
    while cursor.position() < total {
        let frame_start = cursor.position();
        let mut decoder = ruzstd::decoding::FrameDecoder::new();
        decoder.set_max_window_size(1u64 << constants::ZSTD_WINDOW_LOG_MAX);
        let mut streaming = ruzstd::decoding::StreamingDecoder::new_with_decoder(
            &mut cursor,
            decoder,
        )
        .map_err(|e| {
            Error::ArtifactCorrupt(format!("zstd frame header at byte {frame_start}: {e:?}"))
        })?;
        streaming
            .read_to_end(&mut out)
            .map_err(|e| Error::ArtifactCorrupt(format!("zstd decode: {e}")))?;
        if out.len() as u64 > constants::MAX_UNPACKED_BYTES {
            return Err(Error::ArtifactCorrupt(format!(
                "decompressed output exceeds the {}-byte cap",
                constants::MAX_UNPACKED_BYTES
            )));
        }
    }
    Ok(out)
}

/// Unpack a tar byte stream into `dest` (already an empty temp directory the
/// caller will rename into place). Refuses anything but regular files and
/// directories, an absolute path, a `..` component, or a path repeated by a
/// second entry — each refusal is `ArtifactCorrupt`, matching
/// `tar-symlink`/`tar-hardlink`/`tar-device`/`tar-abs-path`/`tar-dotdot`/
/// `tar-duplicate-entry` in the conformance suite.
pub fn unpack_tar(data: &[u8], dest: &Path) -> Result<()> {
    let mut archive = tar::Archive::new(data);
    let mut seen = HashSet::new();
    let entries = archive
        .entries()
        .map_err(|e| Error::ArtifactCorrupt(format!("tar: {e}")))?;
    for entry in entries {
        let mut entry = entry.map_err(|e| Error::ArtifactCorrupt(format!("tar entry: {e}")))?;
        let raw_path = entry
            .path()
            .map_err(|e| Error::ArtifactCorrupt(format!("tar entry path: {e}")))?
            .into_owned();
        let rel = safe_relative_path(&raw_path)?;
        if !seen.insert(rel.clone()) {
            return Err(Error::ArtifactCorrupt(format!(
                "tar entry {} appears more than once",
                rel.display()
            )));
        }
        let header = entry.header().clone();
        if header.entry_type().is_dir() {
            std::fs::create_dir_all(dest.join(&rel))?;
            continue;
        }
        if header.entry_type().is_file() {
            let out_path = dest.join(&rel);
            if let Some(parent) = out_path.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let mut out_file = std::fs::File::create(&out_path)?;
            std::io::copy(&mut entry, &mut out_file)
                .map_err(|e| Error::ArtifactCorrupt(format!("writing {}: {e}", rel.display())))?;
            continue;
        }
        return Err(Error::ArtifactCorrupt(format!(
            "tar entry {} has type {:?}; only regular files and directories are allowed",
            rel.display(),
            header.entry_type()
        )));
    }
    Ok(())
}

/// Refuse an absolute path or one with a `..` component; return the path
/// with any leading `./`/`/` and trailing slash stripped, as the key used
/// for both the filesystem join and duplicate-entry detection.
fn safe_relative_path(p: &Path) -> Result<PathBuf> {
    if p.is_absolute() {
        return Err(Error::ArtifactCorrupt(format!(
            "tar entry {} is an absolute path",
            p.display()
        )));
    }
    let mut out = PathBuf::new();
    for comp in p.components() {
        match comp {
            Component::Normal(part) => out.push(part),
            Component::CurDir => {}
            Component::ParentDir => {
                return Err(Error::ArtifactCorrupt(format!(
                    "tar entry {} contains a '..' component",
                    p.display()
                )));
            }
            Component::RootDir | Component::Prefix(_) => {
                return Err(Error::ArtifactCorrupt(format!(
                    "tar entry {} is an absolute path",
                    p.display()
                )));
            }
        }
    }
    if out.as_os_str().is_empty() {
        return Err(Error::ArtifactCorrupt(
            "tar entry has an empty path".to_string(),
        ));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_relative_path_accepts_plain_names() {
        assert_eq!(
            safe_relative_path(Path::new("libchtypes.so")).unwrap(),
            PathBuf::from("libchtypes.so")
        );
        assert_eq!(
            safe_relative_path(Path::new("./manifest.json")).unwrap(),
            PathBuf::from("manifest.json")
        );
    }

    #[test]
    fn safe_relative_path_refuses_dotdot_and_absolute() {
        assert!(safe_relative_path(Path::new("../etc/passwd")).is_err());
        assert!(safe_relative_path(Path::new("/etc/passwd")).is_err());
        assert!(safe_relative_path(Path::new("a/../../b")).is_err());
    }

    #[test]
    fn decompress_zstd_round_trips_single_frame() {
        // A real zstd frame for b"hi" (`printf hi | zstd -q -c | xxd`), so
        // this test does not depend on any encoder being linked into this
        // crate.
        let frame: &[u8] = &[
            0x28, 0xb5, 0x2f, 0xfd, 0x04, 0x58, 0x11, 0x00, 0x00, 0x68, 0x69, 0xfa, 0x38, 0x26,
            0xea,
        ];
        let out = decompress_zstd(frame).unwrap();
        assert_eq!(out, b"hi");
    }
}
