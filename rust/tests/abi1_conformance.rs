//! The Rust leg of `v1-abi-conformance`: every case in
//! `tests/fixtures/abi-v1/cases.json`, run against the stub libraries
//! `scripts/abi-v1/build-stubs.sh` built, through this binding's generated
//! invoke-by-name dispatcher and hand-written loader.
//!
//! WHY THIS FILE RE-DECLARES `rust/src/abi1`'s MODULES WITH `#[path]`.
//! `rust/src/lib.rs` declares `abi1` as `mod abi1;` (not `pub`): it is not
//! part of this crate's public API, same as every v0 internal module. An
//! integration test under `rust/tests/` is a SEPARATE crate with only this
//! crate's PUBLIC items visible — `rust/tests/abi_revision.rs` reaches only
//! `chtypes::Registry`, for instance — so `chtypes::abi1::*` is not a path
//! this file can use. Instead the three files are re-declared here with
//! `#[path]`, so they are compiled twice (once, unreached, inside the
//! library; once here, where this test actually calls them) from the
//! IDENTICAL source — nothing is duplicated by hand, and `decls.rs`'s and
//! `invoke_gen.rs`'s own `use super::decls::...` resolve the same way in
//! both compilations, since both place `decls` as a sibling of `invoke_gen`
//! one level down from a crate root.
//!
//! WHAT RUNS. `CHTYPES_ABI1_STUBS` unset: this test prints a loud skip on
//! stderr and passes trivially (every test here skips loudly by name; it
//! never passes silently and never fails, per the lane brief). Set: the
//! `"ok"` stub is loaded once through the real loader (proving the loader
//! itself, not just the dispatcher), and every `handshake`/`echo`/`status`
//! case in `cases.json` runs against that one `Api`; every `loader` case
//! loads its OWN named stub variant fresh and checks the loader's refusal
//! reason (or acceptance) against `_stubshared.py`'s plan. A
//! `spec/abi-v1/schema/report.schema.json`-shaped report is written to
//! `CHTYPES_ABI1_REPORT` before the final assertion, so a partial result is
//! visible even when some cases fail.
//!
//! A `loader`-kind case may carry an `"os"` field (`cases.schema.json`,
//! round 2): present only when the case's expectation is platform-specific
//! (`loader.ctor-marker.linux`/`.darwin`: D3's glibc-floor step is
//! Linux-only, so the same stub is refused on Linux and loads cleanly on
//! darwin). A case whose `os` does not match this leg is OMITTED from the
//! report entirely (`case_applies_on_this_os`) — never recorded as a pass,
//! which the PM's ruling calls out explicitly: "a pass that never ran" is
//! exactly the failure pattern this project refuses elsewhere. `parity.py`
//! and `cases.schema.json` are being updated, in a follow-up F-B PR, to
//! expect that an os-mismatched case is absent from a leg's report.

use std::collections::{BTreeMap, HashMap};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

#[path = "../src/abi1/decls.rs"]
mod decls;
#[path = "../src/abi1/invoke_gen.rs"]
mod invoke_gen;
#[path = "../src/abi1/loader.rs"]
mod loader;

use decls::Api;
use invoke_gen::{MintedHandle, Outcome, ResolvedArg, invoke};
use loader::{LoadError, LoadInput, Loaded};

/// A loud announcement on the REAL stderr, uncaptured by `cargo test` even
/// without `--nocapture` — the same mechanism `rust/tests/fetch.rs` and
/// `rust/tests/abi_revision.rs` use for their own skip messages.
fn announce(message: &str) {
    let _ = std::io::stderr().write_all(message.as_bytes());
    let _ = std::io::stderr().write_all(b"\n");
    let _ = std::io::stderr().flush();
}

// ------------------------------------------------------------- case parsing

#[derive(Debug)]
enum CaseArgSpec {
    Int(i64),
    Bytes(Vec<u8>),
    NullHandle,
    Handle(Box<CaseCallSpec>),
    /// A lifecycle step's named handle, bound earlier by a `let` step.
    Ref(String),
}

#[derive(Debug)]
struct CaseCallSpec {
    fn_name: String,
    args: Vec<CaseArgSpec>,
}

fn decode_hex(s: &str) -> Result<Vec<u8>, String> {
    if s.len() & 1 != 0 {
        return Err(format!("odd-length hex string {s:?}"));
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(|e| e.to_string()))
        .collect()
}

