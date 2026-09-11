//! `docs/guides/fetch.md` §3 steps 3–4: the tarball hashed before it is unpacked,
//! unpacked flat into a temporary sibling, renamed into place, and the
//! installed library re-hashed where it landed.

use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

use super::release::IndexRow;
use super::source::Source;
use super::trust::{Hashing, sha256_file};
use super::{Action, Installed};
use crate::error::{Error, Result};
use crate::registry::Manifest;

/// Download `row.file` into `dest`, hashing as it streams (step 3), unpack it
/// flat into `<dest>/.<minor>.incoming.<pid>`, rename into `<dest>/<minor>`,
/// and re-hash the installed library in place (step 4).
pub(crate) fn fetch_and_install(
    source: &Source,
    row: &IndexRow,
    dest: &Path,
    platform: &str,
    progress: bool,
) -> Result<Installed> {
    std::fs::create_dir_all(dest).map_err(|e| Error::Fetch {
        message: format!("creating {}: {e}", dest.display()),
    })?;
    let pid = std::process::id();
    let minor = &row.clickhouse_minor;
    let tarball = dest.join(format!(".{minor}.download.{pid}.tar.gz"));
    let incoming = dest.join(format!(".{minor}.incoming.{pid}"));
    let replaced = dest.join(format!(".{minor}.replaced.{pid}"));
    let cleanup = || {
        std::fs::remove_file(&tarball).ok();
        std::fs::remove_dir_all(&incoming).ok();
    };

    let result = (|| {
        download(source, row, &tarball, progress)?;
        unpack_flat(&tarball, &incoming)?;
        std::fs::remove_file(&tarball).ok();
        // Validated BEFORE it takes the registry's name: a tarball whose
        // manifest disagrees with the index never becomes `<minor>/`.
        read_manifest(&incoming, row)?;
        move_into_place(&incoming, &replaced, &dest.join(minor))
    })();
    cleanup();
    let (target, manifest, action) = match result {
        Ok((target, action)) => {
            let manifest = read_manifest(&target, row)?;
            (target, manifest, action)
        }
        Err(e) => return Err(e),
    };

    // Step 4, second half: everything so far proved the bytes were right
    // somewhere else. Re-hash the installed file, in place.
    let library = target.join(&manifest.library);
    let actual = sha256_file(&library).map_err(|e| Error::Fetch {
        message: format!("re-hashing {}: {e}", library.display()),
    })?;
    if actual != manifest.library_sha256 {
        return Err(Error::ArtifactCorrupt {
            subject: format!("installed {} (the install is bad)", library.display()),
            expected: manifest.library_sha256.clone(),
            actual,
        });
    }
    if progress {
        eprintln!(
            "chtypes: installed and verified {} (ClickHouse {}, sha256 {actual})",
            target.display(),
            manifest.clickhouse_version
        );
    }
    Ok(Installed {
        dir: target,
        line: minor.clone(),
        version: manifest.clickhouse_version.clone(),
        library,
        library_sha256: actual,
        platform: platform.to_string(),
        action,
        asset: Some((row.file.clone(), row.sha256.clone())),
    })
}

