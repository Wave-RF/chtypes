//! The Rust runner for the v1 goldens (`docs/guides/goldens-v1.md`, section 2).
//! CI runs it as
//! `cargo test --locked --features fetch-v1 --test goldens_v1_runner`
//! from the `v1-goldens` workflow's rust leg.
//!
//! WHAT IT DOES, AND ONLY THAT. It fetches the release and its goldens document
//! with this crate's own fetch layer, executes every case's call sequence
//! through the generated call layer (the raw buffers), and RECORDS what the
//! library returned. It compares nothing: `scripts/goldens-v1/compare.py` does
//! that, once, for all four bindings. Bytes are bytes: `document_b64` and
//! `export_b64` are the buffers exactly as the library returned them.
//!
//! WHY THIS FILE RE-DECLARES THE CRATE'S MODULES WITH `#[path]`. An integration
//! test is a separate crate that sees only public items, and the generated
//! call layer, the loader, the fetch layer and the document decoders are
//! deliberately private (`rust/tests/abi1_conformance.rs` explains the same
//! constraint). The files are compiled twice, from identical source. The
//! decoders are the very functions `Schema::row`, `Schema::rows`,
//! `Filter::rows` and `Schema::describe` hand each document to, so
//! `decoded_ok` is the answer the public call a user makes would give.
//!
//! ONE PROCESS PER SETUP. `chs_initialize` is process-once, so the parent
//! process never initializes a library. For each `setups[]` entry it re-executes
//! this test binary (`current_exe`) in child mode (`CHTYPES_GOLDENS_CHILD` names
//! the setup), and the child loads the library, which runs
//! `chs_initialize(image_zone)` and then `chs_set_defaults(defaults)` once
//! (skipped when `defaults_b64` is empty), runs every case of that setup, and
//! writes its records to a file. The parent merges the records into the report.
//!
//! INPUTS (`docs/guides/goldens-v1.md`, section 2): `CHTYPES_GOLDENS_REGISTRY_BASE`,
//! `CHTYPES_GOLDENS_VERSION` and `CHTYPES_GOLDENS_PLATFORM`; outputs
//! `CHTYPES_GOLDENS_REPORT` and `CHTYPES_GOLDENS_DOCUMENT`. Unset, the runner
//! skips LOUDLY by name and exits 0, so a plain `cargo test` stays green.
//!
//! PROVING IT BEFORE A REAL ARTIFACT EXISTS (the stub). Two overrides bypass the
//! fetch: `CHTYPES_GOLDENS_LIBRARY` (a library to load, unverified) with
//! `CHTYPES_GOLDENS_DOCUMENT_IN` (a goldens document to execute). With only
//! `CHTYPES_ABI1_STUBS` set (the `v1-abi-conformance` leg's environment), the
//! runner runs `rust/tests/goldens_v1_stub/goldens.json` against that
//! directory's `ok` stub, hands the report to the comparator, which must pass,
//! and then plants one wrong byte in the report, which the comparator must
//! refuse.

// harness = false (Cargo.toml): this file owns `main`, because it re-executes
// itself as a child and libtest would add nothing but a second argument parser.

// The included files' own `#[cfg(test)]` modules are compiled here too (cargo
// builds this target with cfg(test)), and with no libtest harness their imports
// are unused; hence `unused_imports` on each.
#[path = "../src/abi1/mod.rs"]
#[allow(unused_imports)]
mod abi1;
#[path = "../src/decode.rs"]
#[allow(dead_code, unused_imports)]
mod decode;
#[path = "../src/error.rs"]
#[allow(dead_code, unused_imports)]
mod error;
#[path = "../src/ocifetch/mod.rs"]
#[allow(unused_imports)]
mod ocifetch;
#[path = "../src/raw.rs"]
#[allow(dead_code, unused_imports)]
mod raw;
#[path = "../src/result.rs"]
#[allow(dead_code, unused_imports)]
mod result;

use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::sync::Arc;

use base64::Engine as _;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use abi1::calls_gen::{RawCallError, SchemaHandle};
use abi1::decls::{self, Api};
use abi1::loader::{self, LoadError, LoadInput};
use ocifetch::ensure::{self, Options};