fn parse_arg(v: &Value) -> Result<CaseArgSpec, String> {
    let obj = v
        .as_object()
        .ok_or_else(|| format!("a case argument must be an object, got {v}"))?;
    if let Some(i) = obj.get("int") {
        return Ok(CaseArgSpec::Int(
            i.as_i64().ok_or("\"int\" must be an integer")?,
        ));
    }
    if let Some(h) = obj.get("bytes_hex") {
        let s = h.as_str().ok_or("\"bytes_hex\" must be a string")?;
        return Ok(CaseArgSpec::Bytes(decode_hex(s)?));
    }
    if obj.contains_key("null_handle") {
        return Ok(CaseArgSpec::NullHandle);
    }
    if let Some(r) = obj.get("ref") {
        return Ok(CaseArgSpec::Ref(
            r.as_str().ok_or("\"ref\" must be a string")?.to_string(),
        ));
    }
    if let Some(h) = obj.get("handle") {
        return Ok(CaseArgSpec::Handle(Box::new(parse_call(h)?)));
    }
    Err(format!("unrecognized case argument shape: {v}"))
}

fn parse_call(v: &Value) -> Result<CaseCallSpec, String> {
    let fn_name = v
        .get("fn")
        .and_then(Value::as_str)
        .ok_or("a call needs \"fn\"")?
        .to_string();
    let raw_args = v
        .get("args")
        .and_then(Value::as_array)
        .ok_or("a call needs \"args\"")?;
    let args = raw_args
        .iter()
        .map(parse_arg)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(CaseCallSpec { fn_name, args })
}

// --------------------------------------------------------- recursive calls

/// Resolve one case argument, recursively minting any nested `{"handle": …}`
/// recipe first. Every `out_handle` any nested call produces (not only the
/// one fed onward) is appended to `minted`, so the top-level case can free
/// everything exactly once at the end (D2: any free order is safe).
fn resolve_arg(
    api: &Api,
    spec: &CaseArgSpec,
    minted: &mut Vec<MintedHandle>,
    refs: &HashMap<String, MintedHandle>,
) -> Result<ResolvedArg, String> {
    match spec {
        CaseArgSpec::Ref(name) => refs
            .get(name)
            .map(MintedHandle::as_resolved_arg)
            .ok_or_else(|| format!("ref {name:?} names no bound handle")),
        CaseArgSpec::Int(i) => Ok(ResolvedArg::Int(*i)),
        CaseArgSpec::Bytes(b) => Ok(ResolvedArg::Bytes(b.clone())),
        CaseArgSpec::NullHandle => Ok(ResolvedArg::Null),
        CaseArgSpec::Handle(call) => {
            let outcome = resolve_and_call(api, call, minted, refs)?;
            let Outcome::Call {
                status, outputs, ..
            } = outcome
            else {
                return Err(format!(
                    "{}: a handle recipe must be a status call",
                    call.fn_name
                ));
            };
            if status != "CHS_OK" {
                return Err(format!(
                    "{}: handle recipe did not return CHS_OK ({status})",
                    call.fn_name
                ));
            }
            let handle = outputs.get("out").copied().ok_or_else(|| {
                format!("{}: no \"out\" output to mint a handle from", call.fn_name)
            })?;
            Ok(handle.as_resolved_arg())
        }
    }
}

/// Resolve every argument of `call` (recursively), then dispatch it through
/// the generated `invoke`. Every `out_handle` the call itself produces is
/// also appended to `minted`.
fn resolve_and_call(
    api: &Api,
    call: &CaseCallSpec,
    minted: &mut Vec<MintedHandle>,
    refs: &HashMap<String, MintedHandle>,
) -> Result<Outcome, String> {
    let mut args = Vec::with_capacity(call.args.len());
    for a in &call.args {
        args.push(resolve_arg(api, a, minted, refs)?);
    }
    // SAFETY: every `ResolvedArg::Handle` above was minted by this same
    // `api` (`resolve_arg` only ever builds one from an `outputs` entry this
    // same api's own `invoke` just returned), and `api` completed loader
    // step 6 before any case runs (see `abi1_conformance`, below).
    let outcome = unsafe { invoke(api, &call.fn_name, &args) }?;
    if let Outcome::Call { outputs, .. } = &outcome {
        for h in outputs.values() {
            minted.push(*h);
        }
    }
    Ok(outcome)
}