/// Step 3: stream the asset to `tarball`, hashing every byte written; the
/// hash must equal the (signed, cross-checked) sha256 or the file is deleted
/// and nothing is unpacked.
fn download(source: &Source, row: &IndexRow, tarball: &Path, progress: bool) -> Result<()> {
    let (mut reader, len) = source.open(&row.file)?.ok_or_else(|| Error::Fetch {
        message: format!(
            "{} lists {} but does not serve it",
            source.describe(),
            row.file
        ),
    })?;
    if progress {
        eprintln!(
            "chtypes: downloading {} ({} bytes) from {}",
            row.file,
            row.bytes,
            source.describe()
        );
    }
    let file = std::fs::File::create(tarball).map_err(|e| Error::Fetch {
        message: format!("creating {}: {e}", tarball.display()),
    })?;
    let mut out = Hashing::new(std::io::BufWriter::new(file));
    let total = len.unwrap_or(row.bytes);
    let mut buf = vec![0u8; 1 << 20];
    let mut next_report = total / 10;
    loop {
        let n = reader
            .read(&mut buf)
            .map_err(|e| Error::SourceUnreachable {
                origin: source.describe().to_string(),
                message: format!("reading {}: {e}", row.file),
            })?;
        if n == 0 {
            break;
        }
        out.write_all(&buf[..n]).map_err(|e| Error::Fetch {
            message: format!("writing {}: {e}", tarball.display()),
        })?;
        if progress && total > 0 && out.bytes >= next_report {
            eprintln!(
                "chtypes:   {:>3}%  {} / {} bytes",
                out.bytes * 100 / total,
                out.bytes,
                total
            );
            next_report = out.bytes + total / 10;
        }
    }
    let (writer, actual) = out.finish().map_err(|e| Error::Fetch {
        message: format!("flushing {}: {e}", tarball.display()),
    })?;
    let file = writer.into_inner().map_err(|e| Error::Fetch {
        message: format!("flushing {}: {e}", tarball.display()),
    })?;
    file.sync_all().map_err(|e| Error::Fetch {
        message: format!("flushing {}: {e}", tarball.display()),
    })?;
    if actual != row.sha256 {
        std::fs::remove_file(tarball).ok();
        return Err(Error::ArtifactCorrupt {
            subject: format!("{} (NOT unpacking it)", row.file),
            expected: row.sha256.clone(),
            actual,
        });
    }
    if progress {
        eprintln!("chtypes: sha256 verified before unpacking: {actual}");
    }
    Ok(())
}

/// Unpack a gzipped tar of a FLAT artifact directory into `dir`, refusing
/// anything that is not a plain file at the top level: no subdirectories, no
/// `..`, no absolute paths, no links, no devices. An artifact is
/// `manifest.json`, one library, `unsafe_families.txt` and `CH_VERSION`;
/// there is nothing else to extract and so nothing to be clever about.
pub(crate) fn unpack_flat(tarball: &Path, dir: &Path) -> Result<()> {
    let io = |what: String, e: std::io::Error| Error::Fetch {
        message: format!("{what}: {e}"),
    };
    std::fs::remove_dir_all(dir).ok();
    std::fs::create_dir_all(dir).map_err(|e| io(format!("creating {}", dir.display()), e))?;
    let file = std::fs::File::open(tarball)
        .map_err(|e| io(format!("opening {}", tarball.display()), e))?;
    let gz = flate2::read::GzDecoder::new(std::io::BufReader::new(file));
    let mut archive = tar::Archive::new(gz);
    archive.set_preserve_permissions(false);
    archive.set_preserve_mtime(false);
    archive.set_unpack_xattrs(false);
    let entries = archive
        .entries()
        .map_err(|e| io(format!("reading {}", tarball.display()), e))?;
    let mut extracted = 0usize;
    for entry in entries {
        let mut entry = entry.map_err(|e| io(format!("reading {}", tarball.display()), e))?;
        let path = entry
            .path()
            .map_err(|e| io("an entry's path".into(), e))?
            .into_owned();
        let name = flat_member_name(&path).ok_or_else(|| Error::Fetch {
            message: format!(
                "{} contains {:?}, which is not a plain top-level file — refusing to unpack it",
                tarball.display(),
                path.display()
            ),
        })?;
        let kind = entry.header().entry_type();
        match kind {
            // A `./` directory entry is how some tars begin; nothing to do.
            tar::EntryType::Directory if name.is_none() => continue,
            tar::EntryType::Regular | tar::EntryType::Continuous => {}
            other => {
                return Err(Error::Fetch {
                    message: format!(
                        "{} contains {:?} as a {:?} entry — an artifact holds plain files only",
                        tarball.display(),
                        path.display(),
                        other
                    ),
                });
            }
        }
        let Some(name) = name else {
            continue;
        };
        let out_path = dir.join(&name);
        let mut out = std::fs::File::create(&out_path)
            .map_err(|e| io(format!("creating {}", out_path.display()), e))?;
        std::io::copy(&mut entry, &mut out)
            .map_err(|e| io(format!("writing {}", out_path.display()), e))?;
        out.sync_all()
            .map_err(|e| io(format!("flushing {}", out_path.display()), e))?;
        // Keep the packed mode's permission bits (the library ships 0755), and
        // nothing else from the header.
        if let Ok(mode) = entry.header().mode() {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&out_path, std::fs::Permissions::from_mode(mode & 0o777)).ok();
        }
        extracted += 1;
    }
    if extracted == 0 {
        return Err(Error::Fetch {
            message: format!("{} unpacked to nothing", tarball.display()),
        });
    }
    Ok(())
}

