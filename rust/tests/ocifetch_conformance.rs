//! The Rust runner for the v1 fetch-layer conformance suite (plan §3.3,
//! `docs/guides/fetch-v1.md` §10). `CI` runs this as
//! `cargo test --features fetch-v1 --test ocifetch_conformance`.
//!
//! Unset `CHTYPES_V1_CONFORMANCE`, and this test skips LOUDLY (a clear
//! stderr announcement, a report naming zero cases) rather than silently
//! reporting nothing — the same discipline `tests/fetch.rs` and every other
//! binding's fixture-backed suite in this repository follows.
//!
//! This file recompiles `src/ocifetch/` under its own crate root via
//! `#[path]`: `mod ocifetch;` in `lib.rs` is deliberately private (plan §4
//! "Lane Rust", one line only), so an ordinary integration test — a
//! separate crate that links `chtypes` as a ordinary external dependency —
//! cannot otherwise reach it. See `src/ocifetch/mod.rs`'s module doc for the
//! full reasoning.
#[path = "../src/ocifetch/mod.rs"]
mod ocifetch;

use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

use ocifetch::ensure::{self, Options};
use serde::Deserialize;

const ENV_FIXTURES: &str = "CHTYPES_V1_CONFORMANCE";
const ENV_REPORT: &str = "CHTYPES_V1_REPORT";

// ---- cases.json (spec/fetch-v1/schema/cases.schema.json) -----------------

#[derive(Deserialize)]
struct CasesFile {
    #[allow(dead_code)]
    schema: u32,
    cases: Vec<Case>,
}

#[derive(Deserialize, Clone)]
struct Case {
    id: String,
    tree: String,
    transports: Vec<String>,
    #[allow(dead_code)]
    http_script: Option<String>,
    setup: Setup,
    request: Req,
    env: HashMap<String, String>,
    expect: Expect,
}

#[derive(Deserialize, Clone)]
struct Setup {
    cache: String,
    system_dirs: Vec<String>,
    lock: Option<String>,
    before_index_rename_hook: Option<String>,
}

#[derive(Deserialize, Clone)]
struct Req {
    spelling: String,
    platform: String,
    offline: bool,
    frozen: bool,
    lock_write: bool,
    update: bool,
    allow_unsigned: bool,
    trust: String,
    bases: Vec<String>,
}

#[derive(Deserialize, Clone)]
struct Expect {
    ok: bool,
    version: Option<String>,
    #[allow(dead_code)]
    build: Option<String>,
    manifest: Option<String>,
    library_sha256: Option<String>,
    code: Option<String>,
    #[allow(dead_code)]
    sleeps: Vec<f64>,
    warnings: Vec<String>,
    #[allow(dead_code)]
    requests: RequestsExpect,
    lock_after: Option<String>,
}

#[derive(Deserialize, Clone)]
struct RequestsExpect {
    #[allow(dead_code)]
    max: Option<u64>,
    #[allow(dead_code)]
    none_matching: Vec<String>,
    #[allow(dead_code)]
    auth_on_second_origin: bool,
}

// ---- the report (spec/fetch-v1/schema/report.schema.json) ----------------

#[derive(serde::Serialize)]
struct ReportFile {
    schema: u32,
    binding: &'static str,
    toolchain: String,
    cases_sha256: String,
    results: Vec<ReportResult>,
}

#[derive(serde::Serialize)]
struct ReportResult {
    id: String,
    transport: String,
    verdict: &'static str,
    detail: String,
}

/// `transport`s this runner actually drives. `registry` is the `v1-network`
/// job's own suite, against a live `registry:2`; it never runs here.
const RUNNER_TRANSPORTS: &[&str] = &["file", "http"];