/// Free one minted handle through this same `api`'s own free function.
fn free_handle(api: &Api, h: MintedHandle) {
    // SAFETY: every `MintedHandle` reaching here was minted by this same
    // `api`, read (a `chs_buf`) or passed on as a borrowed `handle` argument
    // and never otherwise used, and is freed here exactly once per case.
    unsafe {
        match h {
            MintedHandle::Buf(p) => (api.chs_buf_free)(p),
            MintedHandle::Schema(p) => (api.chs_schema_free)(p),
            MintedHandle::Filter(p) => (api.chs_filter_free)(p),
            MintedHandle::Block(p) => (api.chs_block_free)(p),
        }
    }
}

/// Read (never free) a minted `chs_buf`'s bytes.
fn read_buf_bytes(api: &Api, h: &MintedHandle) -> Result<Vec<u8>, String> {
    let MintedHandle::Buf(p) = h else {
        return Err(format!(
            "expected a chs_buf output, found a {}",
            h.kind_name()
        ));
    };
    // SAFETY: `p` is a live `chs_buf *` this same `api` minted and has not
    // yet been freed (freeing happens only after every case in this run has
    // read what it needs).
    unsafe {
        let data = (api.chs_buf_data)(*p as *const decls::ChsBuf);
        let len = (api.chs_buf_len)(*p as *const decls::ChsBuf);
        if data.is_null() || len == 0 {
            Ok(Vec::new())
        } else {
            Ok(std::slice::from_raw_parts(data, len).to_vec())
        }
    }
}

// --------------------------------------------------------------- matching

/// `expect` is a SUBSET of `got`: every key `expect` names (recursively)
/// must be present in `got` with an equal value; `got` may carry extra keys
/// `expect` does not mention (a minted handle's unpredictable `"id"`, next
/// to its `"kind"` — see `emit/cases.py`'s own docstring on why a handle
/// recipe's expected echo never names `id`). A scalar or string compares
/// exactly; `null` matches only `null`.
fn subset_match(expect: &Value, got: &Value) -> Result<(), String> {
    match expect {
        Value::Object(map) => {
            let got_map = got
                .as_object()
                .ok_or_else(|| format!("expected an object, got {got}"))?;
            for (k, v) in map {
                let got_v = got_map
                    .get(k)
                    .ok_or_else(|| format!("missing key {k:?} in {got}"))?;
                subset_match(v, got_v).map_err(|e| format!("{k}.{e}"))?;
            }
            Ok(())
        }
        Value::Array(items) => {
            let got_items = got
                .as_array()
                .ok_or_else(|| format!("expected an array, got {got}"))?;
            if items.len() != got_items.len() {
                return Err(format!(
                    "array length: want {}, got {}",
                    items.len(),
                    got_items.len()
                ));
            }
            for (i, (e, g)) in items.iter().zip(got_items).enumerate() {
                subset_match(e, g).map_err(|err| format!("[{i}]{err}"))?;
            }
            Ok(())
        }
        other => {
            if other == got {
                Ok(())
            } else {
                Err(format!(": want {other}, got {got}"))
            }
        }
    }
}

fn check_handshake(outcome: &Outcome, expect: &Value) -> Result<(), String> {
    if let Some(want) = expect.get("int") {
        let want_i = want.as_i64().ok_or("expect.int is not an integer")?;
        return match outcome {
            Outcome::Int(got) if *got == want_i => Ok(()),
            Outcome::Int(got) => Err(format!("want int {want_i}, got {got}")),
            _ => Err("expected an int outcome".to_string()),
        };
    }
    if let Some(sub) = expect.get("contains") {
        let sub_s = sub.as_str().ok_or("expect.contains is not a string")?;
        return match outcome {
            Outcome::Str(s) if s.contains(sub_s) => Ok(()),
            Outcome::Str(s) => Err(format!("{s:?} does not contain {sub_s:?}")),
            _ => Err("expected a string outcome".to_string()),
        };
    }
    if expect.get("non_empty").and_then(Value::as_bool) == Some(true) {
        return match outcome {
            Outcome::Str(s) if !s.is_empty() => Ok(()),
            Outcome::Str(s) => Err(format!("expected non-empty, got {s:?}")),
            _ => Err("expected a string outcome".to_string()),
        };
    }
    Err(format!("unrecognized handshake expectation: {expect}"))
}

