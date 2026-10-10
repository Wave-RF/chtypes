//! `chtypes prune` (docs/guides/fetch-v1.md §1, "Pruning"; public issue
//! #494): remove the installed builds of a line that newer installed builds of
//! the same line and platform supersede, keeping the newest N, and never one a
//! live process holds ([`super::hold`]). It reads and writes the cache only,
//! never a system directory, and never another fingerprint's records (§3: the
//! dev channel's own-fingerprint rule).

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use super::channel;
use super::constants;
use super::ensure::{Options, resources};
use super::error::{Error, Result, unwritable};
use super::faults;
use super::hold::{Claim, claim};
use super::layout::{self, VerifiedRecord};

/// Whether `s` is a two-part line spelling, such as `26.8`:
/// `^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`.
pub fn is_line(s: &str) -> bool {
    let part = |p: &str| {
        !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()) && (p == "0" || !p.starts_with('0'))
    };
    match s.split_once('.') {
        Some((major, minor)) => part(major) && part(minor),
        None => false,
    }
}

/// The two-part line of a four-part version: `26.8` for `26.8.15.10`, or the
/// empty string for anything shorter.
pub fn line_of(version: &str) -> String {
    let mut parts = version.split('.');
    match (parts.next(), parts.next()) {
        (Some(major), Some(minor)) => format!("{major}.{minor}"),
        _ => String::new(),
    }
}

/// One installed build a prune found superseded: removed (or, in a dry run,
/// to be removed), or kept because it is in use.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Superseded {
    pub platform: String,
    pub version: String,
    pub build: String,
    /// The entry's own name: `sha256:<hex>` of `unpacked/sha256/<hex>`.
    pub manifest: String,
    pub dir: PathBuf,
    /// The build was kept: a live process holds it (or its record cannot be
    /// locked at all, so nothing can tell).
    pub in_use: bool,
}

/// One installed entry: its directory and its record.
type Entry = (PathBuf, VerifiedRecord);

/// One entry moved aside, to be deleted with its blobs.
struct Removal {
    record: VerifiedRecord,
    manifest: String,
    aside: PathBuf,
}

