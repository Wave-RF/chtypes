//! Concurrency throughput probe for chtypes#364 — "Rust's per-image mutex:
//! MEASURE first, then fix only if it doesn't scale."
//!
//! IGNORED by default. Run explicitly:
//!
//!     cargo test --release --test concurrency_throughput -- --ignored --nocapture
//!
//! Needs a real artifact registry, resolved exactly as `integration.rs` does
//! (`$CHTYPES_REGISTRY`, else the per-user cache); it SKIPS loudly without
//! one rather than passing silently.
//!
//! The shape: open ONE image, compile N independent `Schema` handles on it
//! — distinct handles of one version, which the C contract allows to run in
//! parallel — and run a fixed CPU-bound workload on each for a fixed wall-
//! clock budget, at N=1 and N=8, printing measured throughput and the ratio.
//! The workload is `Schema::parse_block` (`chs_block_parse`): pure parse +
//! coerce, no I/O, the same entry point docs/guides/multi-version.md's own
//! "ParseBlock throughput scales with handle count again" note (chtypes#78)
//! measures N1/N8 against; that note gives no exact row shape to reproduce
//! byte-for-byte, so this uses its own small multi-typed DDL instead of a
//! single column.
//!
//! Every number this prints is tied to an artifact's own identity — line,
//! `chtypes_build`, platform, core count — never a bare ratio with nothing
//! behind it (chtypes#190: a registry PATH is not a fingerprint).
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use chtypes::{Format, Library, NO_SETTINGS, Registry};

fn registry_dir() -> PathBuf {
    match std::env::var_os(chtypes::REGISTRY_ENV) {
        Some(dir) => PathBuf::from(dir),
        None => chtypes::default_registry_dir(),
    }
}

/// `chtypes_build` and `abi_revision`, read straight off the manifest next to
/// the loaded library. Neither field is on [`chtypes::Manifest`] (it carries
/// only what the loader itself needs), so this reads the file a second time
/// as plain JSON — exactly what `scripts/lib/provenance.py` does, for the
/// same reason: the number this test prints must be traceable to a build, or
/// it is not a measurement.
fn build_identity(lib: &Library) -> (String, String) {
    let dir = lib
        .path()
        .parent()
        .expect("a loaded library has a parent directory");
    let raw = std::fs::read_to_string(dir.join("manifest.json")).unwrap_or_default();
    let doc: serde_json::Value = serde_json::from_str(&raw).unwrap_or(serde_json::Value::Null);
    let build = doc
        .get("chtypes_build")
        .and_then(|v| v.as_str())
        .unwrap_or("?")
        .to_string();
    let abi = doc
        .get("abi_revision")
        .map(|v| v.to_string())
        .unwrap_or_else(|| "?".to_string());
    (build, abi)
}

/// One fixed CPU-bound unit of work on `schema`, repeated until `deadline`,
/// counting completions in `counter`. `schema` is OWNED by the calling
/// thread, never shared: `Schema` is `Send` and deliberately `!Sync`, so
/// `&Schema` cannot even be passed across the `thread::scope` boundary — the
/// type system enforces the same rule this probe is measuring.
fn run_until(schema: chtypes::Schema, body: &[u8], deadline: Instant, counter: &AtomicU64) {
    while Instant::now() < deadline {
        let block = schema
            .parse_block(Format::JsonEachRow, body, NO_SETTINGS)
            .expect("parse_block");
        drop(block);
        counter.fetch_add(1, Ordering::Relaxed);
    }
}

/// Compile `n` independent handles on `lib` and run them for `run_time`,
/// each on its own thread, returning completed `parse_block` calls per
/// second across all `n` threads together.
fn throughput_at(lib: &Arc<Library>, ddl: &str, body: &[u8], n: usize, run_time: Duration) -> f64 {
    let schemas: Vec<chtypes::Schema> = (0..n)
        .map(|_| lib.compile(ddl).compile().expect("compile"))
        .collect();
    let counter = AtomicU64::new(0);
    // A plain reference, taken ONCE outside the loop: `&AtomicU64` is `Copy`,
    // so each `move` closure below captures its own copy of the reference
    // rather than the loop re-capturing (and moving away) `counter` itself
    // on every iteration.
    let counter_ref = &counter;
    let deadline = Instant::now() + run_time;
    let start = Instant::now();
    std::thread::scope(|s| {
        for schema in schemas {
            s.spawn(move || run_until(schema, body, deadline, counter_ref));
        }
    });
    let elapsed = start.elapsed().as_secs_f64();
    counter.load(Ordering::Relaxed) as f64 / elapsed
}

#[test]
#[ignore = "manual measurement only (chtypes#364) — run with: cargo test --release --test concurrency_throughput -- --ignored --nocapture"]
fn concurrency_scales_with_handle_count() {
    let dir = registry_dir();
    if !dir.is_dir() {
        eprintln!(
            "\nSKIP concurrency_scales_with_handle_count: no artifact registry at {} — fetch one \
             with scripts/fetch.sh 25.8 (docs/guides/fetch.md), or point ${} at a registry.\n",
            dir.display(),
            chtypes::REGISTRY_ENV
        );
        return;
    }
    let reg = match Registry::new(&dir) {
        Ok(r) => r,
        Err(e) => {
            eprintln!(
                "\nSKIP concurrency_scales_with_handle_count: registry at {} did not open: {e}\n",
                dir.display()
            );
            return;
        }
    };
    let lines = reg.versions();
    if lines.is_empty() {
        eprintln!(
            "\nSKIP concurrency_scales_with_handle_count: registry {} holds no artifact\n",
            dir.display()
        );
        return;
    }
    // "25.8" when present, same precedent as integration.rs's `primary()`:
    // the version every other observation in docs/reference/ was captured
    // on; else the newest loaded line.
    let version = if lines.iter().any(|v| v == "25.8") {
        "25.8".to_string()
    } else {
        lines.last().cloned().expect("lines is non-empty")
    };
    let lib = reg.for_version(&version).expect("the selected line loads");

    let (build, abi) = build_identity(&lib);
    let platform = chtypes::host_platform();
    let cores = std::thread::available_parallelism()
        .map(std::num::NonZeroUsize::get)
        .unwrap_or(0);

    let ddl = "a UInt8, b String, c Nullable(Int64), d DateTime, e Float64";
    let body = b"{\"a\":1,\"b\":\"x\",\"c\":2,\"d\":\"2024-01-01 00:00:00\",\"e\":1.5}\n\
{\"a\":2,\"b\":\"y\",\"c\":null,\"d\":\"2024-01-02 00:00:00\",\"e\":2.5}\n\
{\"a\":3,\"b\":\"z\",\"c\":4,\"d\":\"2024-01-03 00:00:00\",\"e\":3.5}\n";
    let run_time = Duration::from_millis(1500);

    let t1 = throughput_at(&lib, ddl, body, 1, run_time);
    let t8 = throughput_at(&lib, ddl, body, 8, run_time);
    let ratio = t8 / t1;

    println!(
        "\nconcurrency probe (chtypes#364): line={version} chtypes_build={build} abi_revision={abi} \
         platform={platform} runner_cores={cores}\n\
         N=1  {t1:.0} parse_block/s\n\
         N=8  {t8:.0} parse_block/s\n\
         ratio N8/N1 = {ratio:.2}x\n"
    );
}
