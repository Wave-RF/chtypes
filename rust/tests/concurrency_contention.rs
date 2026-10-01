//! Contention test for chtypes#364's per-image `RwLock` — the Rust twin of
//! Python's own `test_set_default_settings_excludes_the_row_path`.
//!
//! Needs a real artifact registry; SKIPS loudly, by name, on the real
//! stderr without one (same `$CHTYPES_REGISTRY` resolution as
//! `integration.rs`). Runs in CI's `artifacts` job, not ignored: this is the
//! regression guard that the exclusion still holds now that ordinary calls
//! run concurrently, not a benchmark.
//!
//! The shape: N reader threads hammer `rows()` on their OWN `Schema`
//! handles — one compiled `Schema` per thread, since a single handle is
//! single-threaded by the ABI regardless of the image lock — while one
//! writer thread repeatedly swaps the process-wide default settings
//! underneath them with [`chtypes::Library::set_default_settings`]. The
//! assertion is not "it did not panic" (a race that only sometimes corrupts
//! would still pass that): every one of the thousands of answers must be
//! byte-for-byte the answer the same call gives with nothing else running,
//! and both sides must actually have run under the per-image `RwLock` (a
//! shared/exclusive mode mix-up could otherwise starve one side entirely
//! and still report green).

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use chtypes::{Format, Library, NO_SETTINGS, Outcome, Registry};

fn announce(message: &str) {
    use std::io::Write;
    let _ = std::io::stderr().write_all(message.as_bytes());
    let _ = std::io::stderr().flush();
}

fn registry_dir() -> PathBuf {
    match std::env::var_os(chtypes::REGISTRY_ENV) {
        Some(dir) => PathBuf::from(dir),
        None => chtypes::default_registry_dir(),
    }
}

/// Open the registry and load one library, or `None` after announcing a
/// by-name skip on the real stderr.
fn primary() -> Option<Arc<Library>> {
    let dir = registry_dir();
    if !dir.is_dir() {
        announce(&format!(
            "\nSKIP concurrent_reads_survive_concurrent_default_settings_swaps: no chtypes \
             artifact registry at {} — fetch one with scripts/fetch.sh 25.8 \
             (docs/guides/fetch.md), or point ${} at a registry.\n",
            dir.display(),
            chtypes::REGISTRY_ENV
        ));
        return None;
    }
    let reg = match Registry::new(&dir) {
        Ok(r) => r,
        Err(e) => {
            announce(&format!(
                "\nSKIP concurrent_reads_survive_concurrent_default_settings_swaps: registry at \
                 {} did not open: {e}\n",
                dir.display()
            ));
            return None;
        }
    };
    let lines = reg.versions();
    if lines.is_empty() {
        announce(&format!(
            "\nSKIP concurrent_reads_survive_concurrent_default_settings_swaps: registry {} \
             holds no artifact\n",
            dir.display()
        ));
        return None;
    }
    let version = if lines.iter().any(|v| v == "25.8") {
        "25.8".to_string()
    } else {
        lines.last().cloned().expect("lines is non-empty")
    };
    match reg.for_version(&version) {
        Ok(lib) => Some(lib),
        Err(e) => {
            announce(&format!(
                "\nSKIP concurrent_reads_survive_concurrent_default_settings_swaps: {version} did \
                 not load: {e}\n"
            ));
            None
        }
    }
}