/// Remove, from the cache, every installed build that at least `keep` newer
/// installed builds of its own line and platform supersede, oldest first, and
/// report each. Builds are ordered by (version, build), and the report by
/// platform (the constants' order), then oldest first. A superseded build a
/// live process holds is reported `in_use` and kept. For each build it moves
/// the unpacked entry with its record aside, then drops its `index.json`
/// entries and its blobs (the manifest, its config and layer, and every
/// referrer manifest of it the layout holds, with that referrer's layers),
/// never a blob a remaining build names. `line` limits it to one line;
/// `dry_run` decides and reports exactly what would go, and removes nothing.
///
/// A `keep` below 1 or a `line` that is not a two-part line is the caller's
/// misuse (`InvalidInput`); the CLI refuses both first.
pub fn prune(
    mut options: Options,
    line: Option<&str>,
    keep: usize,
    dry_run: bool,
) -> Result<Vec<Superseded>> {
    if keep < 1 {
        return Err(Error::InvalidInput(format!(
            "--keep is at least 1 (the newest build of a line is never superseded), not {keep}"
        )));
    }
    if let Some(line) = line.filter(|l| !is_line(l)) {
        return Err(Error::InvalidInput(format!(
            "--line takes a two-part line such as 26.8, not {line:?}"
        )));
    }
    // Every platform's builds are pruned, so the host's own is never needed;
    // the shared setup still wants one known key.
    if options.platform.is_none() {
        options.platform = constants::PLATFORMS.first().map(|p| p.key.to_string());
    }
    let res = resources(&mut options)?;
    // Only the cache is pruned: a system directory is read-only. An unreadable
    // cache holds nothing this call can prune; the default mode's warning
    // names it (`missing_notes`), and strict mode refuses it here.
    faults::probe_roots(&[res.root.as_path()], res.strict)?;
    let entries = layout::list_verified(&res.root).unwrap_or_default();

    let mut groups: BTreeMap<(String, String), Vec<Entry>> = BTreeMap::new();
    for (dir, record) in entries {
        if !channel::visible(&record.predicate) {
            continue; // another fingerprint's build: its own SDK keeps or prunes it
        }
        let entry_line = line_of(&record.version);
        if line.is_some_and(|l| l != entry_line) {
            continue;
        }
        groups
            .entry((record.platform.clone(), entry_line))
            .or_default()
            .push((dir, record));
    }
    let mut superseded: Vec<Entry> = Vec::new();
    for (_, mut members) in groups {
        // Newest first: the first `keep` stay.
        members.sort_by(|a, b| entry_age(b, a));
        if members.len() > keep {
            superseded.extend(members.drain(keep..));
        }
    }
    superseded.sort_by(|a, b| {
        platform_rank(&a.1.platform)
            .cmp(&platform_rank(&b.1.platform))
            .then_with(|| entry_age(a, b))
    });

    let unpacked_root = res.root.join(constants::CACHE_UNPACKED_DIR);
    let mut out = Vec::new();
    let mut removed: Vec<Removal> = Vec::new();
    for (dir, record) in superseded {
        let manifest = entry_manifest(&dir);
        let mut reported = Superseded {
            platform: record.platform.clone(),
            version: record.version.clone(),
            build: record.build.clone(),
            manifest: manifest.clone(),
            dir: dir.clone(),
            in_use: false,
        };
        let owned = match claim(&dir) {
            // Another prune removed it, or an install replaced it.
            Claim::Gone => continue,
            Claim::InUse => {
                reported.in_use = true;
                out.push(reported);
                continue;
            }
            Claim::Owned(owned) => owned,
        };
        if dry_run {
            drop(owned);
            out.push(reported);
            continue;
        }
        // Moved aside while the exclusive lock is held: once it drops, no
        // lookup lists the entry, and a hold taken on it finds it gone.
        let aside = layout::create_unique_dir(&unpacked_root, ".prune-")?;
        let moved_to = aside.join("entry");
        let moved = std::fs::rename(&dir, &moved_to);
        if moved.is_err() {
            let _ = std::fs::remove_dir(&aside);
        }
        drop(owned);
        moved.map_err(unwritable(&moved_to))?;
        removed.push(Removal {
            record,
            manifest,
            aside,
        });
        out.push(reported);
    }
    if removed.is_empty() {
        return Ok(out);
    }

    let doomed: BTreeSet<String> = removed.iter().map(|r| r.manifest.clone()).collect();
    let mut blobs = blobs_of(&res.root, &doomed);
    for r in &removed {
        let named = [
            Some(&r.record.layer_digest),
            r.record.bundle_digest.as_ref(),
            r.record.bundle_manifest_digest.as_ref(),
        ];
        for digest in named.into_iter().flatten() {
            if layout::is_digest(digest) {
                blobs.insert(digest.clone());
            }
        }
    }
    // Never a blob a remaining build names, whatever its fingerprint.
    for (dir, record) in layout::list_verified(&res.root).unwrap_or_default() {
        for digest in own_blobs(&res.root, &entry_manifest(&dir)) {
            blobs.remove(&digest);
        }
        let named = [
            Some(&record.manifest_digest),
            Some(&record.layer_digest),
            record.bundle_digest.as_ref(),
            record.bundle_manifest_digest.as_ref(),
        ];
        for digest in named.into_iter().flatten() {
            blobs.remove(digest);
        }
    }
    remove_index_entries(&res.root, &blobs)?;
    for digest in &blobs {
        let path = layout::blob_path(&res.root, digest)?;
        match std::fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == ErrorKind::NotFound => {}
            Err(e) => return Err(unwritable(&path)(e)),
        }
    }
    for r in &removed {
        std::fs::remove_dir_all(&r.aside).map_err(unwritable(&r.aside))?;
    }
    Ok(out)
}

/// An installed entry's own manifest digest: its directory's name.
fn entry_manifest(dir: &Path) -> String {
    let name = dir
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    format!("sha256:{name}")
}

/// Installed builds by age: (version, build), then the manifest digest, so the
/// order is total. `Less` is older.
fn entry_age(a: &Entry, b: &Entry) -> Ordering {
    version_cmp(&a.1.version, &b.1.version)
        .then_with(|| a.1.build.cmp(&b.1.build))
        .then_with(|| entry_manifest(&a.0).cmp(&entry_manifest(&b.0)))
}