const ENV_REGISTRY_BASE: &str = "CHTYPES_GOLDENS_REGISTRY_BASE";
const ENV_VERSION: &str = "CHTYPES_GOLDENS_VERSION";
const ENV_PLATFORM: &str = "CHTYPES_GOLDENS_PLATFORM";
const ENV_REPORT: &str = "CHTYPES_GOLDENS_REPORT";
const ENV_DOCUMENT: &str = "CHTYPES_GOLDENS_DOCUMENT";
const ENV_LIBRARY: &str = "CHTYPES_GOLDENS_LIBRARY";
const ENV_DOCUMENT_IN: &str = "CHTYPES_GOLDENS_DOCUMENT_IN";
const ENV_STUBS: &str = "CHTYPES_ABI1_STUBS";
// Child mode: the setup id to run, and where the parent put what the child needs.
const ENV_CHILD: &str = "CHTYPES_GOLDENS_CHILD";
const ENV_CHILD_LIBRARY: &str = "CHTYPES_GOLDENS_CHILD_LIBRARY";
const ENV_CHILD_PREDICATE: &str = "CHTYPES_GOLDENS_CHILD_PREDICATE";
const ENV_CHILD_DOCUMENT: &str = "CHTYPES_GOLDENS_CHILD_DOCUMENT";
const ENV_CHILD_OUT: &str = "CHTYPES_GOLDENS_CHILD_OUT";
const ENV_CHILD_PLATFORM: &str = "CHTYPES_GOLDENS_CHILD_PLATFORM";

fn b64() -> base64::engine::GeneralPurpose {
    base64::engine::general_purpose::STANDARD
}

fn enc(bytes: &[u8]) -> String {
    b64().encode(bytes)
}

fn dec(v: &Value, what: &str) -> Result<Vec<u8>, String> {
    let s = v
        .as_str()
        .ok_or_else(|| format!("{what} is not a string"))?;
    b64()
        .decode(s)
        .map_err(|e| format!("{what} is not base64: {e}"))
}

fn announce(message: &str) {
    use std::io::Write;
    let _ = std::io::stderr().write_all(format!("{message}\n").as_bytes());
}

fn main() -> ExitCode {
    let result = if let Ok(setup) = std::env::var(ENV_CHILD) {
        child(&setup)
    } else {
        parent()
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            announce(&format!("goldens_v1_runner: FAILED: {message}"));
            ExitCode::FAILURE
        }
    }
}

// ----------------------------------------------------------------- the parent

/// Where the inputs came from: the fetch layer, or an override.
struct Inputs {
    library: PathBuf,
    /// The verified predicate, or `None` for an override (an unverified load).
    predicate: Option<Value>,
    document: Vec<u8>,
    platform: String,
}

fn parent() -> Result<(), String> {
    let real = [ENV_REGISTRY_BASE, ENV_VERSION, ENV_PLATFORM]
        .iter()
        .all(|k| std::env::var(k).is_ok_and(|v| !v.is_empty()));
    let stub_override = std::env::var(ENV_LIBRARY).is_ok_and(|v| !v.is_empty())
        && std::env::var(ENV_DOCUMENT_IN).is_ok_and(|v| !v.is_empty());
    let stubs = std::env::var(ENV_STUBS).is_ok_and(|v| !v.is_empty());
    if real {
        let inputs = fetch_inputs()?;
        let report = run(&inputs)?;
        return write_outputs(&inputs.document, &report);
    }
    if stub_override {
        let inputs = Inputs {
            library: PathBuf::from(std::env::var(ENV_LIBRARY).map_err(|e| e.to_string())?),
            predicate: None,
            document: std::fs::read(std::env::var(ENV_DOCUMENT_IN).map_err(|e| e.to_string())?)
                .map_err(|e| format!("reading {ENV_DOCUMENT_IN}: {e}"))?,
            platform: std::env::var(ENV_PLATFORM)
                .ok()
                .filter(|v| !v.is_empty())
                .unwrap_or_else(host_platform_key),
        };
        let report = run(&inputs)?;
        return write_outputs(&inputs.document, &report);
    }
    if stubs {
        return stub_selftest();
    }
    announce(&format!(
        "goldens_v1_runner: SKIPPED by name: none of {ENV_REGISTRY_BASE}, {ENV_VERSION} and {ENV_PLATFORM} (all three) is set, \
         no {ENV_LIBRARY}/{ENV_DOCUMENT_IN} override, and no {ENV_STUBS}. Nothing ran, and that is the only skip this runner allows."
    ));
    Ok(())
}

fn host_platform_key() -> String {
    let os = if cfg!(target_os = "macos") {
        "darwin"
    } else {
        "linux"
    };
    let arch = if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        "amd64"
    };
    format!("{os}-{arch}")
}