/// `manifest.json` → `Some("manifest.json")`, `./x` → `Some("x")`, `.` →
/// `None` (nothing to write), anything else → refused (`None` is only for the
/// bare current-directory entry; the caller distinguishes by the entry type).
fn flat_member_name(path: &Path) -> Option<Option<String>> {
    let mut name: Option<String> = None;
    for c in path.components() {
        match c {
            Component::CurDir => {}
            Component::Normal(n) if name.is_none() => {
                let n = n.to_str()?;
                if n.is_empty() || n == ".." {
                    return None;
                }
                name = Some(n.to_string());
            }
            _ => return None,
        }
    }
    Some(name)
}

/// Read the manifest in an unpacked (or installed) directory and insist it
/// describes the artifact the index promised: the index is a convenience,
/// the manifest the artifact's own claim, and they must agree or the index
/// was built from a different artifact.
fn read_manifest(dir: &Path, row: &IndexRow) -> Result<Manifest> {
    let path = dir.join("manifest.json");
    let text = std::fs::read_to_string(&path).map_err(|e| Error::Fetch {
        message: format!("{} contains no usable manifest.json: {e}", row.file),
    })?;
    let manifest: Manifest = serde_json::from_str(&text).map_err(|e| Error::Fetch {
        message: format!("manifest.json inside {}: {e}", row.file),
    })?;
    if manifest.library.is_empty() || manifest.library_sha256.is_empty() {
        return Err(Error::Fetch {
            message: format!(
                "manifest.json inside {} names no library or no library_sha256",
                row.file
            ),
        });
    }
    let minor = if manifest.clickhouse_minor.is_empty() {
        super::minor_of(&manifest.clickhouse_version)
    } else {
        manifest.clickhouse_minor.clone()
    };
    if manifest.library != row.library
        || manifest.clickhouse_version != row.clickhouse_version
        || minor != row.clickhouse_minor
    {
        return Err(Error::Fetch {
            message: format!(
                "manifest.json inside {} disagrees with index.json ({}/{}/{} vs {}/{}/{})",
                row.file,
                manifest.library,
                manifest.clickhouse_version,
                minor,
                row.library,
                row.clickhouse_version,
                row.clickhouse_minor
            ),
        });
    }
    if manifest.library_sha256 != row.library_sha256 {
        return Err(Error::ArtifactCorrupt {
            subject: format!(
                "manifest.json inside {} records a different library sha256 than index.json",
                row.file
            ),
            expected: row.library_sha256.clone(),
            actual: manifest.library_sha256.clone(),
        });
    }
    if !dir.join(&manifest.library).is_file() {
        return Err(Error::Fetch {
            message: format!(
                "{} names library {} but does not contain it",
                row.file, manifest.library
            ),
        });
    }
    Ok(manifest)
}