#[test]
fn concurrent_reads_survive_concurrent_default_settings_swaps() {
    let Some(lib) = primary() else { return };

    let body: &[u8] = b"{\"a\": 1}\n{\"a\": 2}\n{\"a\": 3}\n";

    // The oracle, computed first with nothing else running.
    let quiet = lib.compile("a UInt8").compile().expect("compile");
    let want: Vec<Vec<String>> = quiet
        .rows(Format::JsonEachRow, body, NO_SETTINGS)
        .expect("rows")
        .rows
        .iter()
        .map(|r| r.values.iter().map(|v| v.text.to_string()).collect())
        .collect();
    assert_eq!(
        want,
        vec![
            vec!["1".to_string()],
            vec!["2".to_string()],
            vec!["3".to_string()],
        ]
    );
    drop(quiet);

    let n_readers = 8;
    let deadline = Instant::now() + Duration::from_secs(2);
    let reads: Vec<AtomicU64> = (0..n_readers).map(|_| AtomicU64::new(0)).collect();
    let swaps = AtomicU64::new(0);
    let failed = AtomicBool::new(false);

    std::thread::scope(|s| {
        for slot in 0..n_readers {
            let lib = Arc::clone(&lib);
            let reads = &reads;
            let failed = &failed;
            let want = &want;
            s.spawn(move || {
                // One compiled handle per thread: a single chs_schema * is
                // single-threaded by the ABI, lock or no lock.
                let schema = match lib.compile("a UInt8").compile() {
                    Ok(schema) => schema,
                    Err(e) => {
                        eprintln!("reader {slot}: compile failed: {e}");
                        failed.store(true, Ordering::SeqCst);
                        return;
                    }
                };
                while Instant::now() < deadline {
                    match schema.rows(Format::JsonEachRow, body, NO_SETTINGS) {
                        Ok(got) => {
                            let got_values: Vec<Vec<String>> = got
                                .rows
                                .iter()
                                .map(|r| r.values.iter().map(|v| v.text.to_string()).collect())
                                .collect();
                            if got.outcome != Outcome::Accepted || got_values != *want {
                                eprintln!(
                                    "reader {slot}: mismatch under contention: outcome={:?} \
                                     values={got_values:?} want={want:?}",
                                    got.outcome
                                );
                                failed.store(true, Ordering::SeqCst);
                                return;
                            }
                            reads[slot].fetch_add(1, Ordering::Relaxed);
                        }
                        Err(e) => {
                            eprintln!("reader {slot}: rows() failed under contention: {e}");
                            failed.store(true, Ordering::SeqCst);
                            return;
                        }
                    }
                }
            });
        }
        s.spawn(|| {
            let mut toggle = false;
            while Instant::now() < deadline {
                // Both payloads are inert for an `a UInt8` row; what is under
                // test is the REPLACEMENT of the seeded list, not its
                // content — exactly Python's own reasoning.
                let settings: &[(&str, &str)] = if toggle {
                    &[("chtypes_default_eval_wall_nanos", "2000000000")]
                } else {
                    &[]
                };
                if let Err(e) = lib.set_default_settings(settings) {
                    eprintln!("writer: set_default_settings failed: {e}");
                    failed.store(true, Ordering::SeqCst);
                    return;
                }
                toggle = !toggle;
                swaps.fetch_add(1, Ordering::Relaxed);
                std::thread::sleep(Duration::from_millis(1));
            }
        });
    });

    // Leave the process as it was found.
    lib.set_default_settings(NO_SETTINGS)
        .expect("reset default settings");

    assert!(
        !failed.load(Ordering::SeqCst),
        "a reader or the writer hit an error or a mismatch under contention"
    );
    assert!(
        reads.iter().all(|r| r.load(Ordering::Relaxed) > 0),
        "a reader never ran: {:?}",
        reads
            .iter()
            .map(|r| r.load(Ordering::Relaxed))
            .collect::<Vec<_>>()
    );
    let total_reads: u64 = reads.iter().map(|r| r.load(Ordering::Relaxed)).sum();
    assert!(
        total_reads > 200,
        "too few reads to be contention: {total_reads}"
    );
    let total_swaps = swaps.load(Ordering::Relaxed);
    assert!(
        total_swaps > 50,
        "the writer was starved: {total_swaps} swaps"
    );
    println!(
        "\ncontention measured: {total_reads} batch reads across {n_readers} threads against \
         {total_swaps} default-settings swaps, 0 mismatches, 0 panics, 0 deadlocks\n"
    );
}
