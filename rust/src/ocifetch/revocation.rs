//! Build-level revocation (docs/guides/fetch-v1.md, "Build-level revocation";
//! public issue #625). A revocation is a signed referrer of a platform
//! manifest, read from the listing its signature lookup reads; one that
//! verifies refuses the build with `CHTYPES_ARTIFACT_REVOKED` and is stored in
//! the build's cache entry, and one that does not is ignored with one warning.
//! A 410 whose body's `errors[0].detail.revoked` is an object is the same code.
//! A status that cannot be read fails open with one warning, or in strict mode
//! is the transport's error. Generation 2 only
//! ([`super::channel::Channel::revocation`]): the v1 contract has none of it.
//!
//! Four paths check, each with its own budget:
//!
//! * an install reads its revocation candidates from the trust listing it
//!   already made (`ensure_online`), under the retry table;
//! * `--frozen` reads one listing, warm or cold, under the retry table;
//! * a build already installed and used without a download (a registry open,
//!   `preload`, an `ensure` that finds it installed) is checked at most once
//!   per process ([`check_installed_once`], the memo), one attempt per base, no
//!   backoff, and one deadline ([`constants::REVOCATION_CHECK_S`]) for the
//!   whole check;
//! * `--lock` and `update` refuse a revoked build on every platform they pin.

use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use serde_json::value::RawValue;

use super::channel;
use super::constants;
use super::dsse::{self, TrustedKey, VerifiedStatement};
use super::ensure::{Resolved, Resources};
use super::error::{Error, Result};
use super::http::Budget;
use super::layout;
use super::oci::{Descriptor, Source};
use super::referrers::{self, Listing};
use super::retired;

/// One advisory a revocation names, every string made safe to print (the
/// retired message's rule, §2). A signed revocation's carries its
/// `revoked_at`; a 410's carries the `build` it names, newest first.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Advisory {
    /// The advisory's id (a GHSA id).
    pub id: String,
    /// The advisory's URL.
    pub url: String,
    /// `over-accept`, `crash` or `other`.
    pub class: String,
    /// The advisory's summary.
    pub summary: String,
    /// The revoked build.
    pub build: String,
    /// The build that supersedes it.
    pub superseded_by: String,
    /// When the revocation was signed.
    pub revoked_at: String,
}

/// What a `CHTYPES_ARTIFACT_REVOKED` carries, for programs: whether it is
/// signed, the manifest a signed one revokes, and its advisories. Displays as
/// the error's message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Revocation {
    /// True for a verified, signed revocation of `manifest`, whose one advisory
    /// it is, and false for a 410's body, which is unsigned, names no manifest,
    /// and lists one advisory per revoked build.
    pub signed: bool,
    /// The platform manifest a signed revocation revokes; empty for a 410's.
    pub manifest: String,
    /// The advisories, newest build first for a 410's.
    pub advisories: Vec<Advisory>,
    message: String,
}

impl Revocation {
    /// The error's complete message.
    pub fn message(&self) -> &str {
        &self.message
    }

    pub(super) fn with_message(mut self, message: String) -> Revocation {
        self.message = message;
        self
    }
}

impl fmt::Display for Revocation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

// ---- a 410 on a tag ---------------------------------------------------------

type Fields = HashMap<String, Box<RawValue>>;

/// `raw` as a JSON object's members, each left as its raw token; `None` for
/// anything that is not an object. A repeated key reads as its last value.
fn object(raw: &str) -> Option<Fields> {
    let raw = raw.trim();
    if !raw.starts_with('{') {
        return None;
    }
    serde_json::from_str::<Fields>(raw).ok()
}

/// The error for a 410: `CHTYPES_ARTIFACT_REVOKED` when revocation is on and
/// the body's `errors[0].detail.revoked` is an object, else a retired
/// repository's.
pub fn gone_error(url: &str, status: u16, body: &[u8], revocation: bool) -> Error {
    if revocation {
        if let Some(advisories) = revoked_410(body) {
            let message = format!(
                "{url} answered {status} Gone: {}",
                revoked_410_text(&advisories)
            );
            return Error::ArtifactRevoked(Revocation {
                signed: false,
                manifest: String::new(),
                advisories,
                message,
            });
        }
    }
    Error::SourceRetired(retired::retired_text(url, status, body))
}

/// Reads a 410 body: `Some` when it is UTF-8 JSON whose
/// `errors[0].detail.revoked` is an object, with that object's advisories,
/// each field made safe and one that is absent, not a string, empty once made
/// safe or a lone surrogate escape left empty. An entry that is not an object,
/// or leaves every field empty, is skipped. A repeated key reads as its last
/// value. `tests/fixtures/revoked-410/cases.json` is the table all four
/// bindings answer alike.
pub fn revoked_410(body: &[u8]) -> Option<Vec<Advisory>> {
    let text = std::str::from_utf8(body).ok()?;
    let doc = object(text)?;
    let errors: Vec<Box<RawValue>> = serde_json::from_str(doc.get("errors")?.get()).ok()?;
    let first = object(errors.first()?.get())?;
    let detail = object(first.get("detail")?.get())?;
    let revoked = object(detail.get("revoked")?.get())?;
    let mut advisories = Vec::new();
    let Some(entries) = revoked
        .get("advisories")
        .and_then(|raw| serde_json::from_str::<Vec<Box<RawValue>>>(raw.get()).ok())
    else {
        return Some(advisories);
    };
    for entry in entries {
        let Some(obj) = object(entry.get()) else {
            continue;
        };
        let advisory = Advisory {
            id: safe_field(&obj, "id"),
            url: safe_field(&obj, "url"),
            class: safe_field(&obj, "class"),
            summary: safe_field(&obj, "summary"),
            build: safe_field(&obj, "build"),
            superseded_by: safe_field(&obj, "superseded_by"),
            revoked_at: String::new(),
        };
        if advisory == Advisory::default() {
            continue;
        }
        advisories.push(advisory);
    }
    Some(advisories)
}