#[test]
fn conformance() {
    let Ok(fixtures_env) = std::env::var(ENV_FIXTURES) else {
        eprintln!(
            "SKIP: {ENV_FIXTURES} is not set — the v1 conformance suite needs \
             tests/fixtures/fetch-v1/ (plan §3.2); this test runs nothing without it."
        );
        return;
    };
    let fixtures_root = PathBuf::from(&fixtures_env);
    if !fixtures_root.is_dir() {
        panic!("{ENV_FIXTURES}={fixtures_env:?} is not a directory");
    }

    let cases_path = fixtures_root.join("cases.json");
    let cases_bytes = std::fs::read(&cases_path)
        .unwrap_or_else(|e| panic!("reading {}: {e}", cases_path.display()));
    let cases_sha256 = ocifetch::oci::sha256_hex(&cases_bytes);
    let cases_file: CasesFile = serde_json::from_slice(&cases_bytes)
        .unwrap_or_else(|e| panic!("parsing {}: {e}", cases_path.display()));
    assert_eq!(cases_file.schema, 1, "cases.json schema");

    let mut server = ServerHandle::start(&fixtures_root);
    let mut results = Vec::new();

    for case in &cases_file.cases {
        for transport in RUNNER_TRANSPORTS {
            if !case.transports.iter().any(|t| t == transport) {
                continue;
            }
            let (verdict, detail) = run_case(case, transport, &fixtures_root, server.as_mut());
            results.push(ReportResult {
                id: case.id.clone(),
                transport: transport.to_string(),
                verdict,
                detail,
            });
        }
    }

    let toolchain = toolchain_string();
    let report = ReportFile {
        schema: 1,
        binding: "rust",
        toolchain,
        cases_sha256,
        results,
    };
    if let Ok(report_path) = std::env::var(ENV_REPORT) {
        let bytes = serde_json::to_vec_pretty(&report).expect("serializing the report");
        std::fs::write(&report_path, bytes)
            .unwrap_or_else(|e| panic!("writing {ENV_REPORT}={report_path:?}: {e}"));
    } else {
        eprintln!("note: {ENV_REPORT} is not set; the report below was computed but not written");
    }

    let failed: Vec<&ReportResult> = report
        .results
        .iter()
        .filter(|r| r.verdict == "fail")
        .collect();
    if !failed.is_empty() {
        let mut msg = format!(
            "{} of {} (case, transport) pairs failed:\n",
            failed.len(),
            report.results.len()
        );
        for r in &failed {
            msg.push_str(&format!("  {} [{}]: {}\n", r.id, r.transport, r.detail));
        }
        panic!("{msg}");
    }
    assert!(
        !report.results.is_empty(),
        "0 (case, transport) pairs ran — a suite that ran nothing is not a pass"
    );
}

/// Run one (case, transport) pair, catching a panic from inside `ensure()`
/// or an assertion mismatch and turning it into a `fail` verdict rather than
/// aborting the whole suite — one bad case must not hide every other
/// result.
fn run_case(
    case: &Case,
    transport: &str,
    fixtures_root: &Path,
    server: Option<&mut ServerHandle>,
) -> (&'static str, String) {
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        execute_case(case, transport, fixtures_root, server)
    }));
    match outcome {
        Ok(Ok(())) => ("pass", String::new()),
        Ok(Err(detail)) => ("fail", detail),
        Err(payload) => {
            let detail = payload
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| payload.downcast_ref::<&str>().map(|s| s.to_string()))
                .unwrap_or_else(|| "panic with a non-string payload".to_string());
            ("fail", detail)
        }
    }
}

