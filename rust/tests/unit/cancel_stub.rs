//! [`CancelToken`] over the generated ABI v2 stub: the six cancelable calls
//! (row, rows, insert, parse_block, `Filter::rows`, compile), canceled before,
//! during and never; one token across concurrent calls; independent tokens;
//! the live-handle count after; and a soak of racing cancels and returns that
//! must never fire a freed C token.
//!
//! The stub blocks a call whose first byte argument begins `!B:` until its
//! token fires, and answers its counters (`inflight`, `fire_after_free`) to the
//! probe `chs_type_validate("!C:")`, so every "during" case is sequenced on the
//! stub's own state, never on a sleep: the 1 ms wait between probe reads only
//! bounds how long a broken stub can hang a case.
//!
//! A unit test of the crate because it reads the raw echo
//! (`Schema::insert_raw`) to see the options document the call sent. The cases
//! share the stub's counters, so they run one at a time under `SERIAL`, on the
//! `ok-b` image, which no other unit test opens.
//!
//! `CHTYPES_ABI2_STUBS` unset: this suite skips LOUDLY by name and passes.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use serde_json::Value as Json;

use crate::library::{Library, open_image};
use crate::{
    CancelToken, CompileOptions, Error, EvalOptions, Filter, FilterOptions, Format, InsertOptions,
    Result, RowOptions, RowsOptions, Schema, status,
};

const ENV_STUBS: &str = "CHTYPES_ABI2_STUBS";
const STMT: &str = "CREATE TABLE t (x Int32) ENGINE = Memory";
/// The stub's block directive: a call whose body begins with it waits for its
/// token.
const BLOCK: &[u8] = b"!B:{\"x\":1}";
const BODY: &[u8] = br#"{"x":1}"#;

static SERIAL: Mutex<()> = Mutex::new(());

struct Fixture {
    _serial: MutexGuard<'static, ()>,
    lib: Arc<Library>,
    schema: Schema,
    filter: Filter,
}

fn fixture(case: &str) -> Option<Fixture> {
    let Some(dir) = std::env::var_os(ENV_STUBS).map(PathBuf::from) else {
        eprintln!(
            "SKIPPED (loudly): cancel_stub::{case} needs {ENV_STUBS} (the ABI v2 stub directory); nothing was exercised"
        );
        return None;
    };
    let serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let manifest: Json =
        serde_json::from_str(&std::fs::read_to_string(dir.join("stubs.json")).unwrap()).unwrap();
    let file = manifest["variants"]["ok-b"]["path"]
        .as_str()
        .expect("ok-b variant");
    let lib = open_image(&dir.join(Path::new(file).file_name().unwrap()), None, None)
        .expect("the ok-b stub opens");
    let schema = lib.compile_table(STMT, &CompileOptions::default()).unwrap();
    let filter = schema
        .compile_filter("x = 1", &FilterOptions::default())
        .unwrap();
    assert_eq!(
        live_cancels(&lib),
        0,
        "{case}: a token is live before the case"
    );
    Some(Fixture {
        _serial: serial,
        lib,
        schema,
        filter,
    })
}

/// The stub's cancel counters, through its probe.
fn probe(lib: &Library) -> (i64, i64) {
    let doc = lib.validate_type("!C:").expect("the cancel probe answers");
    let v: Json = serde_json::from_slice(doc.as_bytes()).expect("the probe is JSON");
    (
        v["inflight"].as_i64().expect("inflight"),
        v["fire_after_free"].as_i64().expect("fire_after_free"),
    )
}

fn live_cancels(lib: &Library) -> u64 {
    let live = lib.live_handles().unwrap();
    *live
        .get("chs_cancel")
        .unwrap_or_else(|| panic!("live_handles has no chs_cancel: {live:?}"))
}