fn check_call(api: &Api, outcome: &Outcome, expect: &Value) -> Result<(), String> {
    let Outcome::Call {
        status,
        error,
        outputs,
    } = outcome
    else {
        return Err("expected a status-call outcome".to_string());
    };
    let want_status = expect
        .get("status")
        .and_then(Value::as_str)
        .ok_or("expect.status is missing")?;
    if *status != want_status {
        return Err(format!("status: want {want_status}, got {status}"));
    }
    if let Some(want_err) = expect.get("error") {
        let want_obj = want_err
            .as_object()
            .ok_or("expect.error is not an object")?;
        let got_err = error
            .as_ref()
            .ok_or("expected an error; the call set none")?;
        for (field, want_v) in want_obj {
            let got_v = match field.as_str() {
                "ch_code" => Value::from(got_err.ch_code),
                "ch_name" => Value::from(got_err.ch_name.clone()),
                "message" => Value::from(got_err.message.clone()),
                "column" => Value::from(got_err.column.clone()),
                other => return Err(format!("expect.error names an unknown field {other:?}")),
            };
            if &got_v != want_v {
                return Err(format!("error.{field}: want {want_v}, got {got_v}"));
            }
        }
    }
    if let Some(want_outputs) = expect.get("outputs") {
        let want_obj = want_outputs
            .as_object()
            .ok_or("expect.outputs is not an object")?;
        for (name, want_shape) in want_obj {
            let minted = outputs
                .get(name)
                .ok_or_else(|| format!("no output named {name:?}"))?;
            let bytes = read_buf_bytes(api, minted).map_err(|e| format!("outputs.{name}: {e}"))?;
            let got_json: Value = serde_json::from_slice(&bytes).map_err(|e| {
                format!(
                    "outputs.{name}: not valid JSON ({e}): {:?}",
                    String::from_utf8_lossy(&bytes)
                )
            })?;
            subset_match(want_shape, &got_json).map_err(|e| format!("outputs.{name}{e}"))?;
        }
    }
    // A "document" case: each named output parses as STRICT UTF-8 JSON
    // (serde_json::from_slice refuses invalid UTF-8) and equals the expected
    // document exactly, every member and no more, so a member present in
    // both its plain and its _b64 form fails.
    if let Some(want_docs) = expect.get("documents") {
        let want_obj = want_docs
            .as_object()
            .ok_or("expect.documents is not an object")?;
        if want_obj.is_empty() {
            return Err("expect.documents names no output".to_string());
        }
        for (name, want_doc) in want_obj {
            let minted = outputs
                .get(name)
                .ok_or_else(|| format!("no output named {name:?}"))?;
            let bytes =
                read_buf_bytes(api, minted).map_err(|e| format!("documents.{name}: {e}"))?;
            let got_doc: Value = serde_json::from_slice(&bytes).map_err(|e| {
                format!(
                    "documents.{name}: not valid UTF-8 JSON ({e}): {:?}",
                    String::from_utf8_lossy(&bytes)
                )
            })?;
            if &got_doc != want_doc {
                return Err(format!("documents.{name}: want {want_doc}, got {got_doc}"));
            }
        }
    }
    Ok(())
}

// ------------------------------------------------------------------ loader

fn predicate_map(v: &Value) -> Result<serde_json::Map<String, Value>, String> {
    v.as_object()
        .cloned()
        .ok_or_else(|| format!("predicate is not an object: {v}"))
}

/// Resolve one stub variant's library, BY FILE NAME, under `stubs_dir` — the
/// real `CHTYPES_ABI1_STUBS` directory for THIS job. `stubs.json`'s own
/// `path` field was written by the (separate) `v1-abi-stubs` build job, under
/// ITS OWN `$RUNNER_TEMP`; once the artifact crosses jobs (download-artifact
/// into this job's `${{ runner.temp }}/abi1-stubs`, a different path on a
/// different runner), that field's directory component is stale — only its
/// file name still names a real file here. Taking the file name alone is
/// also forward-compatible with a future `stubs.json` that records a
/// relative file name directly (F-B tracking this under a separate fix).
fn load_variant(stubs_dir: &Path, variant: &Value) -> Result<Loaded, LoadError> {
    let path_str = variant
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let file_name = Path::new(path_str).file_name().unwrap_or(path_str.as_ref());
    let path = stubs_dir.join(file_name);
    let predicate =
        predicate_map(variant.get("predicate").unwrap_or(&Value::Null)).unwrap_or_default();
    loader::load(LoadInput {
        library_path: &path,
        predicate,
        timezone: &[],
    })
}