/// `obj[key]` when it is a JSON string with no lone surrogate escape, made
/// safe to print; empty otherwise.
fn safe_field(obj: &Fields, key: &str) -> String {
    let Some(raw) = obj.get(key) else {
        return String::new();
    };
    let raw = raw.get().trim();
    if !raw.starts_with('"') || has_lone_surrogate_escape(raw.as_bytes()) {
        return String::new();
    }
    match serde_json::from_str::<String>(raw) {
        Ok(s) => retired::sanitize(&s),
        Err(_) => String::new(),
    }
}

/// Whether a JSON string token holds a `\u` escape of a UTF-16 surrogate that
/// is not half of a high-then-low pair.
fn has_lone_surrogate_escape(token: &[u8]) -> bool {
    fn hex4(token: &[u8], i: usize) -> Option<u32> {
        if i + 6 > token.len() || token[i] != b'\\' || token[i + 1] != b'u' {
            return None;
        }
        let mut v = 0u32;
        for &c in &token[i + 2..i + 6] {
            v = (v << 4) | char::from(c).to_digit(16)?;
        }
        Some(v)
    }
    let mut i = 0;
    while i < token.len() {
        if token[i] != b'\\' {
            i += 1;
            continue;
        }
        let Some(v) = hex4(token, i) else {
            // A two-character escape such as \\ or \".
            i += 2;
            continue;
        };
        match v {
            0xd800..=0xdbff => match hex4(token, i + 6) {
                Some(low) if (0xdc00..=0xdfff).contains(&low) => i += 12,
                _ => return true,
            },
            0xdc00..=0xdfff => return true,
            _ => i += 6,
        }
    }
    false
}

/// What follows `"<url> answered 410 Gone: "`.
fn revoked_410_text(advisories: &[Advisory]) -> String {
    let parts: Vec<String> = advisories
        .iter()
        .map(advisory_text)
        .filter(|t| !t.is_empty())
        .collect();
    if parts.is_empty() {
        "revoked, and the registry names no advisory".to_string()
    } else {
        format!("revoked: {}", parts.join("; "))
    }
}

/// One 410 advisory: `<id> <url> (<class>) build <build>, superseded by build
/// <superseded_by>: <summary>`, a part whose field is empty left out with the
/// separator before it, and the first part present taking none.
fn advisory_text(a: &Advisory) -> String {
    let mut s = String::new();
    let mut join = |sep: &str, part: String| {
        if !s.is_empty() {
            s.push_str(sep);
        }
        s.push_str(&part);
    };
    for p in [&a.id, &a.url] {
        if !p.is_empty() {
            join(" ", p.clone());
        }
    }
    if !a.class.is_empty() {
        join(" ", format!("({})", a.class));
    }
    if !a.build.is_empty() {
        join(" ", format!("build {}", a.build));
    }
    if !a.superseded_by.is_empty() {
        join(", ", format!("superseded by build {}", a.superseded_by));
    }
    if !a.summary.is_empty() {
        join(": ", a.summary.clone());
    }
    s
}

// ---- the build a check concerns ---------------------------------------------

/// The build a check concerns: its own digests and signed predicate, and its
/// installed entry (`dir`) when it has one, writable when that entry is in the
/// user cache.
pub struct Subject {
    pub manifest: String,
    pub layer: String,
    pub predicate: serde_json::Value,
    pub version: String,
    pub platform: String,
    pub build: String,
    pub request: String,
    pub dir: Option<PathBuf>,
    pub writable: bool,
}

fn predicate_str(predicate: &serde_json::Value, key: &str) -> String {
    predicate
        .get(key)
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
        .to_string()
}

impl Subject {
    /// The subject an installed build's [`Resolved`] names.
    pub(super) fn of_resolved(res: &Resources, resolved: &Resolved) -> Subject {
        Subject {
            manifest: resolved.digests.manifest.clone(),
            layer: resolved.digests.layer.clone(),
            predicate: resolved.predicate.clone(),
            version: resolved.version.clone(),
            platform: resolved.platform.clone(),
            build: resolved.build.clone(),
            request: resolved.request.clone(),
            dir: Some(resolved.dir.clone()),
            writable: under_dir(&res.root, &resolved.dir),
        }
    }

    /// The subject of a build not yet installed: the signed `predicate`
    /// verified for it.
    pub fn of_statement(
        predicate: &serde_json::Value,
        manifest: &str,
        layer: &str,
        platform: &str,
        request: &str,
    ) -> Subject {
        Subject {
            manifest: manifest.to_string(),
            layer: layer.to_string(),
            predicate: predicate.clone(),
            version: predicate_str(predicate, "clickhouse_version"),
            platform: platform.to_string(),
            build: predicate_str(predicate, "build"),
            request: request.to_string(),
            dir: None,
            writable: false,
        }
    }
}

fn under_dir(root: &Path, dir: &Path) -> bool {
    dir != root && dir.starts_with(root)
}

// ---- verifying a revocation -------------------------------------------------

