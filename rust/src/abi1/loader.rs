//! The ABI v1 loader: plan §3.2's steps 1-7, hand-written (nothing here is
//! generated; `decls.rs` is the generated layer this calls into).
//!
//! # Safety invariants this module owns
//!
//! * **Steps run in order, exactly as §3.2**: glibc (Linux only, before
//!   `dlopen`), `dlopen(RTLD_NOW | RTLD_LOCAL)`, `chs_abi_version`,
//!   `chs_build_info`'s fingerprint, the nine-field cross-check, then (and
//!   only then) resolve every remaining symbol.
//! * **No `dlclose`, ever** (D2). The opened library is moved into
//!   [`decls::Api`]'s `ManuallyDrop<Library>` field once step 6 succeeds, and
//!   on any earlier refusal the (unused) `Library` is also never closed —
//!   `libloading`'s `Library` has no `Drop` impl that calls `dlclose` unless
//!   its owner is dropped, and this module never drops one; see
//!   `rust/src/ffi.rs`'s identical v0 rule for why (a leaked `Context`'s
//!   destructor order).
//! * **`open_unverified`** (plan §3.1) is NOT implemented here: wave B has no
//!   caller for it (core's own local-build path and the linked mode are
//!   wave C's concern), and the two-gate rule (`unverified: true` plus
//!   `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1`) belongs with whichever caller
//!   first needs it, so it is not invented speculatively here.
//! * **Errors stay abi1-local until wave C** (the common brief): [`Refusal`]
//!   carries the same fields `sdk.json`'s loader-refusal table promises
//!   (`reason`, `path`, `want`/`got`), but is not wired to this crate's
//!   public `Error` enum — that mapping lands with the public API.

use std::ffi::CStr;
use std::path::{Path, PathBuf};

#[cfg(target_os = "linux")]
use libloading::os::unix::Symbol;
use libloading::os::unix::{Library as UnixLibrary, RTLD_LOCAL, RTLD_NOW};

use super::decls::{self, Api, CrossCheckKind, Handshake};

/// One loader refusal: the `sdk.json` reason WORD, EXACTLY (`glibc_floor`,
/// `no_glibc`, `predicate_malformed`, `dlopen`, `not_v1`, `abi_version`,
/// `build_info_malformed`, `fingerprint`, `build_info_mismatch:<field>` or
/// `missing_symbol:<name>` — never a sentence built around one), the library
/// path, and — where applicable — what was wanted, what was found, and any
/// free-form detail. `reason` is what a conformance case compares against
/// `scripts/abi-v1/emit/_stubshared.py`'s variant plan verbatim; it is never
/// decorated, so that comparison can be a plain string equality.
#[derive(Debug, Clone)]
pub(crate) struct Refusal {
    pub(crate) reason: String,
    pub(crate) path: PathBuf,
    pub(crate) want: Option<String>,
    pub(crate) got: Option<String>,
    pub(crate) detail: Option<String>,
}

impl Refusal {
    fn new(reason: impl Into<String>, path: &Path) -> Refusal {
        Refusal {
            reason: reason.into(),
            path: path.to_path_buf(),
            want: None,
            got: None,
            detail: None,
        }
    }

    fn with_detail(reason: impl Into<String>, path: &Path, detail: impl Into<String>) -> Refusal {
        Refusal {
            reason: reason.into(),
            path: path.to_path_buf(),
            want: None,
            got: None,
            detail: Some(detail.into()),
        }
    }

    fn naming(
        reason: impl Into<String>,
        path: &Path,
        want: impl Into<String>,
        got: impl Into<String>,
    ) -> Refusal {
        Refusal {
            reason: reason.into(),
            path: path.to_path_buf(),
            want: Some(want.into()),
            got: Some(got.into()),
            detail: None,
        }
    }
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.path.display(), self.reason)?;
        if let (Some(want), Some(got)) = (&self.want, &self.got) {
            write!(f, " (want {want}, got {got})")?;
        }
        if let Some(detail) = &self.detail {
            write!(f, ": {detail}")?;
        }
        Ok(())
    }
}