/// Wait until the stub reads at least `want` calls blocked in its directive.
fn wait_inflight(lib: &Library, want: i64) {
    for _ in 0..5000 {
        if probe(lib).0 >= want {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
    panic!(
        "the stub never read {want} calls in flight (now {})",
        probe(lib).0
    );
}

#[derive(Clone, Copy, Debug)]
enum Call {
    Row,
    Rows,
    Insert,
    ParseBlock,
    FilterRows,
    Compile,
}

const CALLS: [Call; 6] = [
    Call::Row,
    Call::Rows,
    Call::Insert,
    Call::ParseBlock,
    Call::FilterRows,
    Call::Compile,
];

/// Make one cancelable call through the public API, its answer rendered so
/// two answers can be compared. `body` is the first byte argument: the body,
/// or the statement for a compile.
fn make(f: &Fixture, call: Call, body: &[u8], cancel: Option<CancelToken>) -> Result<String> {
    let fmt = Format::JsonEachRow;
    Ok(match call {
        Call::Row => format!(
            "{:?}",
            f.schema.row(
                fmt,
                body,
                &RowOptions {
                    cancel,
                    ..Default::default()
                }
            )?
        ),
        Call::Rows => {
            let mut batch = f.schema.rows(
                fmt,
                body,
                &RowsOptions {
                    cancel,
                    filter: Some(f.filter.clone()),
                    ..Default::default()
                },
            )?;
            // The stub's payload is its echo of every argument, the options
            // document (which names the C token's id) among them: the one
            // field that differs by construction. The test below reads it.
            batch.payload = None;
            format!("{batch:?}")
        }
        Call::Insert => format!(
            "{:?}",
            f.schema.insert(
                fmt,
                body,
                Some(fmt),
                &InsertOptions {
                    cancel,
                    ..Default::default()
                }
            )?
        ),
        Call::ParseBlock => {
            let block = f.schema.parse_block(
                fmt,
                body,
                &RowOptions {
                    cancel,
                    ..Default::default()
                },
            )?;
            format!("{:?}", f.filter.eval(&block)?)
        }
        Call::FilterRows => format!(
            "{:?}",
            f.filter.rows(
                fmt,
                body,
                &EvalOptions {
                    cancel,
                    ..Default::default()
                }
            )?
        ),
        Call::Compile => {
            let schema = f.lib.compile_table(
                body,
                &CompileOptions {
                    cancel,
                    ..Default::default()
                },
            )?;
            format!("{:?}", schema.describe()?)
        }
    })
}

/// The first byte argument a call is made with: its body, or its statement.
fn plain_arg(call: Call) -> &'static [u8] {
    match call {
        Call::Compile => STMT.as_bytes(),
        _ => BODY,
    }
}

#[track_caller]
fn assert_canceled(what: &str, r: Result<String>) {
    match r {
        Err(Error::Canceled(c)) => {
            assert_eq!(c.status, status::CANCELED, "{what}: {c:?}");
            assert_eq!(c.reason, "call-canceled", "{what}: {c:?}");
            assert_eq!(c.ch_code, 0, "{what}: {c:?}");
        }
        other => panic!("{what}: want Error::Canceled, got {other:?}"),
    }
}

#[test]
fn a_token_canceled_before_the_call_cancels_it_at_entry() {
    let Some(f) = fixture("a_token_canceled_before_the_call_cancels_it_at_entry") else {
        return;
    };
    for call in CALLS {
        let token = CancelToken::new();
        token.cancel();
        // The call is still made, with a pre-fired C token, and the library
        // answers it canceled at entry: no output.
        assert_canceled(
            &format!("{call:?}"),
            make(&f, call, plain_arg(call), Some(token)),
        );
    }
    assert_eq!(live_cancels(&f.lib), 0);
}

#[test]
fn a_token_canceled_during_the_call_cancels_it() {
    let Some(f) = fixture("a_token_canceled_during_the_call_cancels_it") else {
        return;
    };
    for call in CALLS {
        let before = probe(&f.lib).0;
        let token = CancelToken::new();
        let r = std::thread::scope(|s| {
            let t = token.clone();
            let worker = s.spawn(|| make(&f, call, BLOCK, Some(t)));
            wait_inflight(&f.lib, before + 1);
            token.cancel();
            worker.join().expect("the call's thread panicked")
        });
        assert_canceled(&format!("{call:?}"), r);
    }
    assert_eq!(live_cancels(&f.lib), 0);
}

/// A cancel that arrives after the library's last check point finds a
/// complete answer, and the call returns it: a complete answer is never turned
/// into a cancel. The stub's `!L:` body passes its last check point, waits for
/// the token, then answers its echo.
#[test]
fn cancel_after_last_check_point() {
    let Some(f) = fixture("cancel_after_last_check_point") else {
        return;
    };
    let late: &[u8] = b"!L:{\"x\":1}";
    let want = make(&f, Call::Row, late, None).expect("without a token");
    let before = probe(&f.lib).0;
    let token = CancelToken::new();
    let got = std::thread::scope(|s| {
        let t = token.clone();
        let worker = s.spawn(|| make(&f, Call::Row, late, Some(t)));
        wait_inflight(&f.lib, before + 1);
        token.cancel();
        worker.join().expect("the call's thread panicked")
    });
    assert_eq!(
        got.expect("a cancel after the last check point is not an error"),
        want
    );
    assert_eq!(probe(&f.lib).0, before);
    assert_eq!(live_cancels(&f.lib), 0);
}

#[test]
fn a_token_never_canceled_gives_the_answer_the_call_gives_without_one() {
    let Some(f) = fixture("a_token_never_canceled_gives_the_answer_the_call_gives_without_one")
    else {
        return;
    };
    for call in CALLS {
        let without = make(&f, call, plain_arg(call), None).expect("without a token");
        let with = make(&f, call, plain_arg(call), Some(CancelToken::new())).expect("with one");
        assert_eq!(with, without, "{call:?}");
    }
    // What the library received: the cancel id merged into the options the
    // call already builds; with no token, the document the call built before.
    let echo_options = |options: &InsertOptions| -> Json {
        let (doc, _) = f
            .schema
            .insert_raw(Format::JsonEachRow, BODY, None, options)
            .unwrap();
        let v: Json = serde_json::from_slice(&doc).unwrap();
        v["args"][7].clone()
    };
    let plain = echo_options(&InsertOptions {
        memory_limit_bytes: Some(5),
        ..Default::default()
    });
    assert_eq!(plain["len"], 26, "{plain}");
    let with = echo_options(&InsertOptions {
        memory_limit_bytes: Some(5),
        cancel: Some(CancelToken::new()),
        ..Default::default()
    });
    // {"cancel":"c<16 hex>","memory_limit_bytes":"5"}
    assert_eq!(with["len"], 26 + 29, "{with}");
    let head = with["head_hex"].as_str().expect("head_hex");
    let head: Vec<u8> = (0..head.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&head[i..i + 2], 16).unwrap())
        .collect();
    assert!(
        head.starts_with(b"{\"cancel\":\"c"),
        "{:?}",
        String::from_utf8_lossy(&head)
    );
    assert_eq!(live_cancels(&f.lib), 0);
}