/// Versions compare component by component: numerically where both
/// components are numbers, else as strings; a version that is a prefix of the
/// other is the older.
fn version_cmp(a: &str, b: &str) -> Ordering {
    let (mut xs, mut ys) = (a.split('.'), b.split('.'));
    loop {
        match (xs.next(), ys.next()) {
            (Some(x), Some(y)) if x == y => {}
            (Some(x), Some(y)) => {
                return match (x.parse::<u64>(), y.parse::<u64>()) {
                    (Ok(m), Ok(n)) if all_digits(x) && all_digits(y) => m.cmp(&n),
                    _ => x.cmp(y),
                };
            }
            (None, Some(_)) => return Ordering::Less,
            (Some(_), None) => return Ordering::Greater,
            (None, None) => return Ordering::Equal,
        }
    }
}

fn all_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// A platform key's place in the constants' platform list.
fn platform_rank(key: &str) -> usize {
    constants::PLATFORMS
        .iter()
        .position(|p| p.key == key)
        .unwrap_or(constants::PLATFORMS.len())
}

/// The blobs a manifest names itself: the manifest, its config and its layers
/// (only the manifest when its blob is absent or not a JSON document).
fn own_blobs(root: &Path, manifest: &str) -> Vec<String> {
    let mut out = vec![manifest.to_string()];
    let Ok(Some(bytes)) = layout::read_blob(root, manifest) else {
        return out;
    };
    let Ok(doc) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return out;
    };
    if let Some(config) = doc["config"]["digest"]
        .as_str()
        .filter(|d| layout::is_digest(d))
    {
        out.push(config.to_string());
    }
    out.extend(layer_digests(&doc));
    out
}