/// A JSON number as an integer: an integer token, or a float that is one.
fn as_int(v: Option<&serde_json::Value>) -> Option<i64> {
    let n = v?.as_number()?;
    if let Some(i) = n.as_i64() {
        return Some(i);
    }
    let f = n.as_f64()?;
    (f.fract() == 0.0 && f.abs() < 9.0e15).then_some(f as i64)
}

/// Checks one revocation bundle against `sub` (the guide's five rules): the
/// signature under `trust`, the statement and predicate types, the subject and
/// manifest, the build's identity, and the advisory's shape. Returns the
/// revocation, or why it does not verify.
pub fn verify_revocation(
    body: &[u8],
    trust: &[TrustedKey],
    sub: &Subject,
) -> std::result::Result<Revocation, String> {
    let stmt: VerifiedStatement =
        dsse::verify_bundle(body, trust, constants::STATEMENT_TYPE).map_err(|e| e.to_string())?;
    if stmt.predicate_type != constants::PREDICATE_TYPE_REVOCATION {
        return Err(format!(
            "predicateType {:?}, expected {:?}",
            stmt.predicate_type,
            constants::PREDICATE_TYPE_REVOCATION
        ));
    }
    if format!("sha256:{}", stmt.subject_sha256) != sub.layer {
        return Err(format!(
            "no subject digest is the build's layer {}",
            sub.layer
        ));
    }
    let pred = &stmt.predicate;
    let manifest = pred
        .get("manifest")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    if manifest != sub.manifest {
        return Err(format!(
            "predicate manifest {manifest:?} is not the build's {}",
            sub.manifest
        ));
    }
    let (got, want) = (as_int(pred.get("abi")), as_int(sub.predicate.get("abi")));
    if got.is_none() || want.is_none() || got != want {
        return Err(format!(
            "predicate abi {} is not the build's {}",
            show(pred.get("abi")),
            show(sub.predicate.get("abi"))
        ));
    }
    for f in [
        "abi_fingerprint",
        "os",
        "arch",
        "clickhouse_version",
        "build",
    ] {
        let g = pred.get(f).and_then(serde_json::Value::as_str);
        let w = sub.predicate.get(f).and_then(serde_json::Value::as_str);
        if g.is_none() || w.is_none() || g != w {
            return Err(format!(
                "predicate {f} {} is not the build's {}",
                show(pred.get(f)),
                show(sub.predicate.get(f))
            ));
        }
    }
    let advisory = pred.get("advisory").and_then(serde_json::Value::as_object);
    let (id, url) = (advisory_str(advisory, "id"), advisory_str(advisory, "url"));
    if id.is_empty() || url.is_empty() {
        return Err("predicate advisory does not name an id and a URL".to_string());
    }
    let mut strings = [""; 4];
    for (slot, f) in strings
        .iter_mut()
        .zip(["class", "summary", "superseded_by", "revoked_at"])
    {
        match pred.get(f).and_then(serde_json::Value::as_str) {
            Some(v) => *slot = v,
            None => return Err(format!("predicate {f} is not a string")),
        }
    }
    let [class, summary, superseded_by, revoked_at] = strings;
    Ok(Revocation {
        signed: true,
        manifest: sub.manifest.clone(),
        advisories: vec![Advisory {
            id: retired::sanitize(id),
            url: retired::sanitize(url),
            class: retired::sanitize(class),
            summary: retired::sanitize(summary),
            build: sub.build.clone(),
            superseded_by: retired::sanitize(superseded_by),
            revoked_at: retired::sanitize(revoked_at),
        }],
        message: String::new(),
    })
}

/// A predicate member as it is printed in a reason, `null` when it is absent.
fn show(v: Option<&serde_json::Value>) -> String {
    v.map_or_else(|| "null".to_string(), ToString::to_string)
}

/// `advisory[key]` when it is a string, else empty.
fn advisory_str<'a>(
    advisory: Option<&'a serde_json::Map<String, serde_json::Value>>,
    key: &str,
) -> &'a str {
    advisory
        .and_then(|a| a.get(key))
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
}

/// The guide's message for a verified revocation: `<version> <platform> build
/// <build> (manifest <digest>) is revoked: <id> <url> (<class>); superseded by
/// build <superseded_by>: <summary>`, a part whose field is empty left out.
pub fn revoked_text(sub: &Subject, rev: &Revocation) -> String {
    let a = &rev.advisories[0];
    let mut msg = format!(
        "{} {} build {} (manifest {}) is revoked: {} {}",
        sub.version, sub.platform, sub.build, sub.manifest, a.id, a.url
    );
    if !a.class.is_empty() {
        msg.push_str(&format!(" ({})", a.class));
    }
    if !a.superseded_by.is_empty() {
        msg.push_str(&format!("; superseded by build {}", a.superseded_by));
    }
    if !a.summary.is_empty() {
        msg.push_str(&format!(": {}", a.summary));
    }
    msg
}

// ---- reading a status -------------------------------------------------------

/// One verified revocation: the bundle, for the stored record, and what it says.
#[derive(Debug, Clone)]
pub struct Found {
    pub bundle: Vec<u8>,
    pub rev: Revocation,
}

/// Which error a status that could not be read becomes in strict mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnreadKind {
    Unauthorized,
    Forbidden,
    Unreachable,
}

/// Why a status could not be read.
#[derive(Debug, Clone)]
pub struct Unread {
    pub kind: UnreadKind,
    pub cause: String,
}