/// The loader's input (plan §3.1): decoupled from the fetch module's own
/// `Resolved` type so this lane needs no fetch binding PR to merge first. The
/// wave-C adapter is a three-line `Resolved -> LoadInput`.
pub(crate) struct LoadInput<'a> {
    pub(crate) library_path: &'a Path,
    /// The verified predicate, as a parsed JSON object — passed verbatim,
    /// never re-derived.
    pub(crate) predicate: serde_json::Map<String, serde_json::Value>,
}

/// A library that passed every check (steps 1-6): its resolved symbol table.
pub(crate) struct Loaded {
    pub(crate) api: Api,
}

/// Run loader steps 1-6 against `input`. Step 7 (`on_loaded`) is a no-op
/// hook: A5 (init vs. per-call timezone) is unsettled, so nothing is built
/// for it (plan §3.2).
pub(crate) fn load(input: LoadInput<'_>) -> Result<Loaded, Refusal> {
    let path = input.library_path;

    // Step 1: glibc, Linux only, BEFORE dlopen.
    #[cfg(target_os = "linux")]
    check_glibc(&input.predicate, path)?;

    // Step 2: dlopen(RTLD_NOW | RTLD_LOCAL).
    // SAFETY: the artifact is a self-contained chtypes v1 library (exports
    // only chs_*), the same posture `rust/src/ffi.rs`'s v0 loader documents;
    // RTLD_NOW is required by D2/D3 (no lazy binding, so an unresolved
    // reference fails HERE, not on first use — the "unbound" stub variant's
    // whole point) and RTLD_LOCAL keeps two loaded versions from colliding.
    let lib = unsafe { UnixLibrary::open(Some(path), RTLD_NOW | RTLD_LOCAL) }
        .map_err(|e| Refusal::with_detail("dlopen", path, e.to_string()))?;

    // Step 3: chs_abi_version.
    // SAFETY: `lib` was just opened above and is never dlclose'd (this
    // function either returns it inside `Api` or lets it leak, never drops
    // it), so a `Handshake` resolved from it stays valid for the process's
    // life, which is all `Handshake::resolve`'s own contract requires.
    let handshake: Handshake =
        unsafe { Handshake::resolve(&lib) }.map_err(|_| Refusal::new("not_v1", path))?;
    // SAFETY: chs_abi_version takes no arguments and is documented callable
    // before any other check (D1.2, the handshake class).
    let version = unsafe { (handshake.chs_abi_version)() };
    if version != decls::CHS_ABI_VERSION {
        return Err(Refusal::naming(
            "abi_version",
            path,
            decls::CHS_ABI_VERSION.to_string(),
            version.to_string(),
        ));
    }

    // Step 4: chs_build_info — parsed strictly, then the fingerprint
    // byte-compared against the compiled-in constant.
    // SAFETY: same handshake contract as chs_abi_version above.
    let raw = unsafe { (handshake.chs_build_info)() };
    if raw.is_null() {
        return Err(Refusal::with_detail(
            "build_info_malformed",
            path,
            "chs_build_info returned NULL",
        ));
    }
    // SAFETY: a NULL check was just performed; chs_build_info's documented
    // contract is a NUL-terminated string valid for the process's life
    // (never freed by the caller).
    let bytes = unsafe { CStr::from_ptr(raw) }.to_bytes();
    let build_info = parse_build_info(bytes, path)?;
    let schema = build_info.get("schema").and_then(serde_json::Value::as_i64);
    if schema != Some(1) {
        return Err(Refusal::with_detail(
            "build_info_malformed",
            path,
            format!("schema is {schema:?}, want 1"),
        ));
    }
    let fingerprint = build_info
        .get("abi_fingerprint")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| {
            Refusal::with_detail(
                "build_info_malformed",
                path,
                "abi_fingerprint is missing or not a string",
            )
        })?;
    if fingerprint.as_bytes() != decls::CHS_ABI_FINGERPRINT.as_bytes() {
        return Err(Refusal::naming(
            "fingerprint",
            path,
            decls::CHS_ABI_FINGERPRINT,
            fingerprint,
        ));
    }

    // Step 5: the nine-field cross-check against the verified predicate.
    for (bi_field, pred_field, kind) in decls::CROSS_CHECK_FIELDS {
        let bi_value = build_info.get(*bi_field).ok_or_else(|| {
            Refusal::with_detail("build_info_malformed", path, format!("missing {bi_field}"))
        })?;
        let pred_value = input.predicate.get(*pred_field).ok_or_else(|| {
            Refusal::with_detail("predicate_malformed", path, format!("missing {pred_field}"))
        })?;
        let equal = match kind {
            CrossCheckKind::Int => {
                bi_value.as_i64().is_some() && bi_value.as_i64() == pred_value.as_i64()
            }
            CrossCheckKind::Bytes => {
                bi_value.as_str().is_some() && bi_value.as_str() == pred_value.as_str()
            }
        };
        if !equal {
            return Err(Refusal::naming(
                format!("build_info_mismatch:{bi_field}"),
                path,
                pred_value.to_string(),
                bi_value.to_string(),
            ));
        }
    }

    // Step 6: resolve every described symbol (api, tooling, the tombstone),
    // for presence. Only now does `lib` move into `Api`'s own `ManuallyDrop`.
    // SAFETY: steps 1-5 passed, and `lib` is never dlclose'd (D2).
    // `sdk.json`'s step-6 refusal carries the missing name as a SUFFIX on the
    // reason itself (`missing_symbol:<name>`), the same shape
    // `_stubshared.py`'s `missing-<sym>` variants are named for — never a
    // separate want/got pair.
    let api = unsafe { Api::resolve_all(lib) }
        .map_err(|name| Refusal::new(format!("missing_symbol:{name}"), path))?;

    Ok(Loaded { api })
}