/// The fetch layer's `ensure` for the release, then `fetch_signed` for the
/// goldens, which takes the PLATFORM manifest digest and does the referrer
/// selection itself: this runner does no lookup of its own.
fn fetch_inputs() -> Result<Inputs, String> {
    let base = std::env::var(ENV_REGISTRY_BASE).map_err(|e| e.to_string())?;
    let version = std::env::var(ENV_VERSION).map_err(|e| e.to_string())?;
    let platform = std::env::var(ENV_PLATFORM).map_err(|e| e.to_string())?;
    let options = || Options {
        platform: Some(platform.clone()),
        bases: Some(vec![base.clone()]),
        ..Options::default()
    };
    let resolved = ensure::ensure(&version, options())
        .map_err(|e| format!("ensure {version} for {platform}: {e}"))?;
    let (document, _predicate, _digests) = ensure::fetch_signed(
        std::slice::from_ref(&base),
        &resolved.digests.manifest,
        ocifetch::constants::PREDICATE_TYPE_GOLDENS,
        &mut options(),
    )
    .map_err(|e| {
        format!(
            "fetch_signed goldens for {}: {e}",
            resolved.digests.manifest
        )
    })?;
    Ok(Inputs {
        library: resolved.library_path.clone(),
        predicate: Some(resolved.predicate.clone()),
        document,
        platform,
    })
}

/// `linux-amd64` to the report's `linux/amd64`.
fn report_platform(key: &str) -> String {
    key.replacen('-', "/", 1)
}