impl Unread {
    fn of(e: &Error, cause: String) -> Unread {
        let kind = match e {
            Error::SourceUnauthorized(_) => UnreadKind::Unauthorized,
            Error::SourceForbidden(_) => UnreadKind::Forbidden,
            _ => UnreadKind::Unreachable,
        };
        Unread { kind, cause }
    }
}

/// What one check learned: a verified revocation, the warnings for the
/// candidates it ignored, and, when it could not read the status, why.
#[derive(Debug, Default)]
pub struct Outcome {
    pub verified: Option<Found>,
    pub warnings: Vec<String>,
    pub unread: Option<Unread>,
}

/// Fetches and verifies each revocation candidate in turn; the first that
/// verifies is the answer. Its error is only ever a 410's.
pub fn check_candidates(
    source: &Source<'_>,
    candidates: &[Descriptor],
    sub: &Subject,
    trust: &[TrustedKey],
) -> Result<Outcome> {
    let mut out = Outcome::default();
    for cand in candidates {
        let reason = match referrers::fetch_bundle(source, cand) {
            Ok((bytes, _)) => match verify_revocation(&bytes, trust, sub) {
                Ok(rev) => {
                    out.verified = Some(Found { bundle: bytes, rev });
                    return Ok(out);
                }
                Err(reason) => reason,
            },
            Err(e) if e.is_gone() => return Err(e),
            Err(Error::ArtifactCorrupt(reason)) => reason,
            Err(e) => {
                out.unread = Some(Unread::of(
                    &e,
                    format!("revocation {} could not be read: {e}", cand.digest),
                ));
                continue;
            }
        };
        out.warnings.push(format!(
            "chtypes: revocation {} for manifest {} does not verify and is ignored: {reason}",
            cand.digest, sub.manifest
        ));
    }
    Ok(out)
}

/// One network check of `sub`: its listing, then its candidates. A listing that
/// names no signature referrer is an unread status.
pub fn check_listing(source: &Source<'_>, trust: &[TrustedKey], sub: &Subject) -> Result<Outcome> {
    let listing = referrers::read_listing(source, &sub.manifest)?;
    check_read_listing(source, trust, sub, listing)
}

/// [`check_listing`] over a listing already read.
pub fn check_read_listing(
    source: &Source<'_>,
    trust: &[TrustedKey],
    sub: &Subject,
    listing: Listing,
) -> Result<Outcome> {
    if !listing.found {
        let unread = match &listing.failure {
            Some(e) => Unread::of(e, e.to_string()),
            None => Unread {
                kind: UnreadKind::Unreachable,
                cause: "no listing names a signature for this manifest".to_string(),
            },
        };
        return Ok(Outcome {
            unread: Some(unread),
            ..Outcome::default()
        });
    }
    check_candidates(source, &listing.revocations, sub, trust)
}

/// `base` with a password in its userinfo redacted.
fn redact(base: &str) -> String {
    let Some(scheme_end) = base.find("://") else {
        return base.to_string();
    };
    let rest = &base[scheme_end + 3..];
    let authority_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = &rest[..authority_end];
    let Some(at) = authority.rfind('@') else {
        return base.to_string();
    };
    let Some((user, _password)) = authority[..at].split_once(':') else {
        return base.to_string();
    };
    format!("{}{user}:xxxxx{}", &base[..scheme_end + 3], &rest[at..])
}

/// The guide's fail-open warning, without the `chtypes: ` every warning starts
/// with: `revocation status not checked for manifest <digest> (bases: <base>,
/// <base>): <cause>`.
fn not_checked_text(sub: &Subject, bases: &[String], cause: &str) -> String {
    let shown: Vec<String> = bases.iter().map(|b| redact(b)).collect();
    format!(
        "revocation status not checked for manifest {} (bases: {}): {cause}",
        sub.manifest,
        shown.join(", ")
    )
}

/// Turns an outcome into the call's answer: a verified revocation is stored in
/// an installed, writable entry (when `store`) and refuses the build, or with
/// the override becomes one warning; an unread status is one warning, or in
/// strict mode the transport's error.
pub(super) fn settle(
    res: &Resources,
    sub: &Subject,
    out: &Outcome,
    store: bool,
) -> Result<Vec<String>> {
    let mut warnings = out.warnings.clone();
    if let Some(found) = &out.verified {
        if store && sub.writable {
            if let Some(dir) = &sub.dir {
                // A read-only cache still refuses, for this call and process.
                let _ = write_record(dir, &found.bundle);
            }
        }
        let text = revoked_text(sub, &found.rev);
        if res.allow_revoked {
            warnings.push(format!("chtypes: allowing a revoked build: {text}"));
            return Ok(warnings);
        }
        return Err(Error::ArtifactRevoked(found.rev.clone().with_message(text)));
    }
    if let Some(unread) = &out.unread {
        let text = not_checked_text(sub, &res.bases, &unread.cause);
        if res.require_revocation_check {
            return Err(match unread.kind {
                UnreadKind::Unauthorized => Error::SourceUnauthorized(text),
                UnreadKind::Forbidden => Error::SourceForbidden(text),
                UnreadKind::Unreachable => Error::SourceUnreachable(text),
            });
        }
        warnings.push(format!("chtypes: {text}"));
    }
    Ok(warnings)
}

// ---- the stored record ------------------------------------------------------