/// `chs_build_info()`'s text, parsed strictly (plan §3.2 step 4): ASCII
/// only, valid JSON, a top-level object, and no duplicate key — each refused
/// with the single `build_info_malformed` reason `sdk.json` assigns every
/// step-4 malformation.
fn parse_build_info(
    bytes: &[u8],
    path: &Path,
) -> Result<serde_json::Map<String, serde_json::Value>, Refusal> {
    if !bytes.is_ascii() {
        return Err(Refusal::with_detail(
            "build_info_malformed",
            path,
            "not ASCII",
        ));
    }
    let DupCheckedObject(map) = serde_json::from_slice(bytes)
        .map_err(|e| Refusal::with_detail("build_info_malformed", path, e.to_string()))?;
    for name in decls::BUILD_INFO_REQUIRED {
        if !map.contains_key(*name) {
            return Err(Refusal::with_detail(
                "build_info_malformed",
                path,
                format!("missing {name}"),
            ));
        }
    }
    Ok(map)
}

/// A JSON object, deserialized with an explicit duplicate-key check —
/// `serde_json`'s ordinary `Map`/`Value` deserialization lets a later key
/// silently win, which is exactly what `sdk.json`'s `build-info-dup-key`
/// stub variant exists to catch (plan §3.2 step 4: "duplicate keys
/// refused").
struct DupCheckedObject(serde_json::Map<String, serde_json::Value>);

impl<'de> serde::de::Deserialize<'de> for DupCheckedObject {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::de::Deserializer<'de>,
    {
        struct Visitor;
        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value = DupCheckedObject;

            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a JSON object with no duplicate key")
            }

            fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
            where
                A: serde::de::MapAccess<'de>,
            {
                let mut out = serde_json::Map::new();
                while let Some((key, value)) = map.next_entry::<String, serde_json::Value>()? {
                    if out.contains_key(&key) {
                        return Err(serde::de::Error::custom(format!(
                            "duplicate object key {key:?}"
                        )));
                    }
                    out.insert(key, value);
                }
                Ok(DupCheckedObject(out))
            }
        }
        deserializer.deserialize_map(Visitor)
    }
}