/// Loader steps 1-6's exact reason word (never the `Display`-formatted
/// sentence) a `loader`-kind case's `expect.reason` compares against.
fn run_loader_case(
    stubs_dir: &Path,
    case: &Value,
    variants: &serde_json::Map<String, Value>,
) -> Result<(), String> {
    let variant_name = case
        .get("variant")
        .and_then(Value::as_str)
        .ok_or("loader case has no \"variant\"")?;
    let expect = case.get("expect").ok_or("loader case has no \"expect\"")?;
    let want_reason = expect
        .get("reason")
        .and_then(Value::as_str)
        .ok_or("expect.reason is missing")?
        .to_string();

    let variant = variants
        .get(variant_name)
        .ok_or_else(|| format!("stubs.json has no variant named {variant_name:?}"))?;
    match load_variant(stubs_dir, variant) {
        Ok(_loaded) => {
            if want_reason == "accepted" {
                Ok(())
            } else {
                Err(format!("want refusal {want_reason:?}, the library loaded"))
            }
        }
        Err(LoadError::Initialize(e)) => Err(format!(
            "want {want_reason:?}, chs_initialize failed ({} / {}): {}",
            e.status, e.class, e.message
        )),
        Err(LoadError::Refused(refusal)) => {
            if refusal.reason == want_reason {
                Ok(())
            } else {
                Err(format!(
                    "want refusal {want_reason:?}, got {:?} ({refusal})",
                    refusal.reason
                ))
            }
        }
    }
}

// --------------------------------------------------------------- generic

fn run_generic_case(api: &Api, case: &Value, kind: &str, expect: &Value) -> Result<(), String> {
    let fn_name = case
        .get("fn")
        .and_then(Value::as_str)
        .ok_or("case has no \"fn\"")?;
    if kind == "handshake" {
        // SAFETY: a handshake/tombstone call takes no arguments.
        let outcome = unsafe { invoke(api, fn_name, &[]) }?;
        return check_handshake(&outcome, expect);
    }
    let raw_args = case
        .get("args")
        .and_then(Value::as_array)
        .ok_or("case has no \"args\"")?;
    let specs = raw_args
        .iter()
        .map(parse_arg)
        .collect::<Result<Vec<_>, _>>()?;
    let call = CaseCallSpec {
        fn_name: fn_name.to_string(),
        args: specs,
    };
    let mut minted = Vec::new();
    let outcome = resolve_and_call(api, &call, &mut minted, &HashMap::new());
    let verdict = match &outcome {
        Ok(o) => check_call(api, o, expect),
        Err(e) => Err(e.clone()),
    };
    for h in minted {
        free_handle(api, h);
    }
    verdict
}

// ------------------------------------------------- lifecycle and concurrent

/// `chs_live_handles`'s per-kind counts. The document's own buffer is read
/// and freed here, after the counts were taken (the document is "taken before
/// the document's own buffer exists").
fn live_counts(api: &Api) -> Result<BTreeMap<String, i64>, String> {
    let mut out: *mut decls::ChsBuf = std::ptr::null_mut();
    let mut err: *mut decls::ChsError = std::ptr::null_mut();
    // SAFETY: `api` completed loader step 7; both outputs are this call's own
    // locals, and the buffer is freed through this same image below.
    let status = unsafe { (api.chs_live_handles)(&mut out, &mut err) };
    if !err.is_null() {
        // SAFETY: a non-null `err` was just handed over by this call.
        unsafe { (api.chs_error_free)(err) };
    }
    if status != 0 {
        return Err(format!("chs_live_handles returned status {status}"));
    }
    let bytes = read_buf_bytes(api, &MintedHandle::Buf(out));
    free_handle(api, MintedHandle::Buf(out));
    let doc: Value = serde_json::from_slice(&bytes?).map_err(|e| e.to_string())?;
    let obj = doc
        .as_object()
        .ok_or("chs_live_handles: not a JSON object")?;
    obj.iter()
        .map(|(k, v)| {
            v.as_i64()
                .map(|n| (k.clone(), n))
                .ok_or_else(|| format!("chs_live_handles: {k} is not an integer"))
        })
        .collect()
}

fn free_outputs(api: &Api, outcome: &Outcome) {
    if let Outcome::Call { outputs, .. } = outcome {
        for h in outputs.values() {
            free_handle(api, *h);
        }
    }
}