/// The atomic step: an existing `<minor>/` moves aside, the incoming
/// directory takes its name, the old one is removed. An interrupted install
/// never leaves a half-populated `<minor>/` for a registry to `dlopen`.
fn move_into_place(incoming: &Path, replaced: &Path, target: &Path) -> Result<(PathBuf, Action)> {
    let io = |what: String, e: std::io::Error| Error::Fetch {
        message: format!("{what}: {e}"),
    };
    std::fs::remove_dir_all(replaced).ok();
    let had_previous = target.exists();
    if had_previous {
        std::fs::rename(target, replaced)
            .map_err(|e| io(format!("moving {} aside", target.display()), e))?;
    }
    if let Err(e) = std::fs::rename(incoming, target) {
        if had_previous {
            // Put the previous install back rather than leave nothing.
            std::fs::rename(replaced, target).ok();
        }
        return Err(io(format!("moving into {}", target.display()), e));
    }
    if had_previous {
        std::fs::remove_dir_all(replaced).ok();
    }
    Ok((
        target.to_path_buf(),
        if had_previous {
            Action::Replaced
        } else {
            Action::Installed
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_plain_top_level_names_are_members() {
        assert_eq!(
            flat_member_name(Path::new("manifest.json")),
            Some(Some("manifest.json".into()))
        );
        assert_eq!(
            flat_member_name(Path::new("./libchtypes.so")),
            Some(Some("libchtypes.so".into()))
        );
        assert_eq!(flat_member_name(Path::new("./")), Some(None));
        assert_eq!(flat_member_name(Path::new(".")), Some(None));
        for bad in ["../x", "a/b", "/etc/passwd", "./a/../b", "a/.."] {
            assert_eq!(flat_member_name(Path::new(bad)), None, "{bad}");
        }
    }

    #[test]
    fn a_traversing_tarball_is_refused_before_anything_is_written() {
        let dir = std::env::temp_dir().join(format!("chtypes-rs-unpack-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let tarball = dir.join("evil.tar.gz");
        {
            let gz = flate2::write::GzEncoder::new(
                std::fs::File::create(&tarball).unwrap(),
                flate2::Compression::fast(),
            );
            let mut builder = tar::Builder::new(gz);
            // The tar crate refuses to WRITE a `..` path, so the header is
            // filled in raw — exactly what a hostile archive would carry.
            let mut header = tar::Header::new_gnu();
            let name = b"../escaped";
            header.as_gnu_mut().unwrap().name[..name.len()].copy_from_slice(name);
            header.set_size(2);
            header.set_mode(0o644);
            header.set_cksum();
            builder.append(&header, &b"hi"[..]).unwrap();
            builder.into_inner().unwrap().finish().unwrap();
        }
        let out = dir.join("out");
        let err = unpack_flat(&tarball, &out).unwrap_err();
        assert!(err.to_string().contains("refusing"), "{err}");
        assert!(!dir.join("escaped").exists());
        assert!(!out.join("escaped").exists());

        // A nested member is refused as well: artifacts are flat.
        let nested = dir.join("nested.tar.gz");
        {
            let gz = flate2::write::GzEncoder::new(
                std::fs::File::create(&nested).unwrap(),
                flate2::Compression::fast(),
            );
            let mut builder = tar::Builder::new(gz);
            let mut header = tar::Header::new_gnu();
            header.set_size(2);
            header.set_mode(0o644);
            header.set_cksum();
            builder
                .append_data(&mut header, "sub/dir.txt", &b"hi"[..])
                .unwrap();
            builder.into_inner().unwrap().finish().unwrap();
        }
        let err = unpack_flat(&nested, &out).unwrap_err();
        assert!(err.to_string().contains("refusing"), "{err}");
        assert!(!out.join("sub").exists());

        // A symlink member is refused too.
        let tarball2 = dir.join("link.tar.gz");
        {
            let gz = flate2::write::GzEncoder::new(
                std::fs::File::create(&tarball2).unwrap(),
                flate2::Compression::fast(),
            );
            let mut builder = tar::Builder::new(gz);
            let mut header = tar::Header::new_gnu();
            header.set_entry_type(tar::EntryType::Symlink);
            header.set_size(0);
            header.set_mode(0o777);
            header.set_link_name("/etc/passwd").unwrap();
            header.set_cksum();
            builder
                .append_data(&mut header, "manifest.json", &b""[..])
                .unwrap();
            builder.into_inner().unwrap().finish().unwrap();
        }
        let err = unpack_flat(&tarball2, &out).unwrap_err();
        assert!(err.to_string().contains("plain files only"), "{err}");

        // A flat one unpacks, with its mode bits.
        let tarball3 = dir.join("flat.tar.gz");
        {
            let gz = flate2::write::GzEncoder::new(
                std::fs::File::create(&tarball3).unwrap(),
                flate2::Compression::fast(),
            );
            let mut builder = tar::Builder::new(gz);
            let mut header = tar::Header::new_gnu();
            header.set_size(3);
            header.set_mode(0o755);
            header.set_cksum();
            builder
                .append_data(&mut header, "./lib.so", &b"abc"[..])
                .unwrap();
            builder.into_inner().unwrap().finish().unwrap();
        }
        unpack_flat(&tarball3, &out).unwrap();
        assert_eq!(std::fs::read(out.join("lib.so")).unwrap(), b"abc");
        std::fs::remove_dir_all(&dir).ok();
    }
}