#[test]
fn one_token_across_concurrent_calls_cancels_every_one() {
    let Some(f) = fixture("one_token_across_concurrent_calls_cancels_every_one") else {
        return;
    };
    let before = probe(&f.lib).0;
    let token = CancelToken::new();
    let results = std::thread::scope(|s| {
        let workers: Vec<_> = CALLS
            .iter()
            .map(|&call| {
                let t = token.clone();
                let f = &f;
                s.spawn(move || (call, make(f, call, BLOCK, Some(t))))
            })
            .collect();
        wait_inflight(&f.lib, before + CALLS.len() as i64);
        token.cancel();
        workers
            .into_iter()
            .map(|w| w.join().expect("a call's thread panicked"))
            .collect::<Vec<_>>()
    });
    assert_eq!(results.len(), CALLS.len());
    for (call, r) in results {
        assert_canceled(&format!("{call:?}"), r);
    }
    assert_eq!(live_cancels(&f.lib), 0);
}

#[test]
fn independent_tokens_cancel_only_their_own_call() {
    let Some(f) = fixture("independent_tokens_cancel_only_their_own_call") else {
        return;
    };
    let before = probe(&f.lib).0;
    let a = CancelToken::new();
    let b = CancelToken::new();
    std::thread::scope(|s| {
        let (ta, tb) = (a.clone(), b.clone());
        let fa = &f;
        let fb = &f;
        let wa = s.spawn(move || make(fa, Call::Row, BLOCK, Some(ta)));
        let wb = s.spawn(move || make(fb, Call::FilterRows, BLOCK, Some(tb)));
        wait_inflight(&f.lib, before + 2);
        a.cancel();
        assert_canceled("a", wa.join().expect("a's thread panicked"));
        // b's call is still blocked on its own token, which nothing fired.
        assert_eq!(probe(&f.lib).0, before + 1, "b's call is still in flight");
        assert!(!b.is_canceled());
        // And a call given b now answers in full.
        make(&f, Call::Row, BODY, Some(b.clone())).expect("b is not canceled");
        b.cancel();
        assert_canceled("b", wb.join().expect("b's thread panicked"));
    });
    assert_eq!(live_cancels(&f.lib), 0);
}

#[test]
fn racing_cancels_and_returns_never_fire_a_freed_token() {
    let Some(f) = fixture("racing_cancels_and_returns_never_fire_a_freed_token") else {
        return;
    };
    let (_, fire_after_free_before) = probe(&f.lib);
    let (mut answered, mut canceled) = (0u32, 0u32);
    std::thread::scope(|s| {
        let workers: Vec<_> = (0..8)
            .map(|i| {
                let f = &f;
                s.spawn(move || {
                    let (mut answered, mut canceled) = (0u32, 0u32);
                    for n in 0..300 {
                        let token = CancelToken::new();
                        let call = CALLS[(i + n) % CALLS.len()];
                        let r = std::thread::scope(|inner| {
                            let t = token.clone();
                            inner.spawn(move || t.cancel());
                            make(f, call, plain_arg(call), Some(token.clone()))
                        });
                        match r {
                            Ok(_) => answered += 1,
                            Err(Error::Canceled(_)) => canceled += 1,
                            Err(e) => panic!("{call:?}: {e:?}"),
                        }
                    }
                    (answered, canceled)
                })
            })
            .collect();
        for w in workers {
            let (a, c) = w.join().expect("a soak thread panicked");
            answered += a;
            canceled += c;
        }
    });
    eprintln!("cancel soak: {answered} answered, {canceled} canceled");
    assert_eq!(answered + canceled, 8 * 300);
    let (inflight, fire_after_free) = probe(&f.lib);
    assert_eq!(
        fire_after_free, fire_after_free_before,
        "a cancel fired a C token after it was freed"
    );
    assert_eq!(inflight, 0);
    // Back at baseline: every C token the soak created is freed.
    assert_eq!(live_cancels(&f.lib), 0);
}