/// A manifest document's valid layer digests.
fn layer_digests(doc: &serde_json::Value) -> Vec<String> {
    doc["layers"]
        .as_array()
        .map(|layers| {
            layers
                .iter()
                .filter_map(|l| l["digest"].as_str())
                .filter(|d| layout::is_digest(d))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// The blobs a prune removes with `manifests`: each manifest's own
/// ([`own_blobs`]), and every referrer manifest the layout holds whose subject
/// is one of them (a signature, a goldens document), with that referrer's
/// layers. A referrer's config, the empty `{}` every referrer shares, is never
/// among them.
fn blobs_of(root: &Path, manifests: &BTreeSet<String>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for manifest in manifests {
        out.extend(own_blobs(root, manifest));
    }
    let dir = root.join("blobs").join("sha256");
    let Ok(names) = std::fs::read_dir(&dir) else {
        return out;
    };
    for entry in names.flatten() {
        let Ok(name) = entry.file_name().into_string() else {
            continue;
        };
        if !layout::is_hex64(&name) {
            continue;
        }
        let Ok(meta) = entry.metadata() else {
            continue;
        };
        if meta.is_dir() || meta.len() > constants::MANIFEST_MAX_BYTES {
            continue;
        }
        let Ok(bytes) = std::fs::read(entry.path()) else {
            continue;
        };
        let Ok(doc) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
            continue;
        };
        let Some(subject) = doc["subject"]["digest"].as_str() else {
            continue;
        };
        if !manifests.contains(subject) {
            continue;
        }
        out.insert(format!("sha256:{name}"));
        out.extend(layer_digests(&doc));
    }
    out
}

/// Drop every `index.json` entry whose digest is in `dropped`, by the same
/// read-check-rename loop [`layout::record_in_index`] uses, so an entry another
/// process adds meanwhile survives. An absent or unparsable `index.json` is
/// left as it is, and so is one that lists none of them.
fn remove_index_entries(root: &Path, dropped: &BTreeSet<String>) -> Result<()> {
    let path = root.join("index.json");
    for _ in 0..16 {
        let before = match std::fs::read(&path) {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(unwritable(&path)(e)),
        };
        let Ok(mut value) = serde_json::from_slice::<serde_json::Value>(&before) else {
            return Ok(());
        };
        let Some(manifests) = value["manifests"].as_array() else {
            return Ok(());
        };
        let kept: Vec<serde_json::Value> = manifests
            .iter()
            .filter(|d| {
                !d["digest"]
                    .as_str()
                    .is_some_and(|digest| dropped.contains(digest))
            })
            .cloned()
            .collect();
        if kept.len() == manifests.len() {
            return Ok(());
        }
        value["manifests"] = serde_json::Value::Array(kept);
        let now = std::fs::read(&path).ok();
        if now.as_deref() != Some(before.as_slice()) {
            // Another process wrote it meanwhile: drop from theirs.
            continue;
        }
        return layout::write_atomic(&path, serde_json::to_vec(&value)?.as_slice());
    }
    Err(Error::SourceUnreachable(format!(
        "{} kept changing under this writer; gave up after 16 attempts",
        path.display()
    )))
}

/// The prune over hand-written records, under the v1 contract (a fixture cache
/// used whole): the grouping, the order, the dry run, a held build kept, and
/// the blobs and index entries that go with a removed build. The conformance
/// cases (`prune-*`) hold every binding to the same rules over signed layouts.
#[cfg(test)]
mod tests {
    use super::super::{hold, oci};
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        use std::time::{SystemTime, UNIX_EPOCH};
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        std::env::temp_dir().join(format!(
            "ocifetch-prune-{name}-{}-{nanos:x}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ))
    }

    /// One record installed by hand under `root`, with its manifest blob (a
    /// config and a layer) and an `index.json` entry; returns the entry
    /// directory.
    fn install(root: &Path, platform: &str, version: &str, build: &str) -> PathBuf {
        let blob = |bytes: &[u8]| {
            let digest = format!("sha256:{}", oci::sha256_hex(bytes));
            layout::write_atomic(&layout::blob_path(root, &digest).unwrap(), bytes).unwrap();
            digest
        };
        let config = blob(format!("config {platform} {version} {build}").as_bytes());
        let layer = blob(format!("layer {platform} {version} {build}").as_bytes());
        let manifest_bytes = serde_json::to_vec(&serde_json::json!({
            "config": {"digest": config},
            "layers": [{"digest": layer}],
        }))
        .unwrap();
        let manifest = blob(manifest_bytes.as_slice());
        layout::record_in_index(
            root,
            &manifest,
            manifest_bytes.len() as u64,
            constants::MEDIA_TYPE_MANIFEST,
            None,
            None,
        )
        .unwrap();
        let library = format!("library {platform} {version} {build}");
        let record = VerifiedRecord {
            platform: platform.to_string(),
            version: version.to_string(),
            build: build.to_string(),
            channel: None,
            index_digest: None,
            manifest_digest: manifest.clone(),
            layer_digest: layer,
            bundle_digest: None,
            bundle_manifest_digest: None,
            signed_by: String::new(),
            library: "lib.so".to_string(),
            library_sha256: oci::sha256_hex(library.as_bytes()),
            library_bytes: library.len() as u64,
            predicate: serde_json::json!({"clickhouse_version": version, "build": build}),
        };
        let entry = layout::unpacked_dir(root, &manifest).unwrap();
        layout::write_atomic(&entry.join("lib.so"), library.as_bytes()).unwrap();
        layout::write_atomic(
            &entry.join(constants::CACHE_VERIFIED_RECORD),
            &record.to_json_bytes().unwrap(),
        )
        .unwrap();
        entry
    }

    fn options(cache: &Path) -> Options {
        Options {
            cache_dir: Some(cache.to_string_lossy().into_owned()),
            system_dirs: Vec::new(),
            ..Options::default()
        }
    }

    fn manifests(got: &[Superseded]) -> Vec<(String, bool)> {
        got.iter().map(|s| (s.manifest.clone(), s.in_use)).collect()
    }

    fn index_digests(root: &Path) -> Vec<String> {
        let doc: serde_json::Value =
            serde_json::from_slice(&std::fs::read(root.join("index.json")).unwrap()).unwrap();
        doc["manifests"]
            .as_array()
            .unwrap()
            .iter()
            .map(|d| d["digest"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn lines_are_two_numeric_parts() {
        for good in ["26.8", "0.0", "26.10"] {
            assert!(is_line(good), "{good}");
        }
        for bad in [
            "26", "26.8.15", "v26.8", "26.08", "026.8", "26.", ".8", "26.x", "",
        ] {
            assert!(!is_line(bad), "{bad}");
        }
        assert_eq!(line_of("26.8.15.10"), "26.8");
        assert_eq!(line_of("26"), "");
        assert_eq!(version_cmp("26.10.1.1", "26.9.3.38"), Ordering::Greater);
        assert_eq!(version_cmp("26.8.1.1", "26.8.1.1"), Ordering::Equal);
    }

    #[test]
    fn misuse_is_refused_before_anything_is_read() {
        let _v1 = channel::use_fetch_v1_for_tests();
        let cache = scratch("misuse");
        assert!(matches!(
            prune(options(&cache), None, 0, false),
            Err(Error::InvalidInput(_))
        ));
        assert!(matches!(
            prune(options(&cache), Some("26.8.15"), 1, false),
            Err(Error::InvalidInput(_))
        ));
        assert!(prune(options(&cache), None, 1, false).unwrap().is_empty());
        assert!(
            !cache.exists(),
            "a prune of nothing made {}",
            cache.display()
        );
    }

    /// Per platform and line, the newest `keep` stay; the report is in the
    /// constants' platform order, then oldest first; a dry run removes nothing;
    /// a held build is reported in use and kept; and a removed build takes its
    /// entry, its blobs and its index entry with it, and nothing a remaining
    /// build names.
    #[test]
    fn superseded_builds_go_oldest_first_and_a_held_one_stays() {
        let _v1 = channel::use_fetch_v1_for_tests();
        let root = scratch("lines");
        let amd_old = install(&root, "linux-amd64", "26.8.14.1", "20260801.000000");
        let amd_new = install(&root, "linux-amd64", "26.8.15.2", "20260901.000000");
        let arm_oldest = install(&root, "linux-arm64", "26.8.9.1", "20260701.000000");
        let arm_old = install(&root, "linux-arm64", "26.8.14.1", "20260801.000000");
        let arm_new = install(&root, "linux-arm64", "26.8.14.1", "20260802.000000");
        let arm_other_line = install(&root, "linux-arm64", "26.9.1.1", "20260901.000000");

        let dry = prune(options(&root), None, 1, true).unwrap();
        assert_eq!(
            manifests(&dry),
            vec![
                (entry_manifest(&amd_old), false),
                (entry_manifest(&arm_oldest), false),
                (entry_manifest(&arm_old), false),
            ],
            "the report order: {dry:?}"
        );
        assert!(arm_oldest.exists() && arm_old.exists() && amd_old.exists());
        assert!(
            prune(options(&root), Some("26.9"), 1, false)
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            manifests(&prune(options(&root), None, 2, true).unwrap()),
            vec![(entry_manifest(&arm_oldest), false)]
        );

        // A build this process holds is in use, and kept.
        assert_eq!(hold::hold(&arm_old), hold::HoldState::Held);
        let before = index_digests(&root);
        let got = prune(options(&root), Some("26.8"), 1, false).unwrap();
        assert_eq!(
            manifests(&got),
            vec![
                (entry_manifest(&amd_old), false),
                (entry_manifest(&arm_oldest), false),
                (entry_manifest(&arm_old), true),
            ]
        );
        assert!(!arm_oldest.exists() && !amd_old.exists());
        for kept in [&arm_old, &arm_new, &amd_new, &arm_other_line] {
            assert!(
                kept.join(constants::CACHE_VERIFIED_RECORD).is_file(),
                "{} was removed",
                kept.display()
            );
        }
        let gone = [entry_manifest(&arm_oldest), entry_manifest(&amd_old)];
        let want: Vec<String> = before.into_iter().filter(|d| !gone.contains(d)).collect();
        assert_eq!(index_digests(&root), want);
        for digest in &gone {
            assert!(
                !layout::blob_path(&root, digest).unwrap().exists(),
                "{digest}"
            );
        }
        let leftovers: Vec<String> = std::fs::read_dir(root.join(constants::CACHE_UNPACKED_DIR))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with(".prune-"))
            .collect();
        assert!(
            leftovers.is_empty(),
            "aside directories left: {leftovers:?}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