/// Stores a verified revocation's bundle, byte for byte, in an installed
/// build's entry: a temporary file, then a rename.
pub fn write_record(dir: &Path, bundle: &[u8]) -> Result<()> {
    let dest = dir.join(constants::CACHE_REVOCATION_RECORD);
    if std::fs::read(&dest).is_ok_and(|b| b == bundle) {
        return Ok(());
    }
    layout::write_atomic(&dest, bundle)
}

/// Stores the revocation an override let a call install past, in the entry it
/// installed.
pub(super) fn store_pending(res: &Resources, resolved: &Resolved, pending: Option<&Found>) {
    if let Some(found) = pending {
        if under_dir(&res.root, &resolved.dir) {
            let _ = write_record(&resolved.dir, &found.bundle);
        }
    }
}

/// Reads `sub`'s stored record, if its entry holds one: verified, it decides
/// the call (`decided` is true; an error is the refusal); one that does not
/// verify is a warning, and the call goes on.
fn check_stored(res: &Resources, sub: &Subject) -> Result<(Vec<String>, bool)> {
    let Some(dir) = &sub.dir else {
        return Ok((Vec::new(), false));
    };
    let path = dir.join(constants::CACHE_REVOCATION_RECORD);
    let Ok(body) = std::fs::read(&path) else {
        return Ok((Vec::new(), false));
    };
    match verify_revocation(&body, &res.trust, sub) {
        Err(reason) => Ok((
            vec![format!(
                "chtypes: revocation record {} for manifest {} does not verify and is ignored: {reason}",
                path.display(),
                sub.manifest
            )],
            false,
        )),
        Ok(rev) => {
            let out = Outcome {
                verified: Some(Found { bundle: body, rev }),
                ..Outcome::default()
            };
            Ok((settle(res, sub, &out, false)?, true))
        }
    }
}

/// The check every call that answers from the cache with no request makes
/// (`--offline`, `--frozen --offline`): the stored record.
pub(super) fn check_installed_stored(res: &Resources, resolved: &mut Resolved) -> Result<()> {
    if !channel::active().revocation {
        return Ok(());
    }
    let sub = Subject::of_resolved(res, resolved);
    let (warnings, _) = check_stored(res, &sub)?;
    resolved.warnings.extend(warnings);
    Ok(())
}

/// `--frozen`'s check of the build installed under its pin: the stored record,
/// then one listing under the retry table.
pub(super) fn check_frozen_installed(res: &Resources, resolved: &mut Resolved) -> Result<()> {
    if !channel::active().revocation {
        return Ok(());
    }
    let sub = Subject::of_resolved(res, resolved);
    let (warnings, decided) = check_stored(res, &sub)?;
    resolved.warnings.extend(warnings);
    if decided {
        return Ok(());
    }
    let source = Source {
        bases: &res.bases,
        client: &res.client,
        auth: &res.auth,
        budget: Budget::default(),
    };
    let out = check_listing(&source, &res.trust, &sub)?;
    resolved.warnings.extend(settle(res, &sub, &out, true)?);
    Ok(())
}

// ---- the installed-build check ----------------------------------------------

/// The installed-build check's in-process outcomes, by platform manifest
/// digest: at most one check per build per process.
#[derive(Clone)]
enum MemoState {
    NotRevoked,
    Revoked(Found),
    NotChecked,
}

struct MemoEntry {
    /// `None` until the check has an outcome.
    state: Mutex<Option<MemoState>>,
    done: Condvar,
}

impl MemoEntry {
    fn new() -> MemoEntry {
        MemoEntry {
            state: Mutex::new(None),
            done: Condvar::new(),
        }
    }

    fn set(&self, state: MemoState) {
        *self.state.lock().unwrap_or_else(|e| e.into_inner()) = Some(state);
        self.done.notify_all();
    }

    fn wait(&self) -> MemoState {
        let guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let guard = self
            .done
            .wait_while(guard, |s| s.is_none())
            .unwrap_or_else(|e| e.into_inner());
        guard.clone().unwrap_or(MemoState::NotChecked)
    }
}

static MEMO: Mutex<BTreeMap<String, Arc<MemoEntry>>> = Mutex::new(BTreeMap::new());

fn memo() -> MutexGuard<'static, BTreeMap<String, Arc<MemoEntry>>> {
    MEMO.lock().unwrap_or_else(|e| e.into_inner())
}

/// Forgets every kept outcome, as a new process would. A conformance runner
/// calls it before every case, because the cases reuse manifests across cache
/// directories, which one process never sees in use.
#[cfg(test)]
pub fn reset_revocation_memo_for_tests() {
    // Waits for a unit test that is holding the memo (`MEMO_TEST_LOCK`).
    let _serial = MEMO_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    memo().clear();
}

/// Serialises the unit tests that read the memo against
/// [`reset_revocation_memo_for_tests`]: the conformance suite resets it before
/// every case, and libtest runs its tests in parallel.
#[cfg(test)]
static MEMO_TEST_LOCK: Mutex<()> = Mutex::new(());

#[cfg(test)]
thread_local! {
    static DEADLINE_FOR_TESTS: std::cell::Cell<Option<Duration>> =
        const { std::cell::Cell::new(None) };
}

/// The installed-build check's one deadline: [`constants::REVOCATION_CHECK_S`],
/// unless a unit test on this thread shortened it.
fn check_deadline() -> Duration {
    #[cfg(test)]
    {
        if let Some(shortened) = DEADLINE_FOR_TESTS.with(std::cell::Cell::get) {
            return shortened;
        }
    }
    Duration::from_secs_f64(constants::REVOCATION_CHECK_S)
}