fn execute_case(
    case: &Case,
    transport: &str,
    fixtures_root: &Path,
    server: Option<&mut ServerHandle>,
) -> Result<(), String> {
    let base = match transport {
        "file" => format!(
            "file://{}/trees/{}/v2/chtypes/v1",
            fixtures_root.display(),
            case.tree
        ),
        "http" => {
            let server =
                server.ok_or_else(|| "http transport but the server did not start".to_string())?;
            format!("http://127.0.0.1:{}/s-{}/chtypes/v1", server.port, case.id)
        }
        other => return Err(format!("unsupported transport {other:?}")),
    };
    let bases: Vec<String> = case
        .request
        .bases
        .iter()
        .map(|b| b.replace("{base}", &base))
        .collect();

    let cache_dir = stage_cache(fixtures_root, &case.setup.cache)
        .map_err(|e| format!("staging cache {:?}: {e}", case.setup.cache))?;
    let system_dirs: Vec<PathBuf> = case
        .setup
        .system_dirs
        .iter()
        .map(|name| fixtures_root.join("layouts").join(name))
        .collect();
    let lock_path = cache_dir.path().join("chtypes.lock");
    if let Some(lock_name) = &case.setup.lock {
        let src = fixtures_root
            .join("locks")
            .join(format!("{lock_name}.json"));
        std::fs::copy(&src, &lock_path)
            .map_err(|e| format!("staging lock {:?}: {e} (from {})", lock_name, src.display()))?;
    }
    if case.setup.before_index_rename_hook.is_some() {
        return Err("before_index_rename_hook is not implemented by this runner yet".to_string());
    }

    for (k, v) in &case.env {
        // SAFETY: this test runs single-threaded per case via
        // `catch_unwind`'s synchronous call, and the conformance binary
        // forks no other threads that read these during the window.
        unsafe { std::env::set_var(k, v) };
    }

    let result = ensure::ensure(
        &case.request.spelling,
        Options {
            platform: Some(case.request.platform.clone()),
            bases: Some(bases),
            cache_dir: Some(cache_dir.path().to_string_lossy().to_string()),
            system_dirs,
            offline: case.request.offline,
            frozen: case.request.frozen,
            lock_path: Some(lock_path.clone()),
            lock_write: case.request.lock_write,
            update: case.request.update,
            allow_unsigned: case.request.allow_unsigned,
            trust_test_keys: case.request.trust == "test",
            token: None,
            clock: None,
        },
    );

    for k in case.env.keys() {
        unsafe { std::env::remove_var(k) };
    }

    check_expectation(&case.expect, result)
}

fn check_expectation(
    expect: &Expect,
    result: Result<ensure::Resolved, ocifetch::error::Error>,
) -> Result<(), String> {
    match (expect.ok, result) {
        (true, Ok(resolved)) => {
            if let Some(want) = &expect.version {
                if &resolved.version != want {
                    return Err(format!("version = {:?}, want {want:?}", resolved.version));
                }
            }
            if let Some(want) = &expect.manifest {
                if &resolved.digests.manifest != want {
                    return Err(format!(
                        "manifest digest = {:?}, want {want:?}",
                        resolved.digests.manifest
                    ));
                }
            }
            if let Some(want) = &expect.library_sha256 {
                let predicate_sha = resolved
                    .predicate
                    .get("library_sha256")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if predicate_sha != want {
                    return Err(format!("library_sha256 = {predicate_sha:?}, want {want:?}"));
                }
            }
            if !expect.warnings.is_empty() && resolved.warnings.len() != expect.warnings.len() {
                return Err(format!(
                    "{} warning(s), want {}: got {:?}, want {:?}",
                    resolved.warnings.len(),
                    expect.warnings.len(),
                    resolved.warnings,
                    expect.warnings
                ));
            }
            if expect.lock_after.is_some() {
                // The expected-lock fixture comparison needs 0B's
                // `locks/<name>.json` fixtures to diff against; recorded as
                // a known gap (see this PR's MERGE NOTES).
                return Err(
                    "lock_after comparison is not implemented by this runner yet".to_string(),
                );
            }
            Ok(())
        }
        (false, Err(e)) => {
            let Some(want_code) = &expect.code else {
                return Err(format!(
                    "case expects failure with no code, got {e} ({})",
                    e.code()
                ));
            };
            if e.code() != want_code {
                return Err(format!("error code = {}, want {want_code}: {e}", e.code()));
            }
            Ok(())
        }
        (true, Err(e)) => Err(format!("expected ok, got {e} ({})", e.code())),
        (false, Ok(resolved)) => Err(format!(
            "expected failure {:?}, got ok (version {})",
            expect.code, resolved.version
        )),
    }
}