/// Loader step 1 (Linux only, before `dlopen`): resolve `gnu_get_libc_version`
/// dynamically from the PROCESS image (`Library::this()`, never from the
/// artifact being loaded) and compare it, as dotted integers, against the
/// predicate's `glibc_floor`. Absent entirely, or resolvable only through
/// musl's non-GNU libc, refuses `no_glibc`; a predicate with no
/// `glibc_floor` on Linux refuses `predicate_malformed`.
#[cfg(target_os = "linux")]
fn check_glibc(
    predicate: &serde_json::Map<String, serde_json::Value>,
    path: &Path,
) -> Result<(), Refusal> {
    let floor = predicate
        .get("glibc_floor")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| {
            Refusal::with_detail(
                "predicate_malformed",
                path,
                "a linux predicate has no glibc_floor",
            )
        })?;

    // `Library::this()` is a safe wrapper over the already-loaded process
    // image (no new library is opened, so D2's dlopen-flag rule does not
    // apply here).
    let this = UnixLibrary::this();
    // SAFETY: the symbol below is called with the exact zero-argument,
    // `const char *`-returning signature glibc documents for
    // `gnu_get_libc_version`.
    let version_fn: Symbol<decls::FnGlibcVersion> =
        match unsafe { this.get(b"gnu_get_libc_version\0") } {
            Ok(f) => f,
            Err(_) => return Err(Refusal::new("no_glibc", path)),
        };
    // SAFETY: the symbol above resolved against the documented signature.
    let version_ptr = unsafe { version_fn() };
    if version_ptr.is_null() {
        return Err(Refusal::with_detail(
            "no_glibc",
            path,
            "gnu_get_libc_version returned NULL",
        ));
    }
    // SAFETY: glibc documents a NUL-terminated string valid for the
    // process's life (a compiled-in constant, never freed by the caller).
    let version = unsafe { CStr::from_ptr(version_ptr) }
        .to_string_lossy()
        .into_owned();
    if dotted_version_cmp(&version, floor) == std::cmp::Ordering::Less {
        return Err(Refusal::naming("glibc_floor", path, floor, version));
    }
    Ok(())
}

/// Compare two dotted-integer version strings (`"2.29"` vs `"2.17"`)
/// component-wise; a missing trailing component reads as `0`, and a
/// non-numeric component sorts as `0` too (never a panic — a malformed
/// floor or a libc that spells its version oddly fails the comparison
/// honestly rather than crashing the loader).
fn dotted_version_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    let mut ai = a.split('.').map(|p| p.parse::<u64>().unwrap_or(0));
    let mut bi = b.split('.').map(|p| p.parse::<u64>().unwrap_or(0));
    loop {
        match (ai.next(), bi.next()) {
            (None, None) => return std::cmp::Ordering::Equal,
            (x, y) => {
                let ord = x.unwrap_or(0).cmp(&y.unwrap_or(0));
                if ord != std::cmp::Ordering::Equal {
                    return ord;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dotted_version_cmp_orders_components_numerically_not_lexically() {
        assert_eq!(dotted_version_cmp("2.9", "2.17"), std::cmp::Ordering::Less);
        assert_eq!(
            dotted_version_cmp("2.29", "2.17"),
            std::cmp::Ordering::Greater
        );
        assert_eq!(
            dotted_version_cmp("2.17", "2.17"),
            std::cmp::Ordering::Equal
        );
        assert_eq!(
            dotted_version_cmp("2.17.1", "2.17"),
            std::cmp::Ordering::Greater
        );
        assert_eq!(dotted_version_cmp("2", "2.0.0"), std::cmp::Ordering::Equal);
    }

    #[test]
    fn parse_build_info_refuses_a_duplicate_key() {
        let p = Path::new("/dev/null");
        let err = parse_build_info(br#"{"schema":1,"schema":1}"#, p).unwrap_err();
        assert_eq!(err.reason, "build_info_malformed");
        assert!(
            err.detail
                .as_deref()
                .unwrap_or_default()
                .contains("duplicate"),
            "{err}"
        );
    }

    #[test]
    fn parse_build_info_refuses_non_ascii() {
        let p = Path::new("/dev/null");
        let err = parse_build_info(b"{\"schema\":1,\"x\":\"\xc3\x28\"}", p).unwrap_err();
        assert_eq!(err.reason, "build_info_malformed");
        assert_eq!(err.detail.as_deref(), Some("not ASCII"));
    }

    #[test]
    fn parse_build_info_refuses_bad_json() {
        let p = Path::new("/dev/null");
        let err = parse_build_info(b"{not json", p).unwrap_err();
        assert_eq!(err.reason, "build_info_malformed");
    }
}