/// `lifecycle`: ordered steps with no other case in flight. `let` binds the
/// handle a call produces; `call` + `expect` is checked as an echo case is;
/// `free` releases a bound handle; `live_delta` compares `chs_live_handles`
/// with the counts read when the case began.
fn run_lifecycle_case(api: &Api, case: &Value) -> Result<(), String> {
    let steps = case
        .get("steps")
        .and_then(Value::as_array)
        .ok_or("lifecycle case has no \"steps\"")?;
    let base = live_counts(api)?;
    let mut refs: HashMap<String, MintedHandle> = HashMap::new();
    let mut verdict: Result<(), String> = Ok(());
    for (i, step) in steps.iter().enumerate() {
        let r = run_lifecycle_step(api, step, &base, &mut refs);
        if let Err(e) = r {
            verdict = Err(format!("step {i}: {e}"));
            break;
        }
    }
    // Every handle a step binds is freed by a step; on a failed case, free
    // the leftovers so a later case's counts are not skewed.
    for (_, h) in refs.drain() {
        free_handle(api, h);
    }
    verdict
}

fn run_lifecycle_step(
    api: &Api,
    step: &Value,
    base: &BTreeMap<String, i64>,
    refs: &mut HashMap<String, MintedHandle>,
) -> Result<(), String> {
    if let Some(name) = step.get("free").and_then(Value::as_str) {
        let h = refs
            .remove(name)
            .ok_or_else(|| format!("free: {name:?} names no bound handle"))?;
        free_handle(api, h);
        return Ok(());
    }
    if let Some(want) = step.get("live_delta") {
        let now = live_counts(api)?;
        let want_obj = want.as_object().ok_or("live_delta is not an object")?;
        for (kind, delta) in want_obj {
            let d = delta.as_i64().ok_or("live_delta value is not an integer")?;
            let before = base.get(kind).copied().unwrap_or(0);
            let after = now.get(kind).copied().unwrap_or(0);
            if after - before != d {
                return Err(format!(
                    "live_delta {kind}: want {d}, got {} (began at {before}, now {after})",
                    after - before
                ));
            }
        }
        return Ok(());
    }
    let call_v = step.get("call").ok_or("step has no recognized key")?;
    let call = parse_call(call_v)?;
    let mut minted = Vec::new();
    let outcome = resolve_and_call(api, &call, &mut minted, refs)?;
    if let Some(name) = step.get("let").and_then(Value::as_str) {
        let Outcome::Call {
            status, outputs, ..
        } = &outcome
        else {
            return Err(format!(
                "{}: a let call must be a status call",
                call.fn_name
            ));
        };
        if *status != "CHS_OK" {
            return Err(format!("{}: let call returned {status}", call.fn_name));
        }
        let bound = outputs
            .get("out")
            .copied()
            .ok_or_else(|| format!("{}: no \"out\" output to bind", call.fn_name))?;
        for h in minted {
            if !matches!(h, MintedHandle::Buf(_))
                && h.raw_ptr() == bound.raw_ptr()
                && h.kind_name() == bound.kind_name()
            {
                continue;
            }
            // Anything else the call produced (and any nested recipe) is freed
            // now; the bound handle lives on until its own `free` step.
            free_handle(api, h);
        }
        refs.insert(name.to_string(), bound);
        return Ok(());
    }
    let expect = step
        .get("expect")
        .ok_or("a call step needs \"let\" or \"expect\"")?;
    let verdict = check_call(api, &outcome, expect);
    for h in minted {
        free_handle(api, h);
    }
    verdict
}

/// Shares one `Api`, one resolved argument list and one expectation between
/// the threads of a `concurrent` case. The ABI declares every function the
/// case calls `shared` (any number of threads, same handles), which is what
/// makes this sound; the raw pointers inside the arguments are never freed
/// until every thread has joined.
struct SharedCase<'a> {
    api: &'a Api,
    name: &'a str,
    args: &'a [ResolvedArg],
    expect: &'a Value,
}

// SAFETY: see the type's comment; the borrowed data is read-only for the
// scope's lifetime and the called function is thread class `shared`.
unsafe impl Sync for SharedCase<'_> {}

impl SharedCase<'_> {
    fn call_once(&self) -> Result<(), String> {
        // SAFETY: the arguments were minted by this same `api` and outlive the
        // threads; the function is `shared`, so concurrent use is allowed.
        let outcome = unsafe { invoke(self.api, self.name, self.args) }?;
        let verdict = check_call(self.api, &outcome, self.expect);
        free_outputs(self.api, &outcome);
        verdict
    }
}