/// The installed-build check: the stored record, then, unless offline, the kept
/// outcome or one check of its own, with one attempt per base, no backoff and
/// one deadline. Concurrent checks of one build share one; a kept "not checked"
/// answers only a call without strict mode, and a check that ends in an error
/// keeps nothing. Returns the warnings of the call.
pub(super) fn check_installed_once(res: &Resources, resolved: &Resolved) -> Result<Vec<String>> {
    if !channel::active().revocation {
        return Ok(Vec::new());
    }
    let sub = Subject::of_resolved(res, resolved);
    let (mut warnings, decided) = check_stored(res, &sub)?;
    if decided || res.offline {
        return Ok(warnings);
    }
    loop {
        let (entry, lead) = {
            let mut kept = memo();
            match kept.get(&sub.manifest) {
                Some(e) => (Arc::clone(e), false),
                None => {
                    let e = Arc::new(MemoEntry::new());
                    kept.insert(sub.manifest.clone(), Arc::clone(&e));
                    (e, true)
                }
            }
        };

        if !lead {
            let state = entry.wait();
            let current = memo()
                .get(&sub.manifest)
                .is_some_and(|e| Arc::ptr_eq(e, &entry));
            if !current {
                continue; // that check kept nothing: check again
            }
            return match state {
                MemoState::Revoked(found) => {
                    let out = Outcome {
                        verified: Some(found),
                        ..Outcome::default()
                    };
                    warnings.extend(settle(res, &sub, &out, true)?);
                    Ok(warnings)
                }
                MemoState::NotChecked if res.require_revocation_check => {
                    // A strict call never takes a kept "not checked": it checks
                    // for itself, and keeps a better answer when it gets one.
                    let out = check_with_deadline(res, &sub)?;
                    if out.unread.is_none() {
                        keep(&sub.manifest, &out);
                    }
                    warnings.extend(settle(res, &sub, &out, true)?);
                    Ok(warnings)
                }
                // Not revoked, or not checked and already warned.
                MemoState::NotRevoked | MemoState::NotChecked => Ok(warnings),
            };
        }

        let (outcome, result) = match check_with_deadline(res, &sub) {
            Err(e) => (None, Err(e)),
            Ok(out) => {
                let settled = settle(res, &sub, &out, true);
                (Some(out), settled)
            }
        };
        let verified = outcome.as_ref().and_then(|o| o.verified.clone());
        let unread = outcome.as_ref().is_some_and(|o| o.unread.is_some());
        {
            let mut kept = memo();
            if result.is_err() && verified.is_none() {
                // A 410 or a strict failure: keep nothing.
                kept.remove(&sub.manifest);
                entry.set(MemoState::NotChecked);
            } else if let Some(found) = verified {
                entry.set(MemoState::Revoked(found));
            } else if unread {
                entry.set(MemoState::NotChecked);
            } else {
                entry.set(MemoState::NotRevoked);
            }
        }
        warnings.extend(result?);
        return Ok(warnings);
    }
}

/// Records `out` as `manifest`'s kept outcome, replacing a "not checked".
fn keep(manifest: &str, out: &Outcome) {
    if let Some(e) = memo().get(manifest) {
        e.set(match &out.verified {
            Some(found) => MemoState::Revoked(found.clone()),
            None => MemoState::NotRevoked,
        });
    }
}