/// `setup.cache`: `"empty"` is a fresh temp directory; any other name is a
/// pre-seeded layout (`layouts/<name>/`, e.g. `layouts/oras-preseed/`)
/// copied into a fresh temp directory, since `ensure()` writes into its
/// cache and a fixture tree must never be mutated in place.
fn stage_cache(fixtures_root: &Path, name: &str) -> std::io::Result<tempdir::TempDir> {
    let dir = tempdir::TempDir::new("ocifetch-conformance-cache")?;
    if name != "empty" {
        copy_dir_all(&fixtures_root.join("layouts").join(name), dir.path())?;
    }
    Ok(dir)
}

fn copy_dir_all(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let dst_path = dst.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir_all(&entry.path(), &dst_path)?;
        } else {
            std::fs::copy(entry.path(), &dst_path)?;
        }
    }
    Ok(())
}

/// The scripted HTTP server (`scripts/fetch-v1/server.py --fixtures … --port
/// 0`), started once and reused for every `http`-transport case (plan §3.3:
/// "It starts … once"). Prints `LISTENING <port> <port2>` on stdout once
/// ready; `<port2>` is the second origin for cross-origin redirect cases.
struct ServerHandle {
    child: Child,
    port: u16,
    #[allow(dead_code)]
    second_port: u16,
}

impl ServerHandle {
    /// `None` if the script cannot be started (e.g. no `python3` on PATH) —
    /// callers then fail every `http`-transport case with a clear detail
    /// rather than aborting the whole suite, so `file`-transport cases still
    /// report their real verdicts.
    fn start(fixtures_root: &Path) -> Option<ServerHandle> {
        let script = locate_server_script()?;
        let mut child = Command::new("python3")
            .arg(script)
            .arg("--fixtures")
            .arg(fixtures_root)
            .arg("--port")
            .arg("0")
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .ok()?;
        let stdout = child.stdout.take()?;
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).ok()?;
        let mut parts = line.split_whitespace();
        if parts.next()? != "LISTENING" {
            return None;
        }
        let port: u16 = parts.next()?.parse().ok()?;
        let second_port: u16 = parts.next().unwrap_or("0").parse().unwrap_or(0);
        // Keep draining stdout so the child never blocks on a full pipe.
        std::thread::spawn(move || {
            let mut sink = std::io::sink();
            let _ = std::io::copy(&mut reader.into_inner(), &mut sink);
        });
        Some(ServerHandle {
            child,
            port,
            second_port,
        })
    }
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn locate_server_script() -> Option<PathBuf> {
    let candidate = repo_root()?.join("scripts/fetch-v1/server.py");
    candidate.is_file().then_some(candidate)
}

fn repo_root() -> Option<PathBuf> {
    // `rust/` is a direct child of the repository root.
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
}

/// `"rust1.87.0"`-shaped, read from the compiler that actually built this
/// binary (`rustc --version`) rather than this crate's `rust-version`
/// floor, so the `1.87` and `stable` matrix legs report distinctly.
fn toolchain_string() -> String {
    if let Ok(v) = std::env::var("CHTYPES_V1_TOOLCHAIN") {
        return v;
    }
    let output = Command::new("rustc").arg("--version").output();
    let version = output
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .and_then(|s| s.split_whitespace().nth(1).map(str::to_string));
    format!("rust{}", version.unwrap_or_else(|| "unknown".to_string()))
}

/// A minimal `TempDir`: create-on-new, remove-on-drop. `src/ocifetch` has no
/// dependency on a temp-dir crate, and adding one is outside this lane's
/// `Cargo.toml` scope (plan §4 "Lane Rust"), so the test provides its own.
mod tempdir {
    use std::path::{Path, PathBuf};

    pub struct TempDir(PathBuf);

    impl TempDir {
        pub fn new(prefix: &str) -> std::io::Result<TempDir> {
            let mut path = std::env::temp_dir();
            let unique = format!(
                "{prefix}-{}-{:?}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_nanos()
            );
            path.push(unique);
            std::fs::create_dir_all(&path)?;
            Ok(TempDir(path))
        }

        pub fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
}