/// `concurrent`: the arguments are evaluated once, then `threads` threads each
/// make `calls` calls at once; every call is checked as an echo case is and
/// every produced handle freed; afterwards the live counts must equal those
/// read before the case.
fn run_concurrent_case(api: &Api, case: &Value) -> Result<(), String> {
    let fn_name = case
        .get("fn")
        .and_then(Value::as_str)
        .ok_or("case has no \"fn\"")?;
    let threads = case
        .get("threads")
        .and_then(Value::as_u64)
        .ok_or("no threads")? as usize;
    let calls = case
        .get("calls")
        .and_then(Value::as_u64)
        .ok_or("no calls")? as usize;
    let expect = case.get("expect").ok_or("case has no \"expect\"")?;
    let specs = case
        .get("args")
        .and_then(Value::as_array)
        .ok_or("case has no \"args\"")?
        .iter()
        .map(parse_arg)
        .collect::<Result<Vec<_>, _>>()?;
    let base = live_counts(api)?;
    let mut minted = Vec::new();
    let refs = HashMap::new();
    let mut resolved = Vec::new();
    let mut setup: Result<(), String> = Ok(());
    for spec in &specs {
        match resolve_arg(api, spec, &mut minted, &refs) {
            Ok(a) => resolved.push(a),
            Err(e) => {
                setup = Err(e);
                break;
            }
        }
    }
    let verdict = setup.and_then(|()| {
        let shared = SharedCase {
            api,
            name: fn_name,
            args: &resolved,
            expect,
        };
        let shared = &shared;
        let first_err: Vec<String> = std::thread::scope(|scope| {
            let handles: Vec<_> = (0..threads)
                .map(|_| {
                    scope.spawn(move || {
                        for _ in 0..calls {
                            shared.call_once()?;
                        }
                        Ok::<(), String>(())
                    })
                })
                .collect();
            handles
                .into_iter()
                .filter_map(|h| match h.join() {
                    Ok(Ok(())) => None,
                    Ok(Err(e)) => Some(e),
                    Err(_) => Some("a thread panicked".to_string()),
                })
                .collect()
        });
        match first_err.first() {
            Some(e) => Err(e.clone()),
            None => Ok(()),
        }
    });
    for h in minted {
        free_handle(api, h);
    }
    verdict?;
    let after = live_counts(api)?;
    if after != base {
        return Err(format!("live counts began {base:?}, ended {after:?}"));
    }
    Ok(())
}

// ------------------------------------------------------------------ report

/// sha256 of the sorted, compact JSON of `cases`, matching
/// `scripts/abi-v1/parity.py`'s `compute_cases_hash` byte for byte:
/// `serde_json::Value`'s object type is a `BTreeMap` by construction (this
/// crate enables no `preserve_order` feature anywhere), so key order is
/// already sorted, and `serde_json::to_string` is already the compact
/// `(",", ":")` separator form Python's `sort_keys=True,
/// separators=(",", ":")` produces for this ASCII, integer-only dataset.
fn compute_cases_hash(cases: &Value) -> String {
    let text = serde_json::to_string(cases).expect("cases serialize");
    let digest = Sha256::digest(text.as_bytes());
    let mut hex = String::with_capacity(digest.len() * 2);
    for b in digest {
        use std::fmt::Write;
        let _ = write!(hex, "{b:02x}");
    }
    hex
}

/// `cases.schema.json`'s per-case `"os"` field ("linux" or "darwin"; absent
/// means every platform): "a conformance runner must skip, never fail, a
/// case whose os does not match its own." The report schema has no "skip"
/// status, so an inapplicable case is recorded as a vacuous pass instead
/// (its own `detail` says so) — parity.py still requires a result for every
/// case id in cases.json, matched or not.
fn case_applies_on_this_os(case: &Value) -> bool {
    match case.get("os").and_then(Value::as_str) {
        None => true,
        Some(want) => want == host_os_arch().0,
    }
}

fn host_os_arch() -> (&'static str, &'static str) {
    let os = match std::env::consts::OS {
        "linux" => "linux",
        "macos" => "darwin",
        other => other,
    };
    let arch = match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => other,
    };
    (os, arch)
}