/// Run every setup in a process of its own and assemble the report.
fn run(inputs: &Inputs) -> Result<Value, String> {
    let doc: Value = serde_json::from_slice(&inputs.document)
        .map_err(|e| format!("the goldens document is not JSON: {e}"))?;
    let setups = doc["setups"]
        .as_array()
        .ok_or("the goldens document has no setups[]")?;

    let scratch = std::env::temp_dir().join(format!("goldens_v1_runner_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&scratch);
    std::fs::create_dir_all(&scratch)
        .map_err(|e| format!("creating {}: {e}", scratch.display()))?;
    let document_path = scratch.join("document.json");
    std::fs::write(&document_path, &inputs.document).map_err(|e| e.to_string())?;
    let predicate_path = scratch.join("predicate.json");
    if let Some(p) = &inputs.predicate {
        std::fs::write(
            &predicate_path,
            serde_json::to_vec(p).map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
    }

    let exe = std::env::current_exe().map_err(|e| format!("current_exe: {e}"))?;
    let mut cases: Vec<Value> = Vec::new();
    let mut identity: Option<(String, String)> = None;
    for (i, setup) in setups.iter().enumerate() {
        let id = setup["id"].as_str().ok_or("a setup has no id")?;
        let out = scratch.join(format!("setup-{i}.json"));
        let mut cmd = Command::new(&exe);
        cmd.env(ENV_CHILD, id)
            .env(ENV_CHILD_LIBRARY, &inputs.library)
            .env(ENV_CHILD_DOCUMENT, &document_path)
            .env(ENV_CHILD_OUT, &out)
            .env(ENV_CHILD_PLATFORM, &inputs.platform);
        if inputs.predicate.is_some() {
            cmd.env(ENV_CHILD_PREDICATE, &predicate_path);
        }
        let status = cmd
            .status()
            .map_err(|e| format!("running the child for setup {id}: {e}"))?;
        if !status.success() {
            return Err(format!("the child for setup {id} exited {status}"));
        }
        let record: Value = serde_json::from_slice(
            &std::fs::read(&out).map_err(|e| format!("reading {}: {e}", out.display()))?,
        )
        .map_err(|e| format!("setup {id}'s record is not JSON: {e}"))?;
        let this = (
            record["build_info_b64"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
            record["abi_fingerprint"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
        );
        match &identity {
            None => identity = Some(this),
            Some(first) if *first != this => {
                return Err(format!(
                    "setup {id} loaded a library that identifies differently from the first setup's"
                ));
            }
            Some(_) => {}
        }
        cases.extend(record["cases"].as_array().cloned().unwrap_or_default());
    }
    let _ = std::fs::remove_dir_all(&scratch);
    let (build_info_b64, abi_fingerprint) = identity.ok_or("the document has no setups")?;

    Ok(json!({
        "report_version": 1,
        "binding": "rust",
        "binding_version": env!("CARGO_PKG_VERSION"),
        "platform": report_platform(&inputs.platform),
        "goldens": {
            "clickhouse_version": doc["clickhouse_version"],
            "build": doc["build"],
            "revision": doc["revision"],
            "sha256": hex(&Sha256::digest(&inputs.document)),
        },
        "artifact": {
            "build_info_b64": build_info_b64,
            "abi_fingerprint": abi_fingerprint,
        },
        "cases": cases,
    }))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// The document's EXACT bytes to `$CHTYPES_GOLDENS_DOCUMENT`, the report to
/// `$CHTYPES_GOLDENS_REPORT`.
fn write_outputs(document: &[u8], report: &Value) -> Result<(), String> {
    let report_path = std::env::var(ENV_REPORT).map_err(|_| format!("{ENV_REPORT} is not set"))?;
    let document_path =
        std::env::var(ENV_DOCUMENT).map_err(|_| format!("{ENV_DOCUMENT} is not set"))?;
    std::fs::write(&document_path, document)
        .map_err(|e| format!("writing {document_path}: {e}"))?;
    std::fs::write(
        &report_path,
        serde_json::to_vec_pretty(report).map_err(|e| e.to_string())?,
    )
    .map_err(|e| format!("writing {report_path}: {e}"))?;
    announce(&format!(
        "goldens_v1_runner: recorded {} case(s) to {report_path}",
        report["cases"].as_array().map_or(0, Vec::len)
    ));
    Ok(())
}

// ------------------------------------------------------------------ the child

fn child(setup_id: &str) -> Result<(), String> {
    let var = |k: &str| std::env::var(k).map_err(|_| format!("child mode needs {k}"));
    let library = PathBuf::from(var(ENV_CHILD_LIBRARY)?);
    let out = PathBuf::from(var(ENV_CHILD_OUT)?);
    let platform = report_platform(&var(ENV_CHILD_PLATFORM)?);
    let doc: Value = serde_json::from_slice(
        &std::fs::read(var(ENV_CHILD_DOCUMENT)?).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    let predicate: Option<Value> = match std::env::var(ENV_CHILD_PREDICATE) {
        Ok(path) => Some(
            serde_json::from_slice(&std::fs::read(&path).map_err(|e| e.to_string())?)
                .map_err(|e| format!("the predicate is not JSON: {e}"))?,
        ),
        Err(_) => None,
    };
    let setup = doc["setups"]
        .as_array()
        .and_then(|a| a.iter().find(|s| s["id"] == setup_id))
        .ok_or_else(|| format!("no setup {setup_id:?} in the document"))?;

    let zone = dec(&setup["image_zone_b64"], "image_zone_b64")?;
    let defaults = dec(&setup["defaults_b64"], "defaults_b64")?;
    // chs_initialize, then chs_set_defaults when there are defaults: the
    // loader's step 7, once, in this process.
    let loaded = loader::load(LoadInput {
        library_path: &library,
        predicate: predicate.as_ref(),
        timezone: &zone,
        defaults: (!defaults.is_empty()).then_some(defaults.as_slice()),
    })
    .map_err(|e| match e {
        LoadError::Refused(r) => format!("the loader refused {}: {r}", library.display()),
        LoadError::Call(c) => format!("setup {setup_id}: {}", call_error_text(&c)),
    })?;

    let mut records = Vec::new();
    for case in setup["cases"].as_array().ok_or("the setup has no cases")? {
        records.push(run_case(&loaded.api, case, setup_id, &platform)?);
    }
    let abi_fingerprint = loaded
        .build_info
        .get("abi_fingerprint")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let record = json!({
        "build_info_b64": enc(&loaded.build_info_raw),
        "abi_fingerprint": abi_fingerprint,
        "cases": records,
    });
    std::fs::write(
        &out,
        serde_json::to_vec(&record).map_err(|e| e.to_string())?,
    )
    .map_err(|e| format!("writing {}: {e}", out.display()))
}

fn call_error_text(e: &RawCallError) -> String {
    format!(
        "{} {} {}",
        decls::status_name(e.status).unwrap_or("?"),
        e.ch_name,
        String::from_utf8_lossy(&e.message)
    )
}

/// One step that did not answer `CHS_OK`: the step it stopped at and its error.
struct Stopped {
    at: &'static str,
    error: RawCallError,
}

fn run_case(api: &Arc<Api>, case: &Value, setup_id: &str, platform: &str) -> Result<Value, String> {
    let id = case["id"].as_str().ok_or("a case has no id")?;
    let platforms = case["platforms"]
        .as_array()
        .ok_or_else(|| format!("{id}: no platforms"))?;
    if !platforms.iter().any(|p| p == platform) {
        // The only skip allowed: a platform the case excludes.
        return Ok(json!({
            "id": id,
            "setup": setup_id,
            "result": "skipped",
            "skip_reason": format!("the case's platforms exclude {platform}"),
        }));
    }

    let outcome = execute(api, case).map_err(|e| format!("{id}: {e}"))?;
    let mut rec = Map::new();
    rec.insert("id".into(), json!(id));
    rec.insert("setup".into(), json!(setup_id));
    rec.insert("result".into(), json!("ran"));
    match outcome {
        Err(Stopped { at, error }) => {
            rec.insert("at".into(), json!(at));
            rec.insert(
                "status".into(),
                json!(decls::status_name(error.status).unwrap_or("CHS_INTERNAL")),
            );
            rec.insert(
                "error".into(),
                json!({
                    "ch_code": error.ch_code,
                    "ch_name_b64": enc(error.ch_name.as_bytes()),
                    "message_b64": enc(&error.message),
                    "column_b64": enc(&error.column),
                }),
            );
            rec.insert("decoded_ok".into(), Value::Null);
        }
        Ok((document, export, decoded_ok)) => {
            rec.insert("at".into(), json!("call"));
            rec.insert("status".into(), json!("CHS_OK"));
            rec.insert("document_b64".into(), json!(enc(&document)));
            if let Some(export) = export {
                rec.insert("export_b64".into(), json!(enc(&export)));
            }
            rec.insert("decoded_ok".into(), json!(decoded_ok));
        }
    }
    Ok(Value::Object(rec))
}

/// What a successful sequence returns: the document bytes, the export bytes
/// when the call produced one, and whether the public decoder accepted the
/// document.
type Answered = (Vec<u8>, Option<Vec<u8>>, bool);

/// The call sequence the case's `call` names. The outer error is a malformed
/// case (a runner defect); the inner one is a step the LIBRARY answered with a
/// non-OK status, which is a result to record.
fn execute(api: &Arc<Api>, case: &Value) -> Result<Result<Answered, Stopped>, String> {
    let call = case["call"].as_str().ok_or("no call")?;
    let schema_in = &case["schema"];
    let create_table = dec(&schema_in["create_table_b64"], "create_table_b64")?;
    let schema_settings = dec(&schema_in["settings_b64"], "schema settings_b64")?;
    let schema: SchemaHandle = match api.schema_create(&create_table, &schema_settings) {
        Ok(s) => s,
        Err(error) => {
            return Ok(Err(Stopped {
                at: "schema_create",
                error,
            }));
        }
    };

    if call == "schema_create" {
        // chs_schema_describe's document is the case's output.
        return Ok(match api.schema_describe(&schema) {
            Ok(doc) => {
                let ok = decode::schema_description(&doc).is_ok();
                Ok((doc, None, ok))
            }
            Err(error) => Err(Stopped { at: "call", error }),
        });
    }

    let body_in = &case["body"];
    let format = body_in["format"]
        .as_i64()
        .ok_or("body.format is not an integer")? as i32;
    let body = dec(&body_in["body_b64"], "body_b64")?;
    let settings = dec(&body_in["settings_b64"], "body settings_b64")?;
    // absent means zero bytes (no column list)
    let columns = match body_in.get("columns_b64") {
        Some(v) => dec(v, "columns_b64")?,
        None => Vec::new(),
    };

    match call {
        "preview_row" => Ok(
            match api.preview_row(&schema, format, &body, &settings, &columns) {
                Ok(doc) => {
                    let ok = decode::row(&doc).is_ok();
                    Ok((doc, None, ok))
                }
                Err(error) => Err(Stopped { at: "call", error }),
            },
        ),
        "preview_batch" => {
            let export_format = body_in
                .get("export_format")
                .and_then(Value::as_i64)
                .unwrap_or(-1) as i32;
            let doc_flags = body_in
                .get("doc_flags")
                .and_then(Value::as_u64)
                .unwrap_or(7) as u32;
            Ok(
                match api.preview_batch(
                    &schema,
                    format,
                    &body,
                    &settings,
                    &columns,
                    None,
                    export_format,
                    doc_flags,
                ) {
                    Ok((doc, export)) => {
                        // The public call hands the export to the decoder as the payload.
                        let ok = decode::batch(&doc, export.clone()).is_ok();
                        Ok((doc, export, ok))
                    }
                    Err(error) => Err(Stopped { at: "call", error }),
                },
            )
        }
        "filter_eval_body" => {
            let filter_in = &case["filter"];
            let expr = dec(&filter_in["expr_b64"], "expr_b64")?;
            let params = dec(&filter_in["query_params_b64"], "query_params_b64")?;
            let filter_settings = dec(&filter_in["settings_b64"], "filter settings_b64")?;
            let filter = match api.filter_create(&schema, &expr, &params, &filter_settings) {
                Ok(f) => f,
                Err(error) => {
                    return Ok(Err(Stopped {
                        at: "filter_create",
                        error,
                    }));
                }
            };
            Ok(
                match api.filter_eval_body(&filter, format, &body, &settings) {
                    Ok(doc) => {
                        let ok = decode::filter_result(&doc).is_ok();
                        Ok((doc, None, ok))
                    }
                    Err(error) => Err(Stopped { at: "call", error }),
                },
            )
        }
        other => Err(format!("an unknown call {other:?}")),
    }
}

// ----------------------------------------------------------------- the stub

/// Run the hand-written stub goldens against the `ok` stub, then the comparator
/// twice: it must pass the report, and it must refuse the same report with one
/// byte of one case's document changed.
fn stub_selftest() -> Result<(), String> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let stubs_dir = PathBuf::from(std::env::var(ENV_STUBS).map_err(|e| e.to_string())?);
    let manifest: Value = serde_json::from_slice(
        &std::fs::read(stubs_dir.join("stubs.json")).map_err(|e| format!("stubs.json: {e}"))?,
    )
    .map_err(|e| format!("stubs.json is not JSON: {e}"))?;
    let ok = manifest["variants"]["ok"]["path"]
        .as_str()
        .ok_or("stubs.json names no `ok` variant")?;
    let document_path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/goldens_v1_stub/goldens.json");
    let inputs = Inputs {
        library: stubs_dir.join(ok),
        predicate: None,
        document: std::fs::read(&document_path)
            .map_err(|e| format!("{}: {e}", document_path.display()))?,
        platform: host_platform_key(),
    };
    let report = run(&inputs)?;

    let scratch = std::env::temp_dir().join(format!("goldens_v1_stub_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&scratch);
    std::fs::create_dir_all(&scratch).map_err(|e| e.to_string())?;
    let report_path = scratch.join("report.json");
    std::fs::write(
        &report_path,
        serde_json::to_vec_pretty(&report).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;

    let compare = |report: &Path| -> Result<(bool, String), String> {
        let out = Command::new("python3")
            .arg(root.join("scripts/goldens-v1/compare.py"))
            .arg("--goldens")
            .arg(&document_path)
            .arg("--report")
            .arg(report)
            .output()
            .map_err(|e| format!("running compare.py: {e}"))?;
        let text = format!(
            "{}{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
        Ok((out.status.success(), text))
    };

    let (passed, text) = compare(&report_path)?;
    announce(&text);
    if !passed {
        return Err("the comparator refused the runner's own report over the stub".into());
    }

    // Plant one wrong byte in the first OK case's document and require a FAIL.
    let mut tampered = report.clone();
    let cases = tampered["cases"].as_array_mut().ok_or("no cases")?;
    let victim = cases
        .iter_mut()
        .find(|c| c["result"] == "ran" && c.get("document_b64").is_some())
        .ok_or("the stub document has no OK case to tamper with")?;
    let mut bytes = dec(&victim["document_b64"], "document_b64")?;
    let last = bytes.len() - 1;
    // Change a byte inside the document without making it invalid JSON text:
    // flip the last `}`'s predecessor into a different digit or letter.
    let at = bytes
        .iter()
        .rposition(|b| b.is_ascii_alphanumeric())
        .unwrap_or(last);
    bytes[at] = if bytes[at] == b'z' { b'y' } else { b'z' };
    victim["document_b64"] = json!(enc(&bytes));
    let tampered_path = scratch.join("tampered.json");
    std::fs::write(
        &tampered_path,
        serde_json::to_vec_pretty(&tampered).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    let (passed, text) = compare(&tampered_path)?;
    announce(&text);
    if passed {
        return Err("the comparator PASSED a report with a planted wrong byte".into());
    }
    announce(
        "goldens_v1_runner: stub self-test OK: the comparator passed the runner's report and refused the planted wrong byte",
    );
    let _ = std::fs::remove_dir_all(&scratch);
    Ok(())
}