/// One network check under the installed-build check's budget. Passing the
/// deadline is an unread status.
fn check_with_deadline(res: &Resources, sub: &Subject) -> Result<Outcome> {
    let allowed = check_deadline();
    let deadline = Instant::now() + allowed;
    let source = Source {
        bases: &res.bases,
        client: &res.client,
        auth: &res.auth,
        budget: Budget {
            single: true,
            deadline: Some(deadline),
        },
    };
    let mut out = check_listing(&source, &res.trust, sub)?;
    if out.verified.is_none() && Instant::now() >= deadline {
        if let Some(unread) = &mut out.unread {
            unread.cause = format!(
                "the check passed its {}s deadline: {}",
                allowed.as_secs_f64(),
                unread.cause
            );
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decode_hex(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("hex"))
            .collect()
    }

    /// The table all four bindings read: one answer for every 410 body in it
    /// (`tests/fixtures/revoked-410/cases.json`). Under the v1 contract every
    /// 410 is a retired repository.
    #[test]
    fn gone_error_answers_the_shared_table() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../tests/fixtures/revoked-410/cases.json");
        let table: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).expect("read the shared table"))
                .expect("parse the shared table");
        let cases = table["cases"].as_array().expect("cases");
        assert!(!cases.is_empty(), "the revoked-410 table has no rows");
        const URL: &str = "https://registry.example/v2/chtypes/v2/manifests/26.13";
        let mut failures = Vec::new();
        let mut revoked_rows = 0;
        for case in cases {
            let name = case["name"].as_str().expect("name");
            let body = match (case["body_text"].as_str(), case["body_hex"].as_str()) {
                (Some(text), _) => text.as_bytes().to_vec(),
                (None, Some(hex)) => decode_hex(hex),
                (None, None) => panic!("{name}: neither body_text nor body_hex"),
            };
            let want_code = case["code"].as_str().expect("code");
            let err = gone_error(URL, 410, &body, true);
            if err.code() != want_code {
                failures.push(format!(
                    "{name}: code {}, want {want_code}: {err}",
                    err.code()
                ));
                continue;
            }
            if gone_error(URL, 410, &body, false).code() != "CHTYPES_SOURCE_RETIRED" {
                failures.push(format!(
                    "{name}: with revocation off, not a retired repository"
                ));
            }
            let Error::ArtifactRevoked(rev) = &err else {
                continue;
            };
            revoked_rows += 1;
            let want = &case["revoked"];
            let want_message = format!(
                "{URL} answered 410 Gone: {}",
                want["text"].as_str().expect("text")
            );
            if rev.message() != want_message {
                failures.push(format!(
                    "{name}: message\n got {:?}\nwant {want_message:?}",
                    rev.message()
                ));
            }
            if rev.signed || !rev.manifest.is_empty() {
                failures.push(format!("{name}: want an unsigned revocation, got {rev:?}"));
            }
            let field = |a: &serde_json::Value, k: &str| a[k].as_str().unwrap_or("").to_string();
            let want_advisories: Vec<Advisory> = want["advisories"]
                .as_array()
                .expect("advisories")
                .iter()
                .map(|a| Advisory {
                    id: field(a, "id"),
                    url: field(a, "url"),
                    class: field(a, "class"),
                    summary: field(a, "summary"),
                    build: field(a, "build"),
                    superseded_by: field(a, "superseded_by"),
                    revoked_at: String::new(),
                })
                .collect();
            if rev.advisories != want_advisories {
                failures.push(format!(
                    "{name}: advisories\n got {:?}\nwant {want_advisories:?}",
                    rev.advisories
                ));
            }
        }
        assert!(revoked_rows > 0, "the table has no revoked row");
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }

    #[test]
    fn lone_surrogate_escapes_are_found_only_when_unpaired() {
        assert!(has_lone_surrogate_escape(br#""a\ud800b""#));
        assert!(has_lone_surrogate_escape(br#""\udc00""#));
        assert!(has_lone_surrogate_escape(br#""\ud800\u0041""#));
        assert!(!has_lone_surrogate_escape(br#""\ud83d\ude00""#));
        assert!(!has_lone_surrogate_escape(br#""plain \\ud800 text""#));
    }

    #[test]
    fn a_password_in_a_base_is_redacted() {
        assert_eq!(
            redact("https://user:secret@registry.example/chtypes/v2"),
            "https://user:xxxxx@registry.example/chtypes/v2"
        );
        assert_eq!(
            redact("https://registry.example/chtypes/v2"),
            "https://registry.example/chtypes/v2"
        );
        assert_eq!(
            redact("https://user@registry.example/chtypes/v2"),
            "https://user@registry.example/chtypes/v2"
        );
    }

    #[test]
    fn advisory_parts_are_left_out_with_their_separators() {
        let a = Advisory {
            id: "GHSA-x".into(),
            url: "https://u".into(),
            class: "crash".into(),
            summary: "boom".into(),
            build: "20261001.000001".into(),
            superseded_by: "20261002.000001".into(),
            revoked_at: String::new(),
        };
        assert_eq!(
            advisory_text(&a),
            "GHSA-x https://u (crash) build 20261001.000001, superseded by build 20261002.000001: boom"
        );
        let only_summary = Advisory {
            summary: "just this".into(),
            ..Advisory::default()
        };
        assert_eq!(advisory_text(&only_summary), "just this");
        assert_eq!(
            revoked_410_text(&[]),
            "revoked, and the registry names no advisory"
        );
    }

    // ---- the installed-build check: the memo and the deadline ----------------
    //
    // What no conformance case reaches (docs/guides/fetch-v1.md, "Build-level
    // revocation"): the in-process memo, and the 5 s deadline (shortened here).
    // Each test checks a build of its own digest, so the process-wide memo never
    // mixes two of them; the lock keeps the conformance suite's per-case reset
    // out of the middle of one.

    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    use super::super::http::{AuthConfig, Client, Clock};

    /// A registry that answers every request 404, as a tree with no discovery,
    /// and counts them. While `stall` is set it holds each request unanswered.
    struct ListingServer {
        port: u16,
        requests: Arc<AtomicUsize>,
        stall: Arc<AtomicBool>,
    }

    impl ListingServer {
        fn start(stalled: bool) -> ListingServer {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            let requests = Arc::new(AtomicUsize::new(0));
            let stall = Arc::new(AtomicBool::new(stalled));
            let (n, hold) = (requests.clone(), stall.clone());
            std::thread::spawn(move || {
                for stream in listener.incoming() {
                    let Ok(mut sock) = stream else { continue };
                    let (n, hold) = (n.clone(), hold.clone());
                    std::thread::spawn(move || {
                        let mut buf = [0u8; 4096];
                        if sock.read(&mut buf).unwrap_or(0) == 0 {
                            return;
                        }
                        n.fetch_add(1, Ordering::SeqCst);
                        let started = Instant::now();
                        while hold.load(Ordering::SeqCst) && started.elapsed().as_secs() < 10 {
                            std::thread::sleep(Duration::from_millis(20));
                        }
                        let _ = sock.write_all(
                            b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                        );
                    });
                }
            });
            ListingServer {
                port,
                requests,
                stall,
            }
        }

        fn base(&self, path: &str) -> String {
            format!("http://127.0.0.1:{}{path}", self.port)
        }

        fn count(&self) -> usize {
            self.requests.load(Ordering::SeqCst)
        }
    }

    /// A clock that records every sleep instead of sleeping.
    struct RecordingClock(Arc<Mutex<Vec<f64>>>);

    impl Clock for RecordingClock {
        fn now(&self) -> std::time::SystemTime {
            std::time::SystemTime::now()
        }

        fn sleep(&self, seconds: f64) {
            self.0.lock().unwrap().push(seconds);
        }
    }

    fn resources_for(root: &Path, bases: Vec<String>, strict: bool, client: Client) -> Resources {
        Resources {
            root: root.to_path_buf(),
            platform: "linux-arm64".to_string(),
            bases,
            system_dirs: Vec::new(),
            strict: false,
            client,
            auth: AuthConfig::default(),
            trust: Vec::new(),
            offline: false,
            allow_revoked: false,
            require_revocation_check: strict,
        }
    }

    /// A build "installed" in a directory of its own, whose manifest digest is
    /// `serial` (so no two tests share a memo key).
    fn installed_build(root: &Path, serial: u64) -> Resolved {
        let manifest = format!("sha256:{serial:064x}");
        let dir = root.join(format!("entry-{serial}"));
        std::fs::create_dir_all(&dir).unwrap();
        Resolved {
            abi_generation: 2,
            platform: "linux-arm64".to_string(),
            request: "26.13".to_string(),
            version: "26.13.1.1".to_string(),
            channel: None,
            build: "20261013.000001".to_string(),
            library_path: dir.join("libchtypes.so"),
            dir,
            digests: super::super::ensure::Digests {
                index: None,
                manifest,
                layer: format!("sha256:{:064x}", serial + 1),
                bundle: None,
                bundle_manifest: None,
            },
            predicate: serde_json::json!({
                "abi": 2, "os": "linux", "arch": "arm64",
                "clickhouse_version": "26.13.1.1", "build": "20261013.000001",
            }),
            signed_by: String::new(),
            source: String::new(),
            already_installed: true,
            warnings: Vec::new(),
        }
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("revocation-test-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Two checks of one build make one listing and one warning; a strict call
    /// never reuses the kept "not checked", checks for itself and fails; a later
    /// call without strict mode is answered from what was kept.
    #[test]
    fn an_installed_build_is_checked_once_per_process() {
        let _serial = MEMO_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let root = scratch("once");
        let server = ListingServer::start(false);
        let build = installed_build(&root, 0x1001);
        let bases = vec![server.base("/chtypes/v2")];
        let lenient = resources_for(&root, bases.clone(), false, Client::new());
        let strict = resources_for(&root, bases, true, Client::new());

        let first = check_installed_once(&lenient, &build).expect("a first check fails open");
        assert_eq!(first.len(), 1, "one not-checked warning: {first:?}");
        assert!(
            first[0].contains(&format!(
                "revocation status not checked for manifest {}",
                build.digests.manifest
            )),
            "{first:?}"
        );
        assert_eq!(
            server.count(),
            2,
            "one listing: the referrers API, then the fallback tag"
        );

        let second = check_installed_once(&lenient, &build).expect("a second check fails open");
        assert!(
            second.is_empty(),
            "the second check says nothing: {second:?}"
        );
        assert_eq!(server.count(), 2, "the second check made no request");

        let err = check_installed_once(&strict, &build)
            .expect_err("a strict call never takes a kept \"not checked\"");
        assert!(
            matches!(&err, Error::SourceUnreachable(m) if m.contains("revocation status not checked")),
            "want SourceUnreachable naming the unchecked status, got {err:?}"
        );
        assert_eq!(server.count(), 4, "the strict call checked for itself");

        let after = check_installed_once(&lenient, &build).expect("a lenient call after");
        assert!(after.is_empty(), "answered from what was kept: {after:?}");
        assert_eq!(
            server.count(),
            4,
            "the kept outcome survived the strict failure"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    /// A registry that never answers costs the check its one deadline, not the
    /// retry table's minutes; the call goes on with a warning that names the
    /// deadline, and sleeps never. The deadline is shortened here; the
    /// production one is `constants::REVOCATION_CHECK_S`.
    #[test]
    fn a_stalled_registry_costs_the_check_its_deadline() {
        let _serial = MEMO_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        assert_eq!(constants::REVOCATION_CHECK_S, 5.0, "the record says 5 s");
        assert_eq!(check_deadline(), Duration::from_secs(5));
        DEADLINE_FOR_TESTS.with(|d| d.set(Some(Duration::from_millis(300))));
        let root = scratch("deadline");
        let server = ListingServer::start(true);
        let build = installed_build(&root, 0x2001);
        let sleeps = Arc::new(Mutex::new(Vec::new()));
        let res = resources_for(
            &root,
            vec![
                server.base("/chtypes/v2"),
                server.base("/mirror/chtypes/v2"),
            ],
            false,
            Client::with_clock(Box::new(RecordingClock(sleeps.clone()))),
        );

        let started = Instant::now();
        let result = check_installed_once(&res, &build);
        let elapsed = started.elapsed();
        server.stall.store(false, Ordering::SeqCst);
        DEADLINE_FOR_TESTS.with(|d| d.set(None));

        let warnings = result.expect("a stalled registry fails open");
        assert_eq!(warnings.len(), 1, "one warning: {warnings:?}");
        assert!(
            warnings[0].contains("passed its"),
            "the warning names the deadline: {warnings:?}"
        );
        assert!(
            elapsed < Duration::from_secs(3),
            "the check took {elapsed:?}, far past its 300ms deadline"
        );
        assert!(
            sleeps.lock().unwrap().is_empty(),
            "the check slept {:?}: it has no backoff",
            sleeps.lock().unwrap()
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