#[test]
fn abi1_conformance() {
    let Some(stubs_dir) = std::env::var_os("CHTYPES_ABI1_STUBS") else {
        announce(
            "SKIP: CHTYPES_ABI1_STUBS is unset; the abi1 conformance suite needs the stub \
             libraries scripts/abi-v1/build-stubs.sh produces",
        );
        return;
    };
    let stubs_dir = PathBuf::from(stubs_dir);
    let report_path = std::env::var_os("CHTYPES_ABI1_REPORT").map(PathBuf::from);
    let toolchain =
        std::env::var("CHTYPES_ABI1_TOOLCHAIN").unwrap_or_else(|_| "unknown".to_string());

    let stubs_text = std::fs::read_to_string(stubs_dir.join("stubs.json"))
        .expect("read CHTYPES_ABI1_STUBS/stubs.json");
    let stubs: Value = serde_json::from_str(&stubs_text).expect("parse stubs.json");
    let variants = stubs
        .get("variants")
        .and_then(Value::as_object)
        .expect("stubs.json has no \"variants\"");

    let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("rust/ has a parent directory");
    let cases_path = repo_root.join("tests/fixtures/abi-v1/cases.json");
    let cases_text = std::fs::read_to_string(&cases_path)
        .unwrap_or_else(|e| panic!("read {}: {e}", cases_path.display()));
    let cases_doc: Value = serde_json::from_str(&cases_text).expect("parse cases.json");
    let cases = cases_doc
        .get("cases")
        .and_then(Value::as_array)
        .expect("cases.json has no \"cases\"");

    let mut results: Vec<(String, bool, Option<String>)> = Vec::new();

    let ok_api: Option<Api> = match variants.get("ok") {
        None => {
            results.push((
                "ok-load".to_string(),
                false,
                Some("stubs.json has no \"ok\" variant".to_string()),
            ));
            None
        }
        Some(ok_variant) => match load_variant(&stubs_dir, ok_variant) {
            Ok(loaded) => Some(loaded.api),
            Err(e) => {
                let detail = match e {
                    LoadError::Refused(r) => r.to_string(),
                    LoadError::Initialize(i) => {
                        format!("chs_initialize: {} ({})", i.status, i.message)
                    }
                };
                results.push(("ok-load".to_string(), false, Some(detail)));
                None
            }
        },
    };

    for case in cases {
        let id = case
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("<no id>")
            .to_string();
        let kind = case.get("kind").and_then(Value::as_str).unwrap_or("");
        let expect = case.get("expect").cloned().unwrap_or(Value::Null);
        if !case_applies_on_this_os(case) {
            // OMITTED, not a pass (the PM's ruling): a case whose "os" names a
            // different platform never ran here, and reporting it pass:true
            // would be "a pass that never ran" — exactly the pattern this
            // project refuses elsewhere. No result entry at all; F-B is
            // updating parity.py and cases.schema.json, in a follow-up PR, to
            // expect that a report may omit a case whose "os" excludes it.
            continue;
        }
        let verdict: Result<(), String> = if kind == "loader" {
            run_loader_case(&stubs_dir, case, variants)
        } else {
            match &ok_api {
                None => Err("the \"ok\" stub did not load; see the ok-load result".to_string()),
                Some(api) => match kind {
                    "lifecycle" => run_lifecycle_case(api, case),
                    "concurrent" => run_concurrent_case(api, case),
                    _ => run_generic_case(api, case, kind, &expect),
                },
            }
        };
        match verdict {
            Ok(()) => results.push((id, true, None)),
            Err(detail) => results.push((id, false, Some(detail))),
        }
    }

    let (os, arch) = host_os_arch();
    let results_json: Vec<Value> = results
        .iter()
        .map(|(id, pass, detail)| {
            let mut o = serde_json::Map::new();
            o.insert("id".to_string(), Value::String(id.clone()));
            o.insert("pass".to_string(), Value::Bool(*pass));
            if let Some(d) = detail {
                o.insert("detail".to_string(), Value::String(d.clone()));
            }
            Value::Object(o)
        })
        .collect();
    let report = serde_json::json!({
        "schema": 1,
        "binding": "rust",
        "toolchain": toolchain,
        "os": format!("{os}-{arch}"),
        "cases_sha256": compute_cases_hash(&Value::Array(cases.clone())),
        "results": results_json,
    });

    if let Some(path) = &report_path {
        let text = serde_json::to_string_pretty(&report).expect("serialize report");
        std::fs::write(path, text).unwrap_or_else(|e| panic!("write {}: {e}", path.display()));
    } else {
        announce("CHTYPES_ABI1_REPORT is unset; the report was built but not written to a file");
    }

    let failed: Vec<&str> = results
        .iter()
        .filter(|(_, pass, _)| !pass)
        .map(|(id, _, _)| id.as_str())
        .collect();
    assert!(
        failed.is_empty(),
        "{} of {} abi1 conformance case(s) failed: {failed:?}",
        failed.len(),
        results.len()
    );
}
