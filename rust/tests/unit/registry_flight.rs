//! An open never waits for another request's fetch, and concurrent opens of one
//! request share one fetch (public issue #491).
//!
//! Each test serves the `ok` stub, signed with the fetch fixtures' test key,
//! from the fetch fixture server (`scripts/fetch-v1/server.py`) over HTTP, and
//! holds a fetch in flight with the server's test-only gate: the request is
//! logged and parked until the test opens the gate. Every wait is on an event
//! (the server's parked count, the registry's `on_wait` hook, a channel),
//! never on a clock; a bound only turns a hang into a failure. The verdicts
//! come from what the server logged and what each open returned. Rust has no
//! cancellation of an open, so there is no cancellation case here.
//!
//! A UNIT test for the reason `registry_stub.rs` gives: the test key is
//! reachable only through the fetch layer's test-only seam, which is per
//! thread, so every thread that opens takes it. `CHTYPES_ABI2_STUBS` unset:
//! every open test here skips LOUDLY by name and passes.
//!
//! The `Registry::fetch` tests at the end (public issue #492) need no stub:
//! they serve a signed artifact whose "library" is not a library at all, so
//! nothing could load it, and a fetch that tried to open it would fail. None of
//! them skips on a chtypes platform.

use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, mpsc};
use std::time::Duration;

use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{Value, json};

use crate::ocifetch::abi_fingerprint::DEV_ABI_FINGERPRINT;
use crate::ocifetch::channel;
use crate::ocifetch::ensure::{self, Options};
use crate::ocifetch::hold::{self, Claim, HoldState};
use crate::registry_stub_tests::{
    ARTIFACT_TYPE, ENV_STUBS, LIBRARY_NAME, MEDIA_BUNDLE, MEDIA_CONFIG, MEDIA_EMPTY, MEDIA_INDEX,
    MEDIA_LAYER, MEDIA_MANIFEST, PAYLOAD_TYPE, PREDICATE_TYPE, STATEMENT_TYPE, pae, repo_root,
    sha256_hex, test_signing_key,
};
use crate::{Error, FetchOptions, Library, Registry, RegistryOptions};

/// Turns a hang into a failure; a working registry answers at once.
const BOUND: Duration = Duration::from_secs(30);
const N: usize = 8;
/// The fixture server's case ids, each served from the one stub tree.
const CASES: [&str; 4] = [
    "flight-install",
    "flight-held",
    "flight-one-fetch",
    "flight-fails",
];

type Opened = crate::Result<Arc<Library>>;

/// The fixture server, killed when dropped.
struct Server {
    child: Child,
    origin: String,
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct Fixture {
    server: Server,
    work: PathBuf,
    cache: PathBuf,
    trusted: String,
    /// The layer blob's path under a case's repository.
    layer_path: String,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = self.server.child.kill();
        let _ = self.server.child.wait();
        let _ = std::fs::remove_dir_all(&self.work);
    }
}

/// The `Registry::fetch` tests' case ids, each served from the one tree.
const FETCH_CASES: [&str; 3] = ["fetch-installs", "fetch-shared", "fetch-unpublished"];

/// The fetch tests' library bytes: no loader accepts them.
const NOT_A_LIBRARY: &[u8] =
    b"chtypes registry fetch test: these bytes are not a loadable library\n";

/// Opens the gate it names when dropped, whatever happened, so no thread is
/// left parked.
struct Gate<'a> {
    fx: &'a Fixture,
    case_id: &'static str,
}

impl Drop for Gate<'_> {
    fn drop(&mut self) {
        let url = format!("{}/_gate/open/s-{}", self.fx.server.origin, self.case_id);
        let _ = ureq::Agent::new_with_defaults().get(&url).call();
    }
}

fn put(dir: &Path, name: &str, bytes: &[u8]) {
    std::fs::create_dir_all(dir).expect("create a route directory");
    std::fs::write(dir.join(name), bytes).expect("write a route file");
}

/// Write `library` as `tag`'s one-platform signed artifact in the route-tree
/// shape the fixture server serves (`manifests/<tag or digest>`,
/// `blobs/<digest>`, `referrers/<digest>`), and return the layer's digest.
fn write_route_tree(
    repo: &Path,
    tag: &str,
    library: &[u8],
    predicate: &Value,
    key: &SigningKey,
    keyid: &str,
) -> String {
    let blobs = repo.join("blobs");
    let manifests = repo.join("manifests");
    let blob = |bytes: &[u8]| {
        let digest = format!("sha256:{}", sha256_hex(bytes));
        put(&blobs, &digest, bytes);
        (digest, bytes.len() as u64)
    };
    let manifest = |doc: &Value| {
        let bytes = serde_json::to_vec(doc).unwrap();
        let digest = format!("sha256:{}", sha256_hex(&bytes));
        put(&manifests, &digest, &bytes);
        (digest, bytes.len() as u64)
    };

    let mut tar_bytes = Vec::new();
    {
        let mut builder = tar::Builder::new(&mut tar_bytes);
        let mut header = tar::Header::new_gnu();
        header.set_size(library.len() as u64);
        header.set_mode(0o755);
        header.set_mtime(0);
        header.set_entry_type(tar::EntryType::Regular);
        builder
            .append_data(&mut header, LIBRARY_NAME, library)
            .expect("append the library");
        builder.finish().expect("finish the tar");
    }
    let layer = ruzstd::encoding::compress_to_vec(
        &tar_bytes[..],
        ruzstd::encoding::CompressionLevel::Uncompressed,
    );
    let (layer_digest, layer_size) = blob(&layer[..]);
    let (config_digest, config_size) = blob(&b"{}"[..]);
    let (manifest_digest, manifest_size) = manifest(&json!({
        "schemaVersion": 2,
        "mediaType": MEDIA_MANIFEST,
        "artifactType": ARTIFACT_TYPE,
        "config": {"mediaType": MEDIA_CONFIG, "digest": config_digest, "size": config_size},
        "layers": [{"mediaType": MEDIA_LAYER, "digest": layer_digest, "size": layer_size}],
    }));

    let statement = json!({
        "_type": STATEMENT_TYPE,
        "subject": [{"name": LIBRARY_NAME, "digest": {"sha256": layer_digest.trim_start_matches("sha256:")}}],
        "predicateType": PREDICATE_TYPE,
        "predicate": predicate,
    });
    let payload = serde_json::to_vec(&statement).unwrap();
    let signature = key.sign(&pae(PAYLOAD_TYPE, &payload));
    let b64 = base64::engine::general_purpose::STANDARD;
    let bundle = json!({
        "mediaType": MEDIA_BUNDLE,
        "verificationMaterial": {"publicKey": {"hint": keyid}},
        "dsseEnvelope": {
            "payload": b64.encode(&payload),
            "payloadType": PAYLOAD_TYPE,
            "signatures": [{"sig": b64.encode(signature.to_bytes()), "keyid": keyid}],
        },
    });
    let (bundle_digest, bundle_size) = blob(&serde_json::to_vec(&bundle).unwrap()[..]);
    let (empty_digest, empty_size) = blob(&b"{}"[..]);
    let (referrer_digest, referrer_size) = manifest(&json!({
        "schemaVersion": 2,
        "mediaType": MEDIA_MANIFEST,
        "artifactType": MEDIA_BUNDLE,
        "config": {"mediaType": MEDIA_EMPTY, "digest": empty_digest, "size": empty_size},
        "layers": [{"mediaType": MEDIA_BUNDLE, "digest": bundle_digest, "size": bundle_size}],
        "subject": {"mediaType": MEDIA_MANIFEST, "digest": manifest_digest, "size": manifest_size},
    }));
    let referrers = serde_json::to_vec(&json!({
        "schemaVersion": 2,
        "mediaType": MEDIA_INDEX,
        "manifests": [{
            "mediaType": MEDIA_MANIFEST,
            "digest": referrer_digest,
            "size": referrer_size,
            "artifactType": MEDIA_BUNDLE,
        }],
    }))
    .unwrap();
    put(&repo.join("referrers"), &manifest_digest, &referrers);
    let fallback = format!("sha256-{}", manifest_digest.trim_start_matches("sha256:"));
    put(&manifests, &fallback, &referrers);

    let index = json!({
        "schemaVersion": 2,
        "mediaType": MEDIA_INDEX,
        "manifests": [{
            "mediaType": MEDIA_MANIFEST,
            "digest": manifest_digest,
            "size": manifest_size,
            "platform": {
                "architecture": predicate["arch"].as_str().expect("predicate arch"),
                "os": predicate["os"].as_str().expect("predicate os"),
            },
        }],
    });
    put(&manifests, tag, &serde_json::to_vec(&index).unwrap());
    layer_digest
}

impl Fixture {
    /// The stub served as tag 26.8 of every flight case, or `None` (after a
    /// loud skip line) without the stubs.
    fn new(test: &str) -> Option<Fixture> {
        let Some(dir) = std::env::var_os(ENV_STUBS).map(PathBuf::from) else {
            eprintln!(
                "SKIPPED (loudly): registry_flight_tests::{test} needs {ENV_STUBS} (the ABI v2 stub directory); nothing was exercised"
            );
            return None;
        };
        let manifest: Value = serde_json::from_slice(
            &std::fs::read(dir.join("stubs.json")).expect("read stubs.json"),
        )
        .expect("parse stubs.json");
        let ok = &manifest["variants"]["ok"];
        let library = std::fs::read(
            dir.join(
                Path::new(ok["path"].as_str().expect("ok path"))
                    .file_name()
                    .expect("file name"),
            ),
        )
        .expect("read the ok stub");
        let mut predicate = ok["predicate"].clone();
        let fields = predicate.as_object_mut().expect("predicate is an object");
        fields.insert("library".into(), json!(LIBRARY_NAME));
        fields.insert("library_sha256".into(), json!(sha256_hex(&library)));
        fields.insert("library_bytes".into(), json!(library.len()));
        if fields["os"] == "linux" {
            fields.entry("glibc_floor").or_insert(json!("2.17"));
        }
        Some(Fixture::serve(test, &CASES, &library, &predicate))
    }

    /// [`NOT_A_LIBRARY`] served as tag 26.8 of every case in [`FETCH_CASES`],
    /// signed with the test key for this SDK's own fingerprint, or `None`
    /// (after a loud skip line) on a host that is not a chtypes platform.
    fn not_a_library(test: &str) -> Option<Fixture> {
        let arch = match std::env::consts::ARCH {
            "x86_64" => "amd64",
            "aarch64" => "arm64",
            other => other,
        };
        let os = match std::env::consts::OS {
            "macos" => "darwin",
            other => other,
        };
        if !crate::ocifetch::constants::PLATFORMS
            .iter()
            .any(|p| p.os == os && p.architecture == arch)
        {
            eprintln!(
                "SKIPPED (loudly): registry_flight_tests::{test}: this host ({os}-{arch}) is not a chtypes platform; nothing was exercised"
            );
            return None;
        }
        let predicate = json!({
            "abi": channel::DEV_ABI_GENERATION,
            "abi_fingerprint": DEV_ABI_FINGERPRINT,
            "clickhouse_version": "26.8.15.10",
            "clickhouse_minor": "26.8",
            "channel": "lts",
            "build": "20261001.183455",
            "os": os,
            "arch": arch,
            "library": LIBRARY_NAME,
            "library_sha256": sha256_hex(NOT_A_LIBRARY),
            "library_bytes": NOT_A_LIBRARY.len(),
        });
        Some(Fixture::serve(
            test,
            &FETCH_CASES,
            NOT_A_LIBRARY,
            &predicate,
        ))
    }

    /// `library` as tag 26.8 of every case in `cases`, signed with the test
    /// key over `predicate`, from a fixture server of its own.
    fn serve(test: &str, cases: &[&str], library: &[u8], predicate: &Value) -> Fixture {
        static SEQ: AtomicUsize = AtomicUsize::new(0);
        let work = std::env::temp_dir().join(format!(
            "chtypes_registry_flight_{}_{}_{test}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = std::fs::remove_dir_all(&work);
        let fixtures = work.join("fixtures");
        let (key, public_hex, keyid) = test_signing_key();
        let layer = write_route_tree(
            &fixtures.join("trees/stub/v2/chtypes/v1"),
            "26.8",
            library,
            predicate,
            &key,
            &keyid,
        );
        let cases: Vec<Value> = cases
            .iter()
            .map(|id| json!({"id": id, "tree": "stub"}))
            .collect();
        put(
            &fixtures,
            "cases.json",
            &serde_json::to_vec(&json!({"schema": 1, "cases": cases})).unwrap(),
        );

        let script = repo_root().join("scripts/fetch-v1/server.py");
        let mut child = Command::new("python3")
            .arg(&script)
            .arg("--fixtures")
            .arg(&fixtures)
            .arg("--port")
            .arg("0")
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap_or_else(|e| panic!("starting python3 {}: {e}", script.display()));
        let mut reader = BufReader::new(child.stdout.take().expect("the server's stdout"));
        let mut line = String::new();
        reader
            .read_line(&mut line)
            .expect("read the LISTENING line");
        let fields: Vec<&str> = line.split_whitespace().collect();
        assert!(
            fields.len() == 3 && fields[0] == "LISTENING",
            "{} printed {line:?}, want LISTENING <port> <port2>",
            script.display()
        );
        let origin = format!("http://127.0.0.1:{}", fields[1]);
        // Keep draining stdout so the server never blocks on a full pipe.
        std::thread::spawn(move || {
            let _ = std::io::copy(&mut reader, &mut std::io::sink());
        });
        Fixture {
            server: Server { child, origin },
            cache: work.join("cache"),
            work,
            trusted: public_hex,
            layer_path: format!("/chtypes/v1/blobs/{layer}"),
        }
    }

    /// Autofetch on, the one base `case_id`'s repository on the server.
    fn registry(&self, case_id: &str) -> Registry {
        Registry::new(RegistryOptions {
            fetch: FetchOptions {
                bases: Some(vec![format!(
                    "{}/s-{case_id}/chtypes/v1",
                    self.server.origin
                )]),
                cache_dir: Some(self.cache.to_string_lossy().into_owned()),
                system_dirs: Some(Vec::new()),
                trusted_keys: Some(vec![self.trusted.clone()]),
                ..Default::default()
            },
            autofetch: Some(true),
            preload: Vec::new(),
        })
        .expect("construction opens nothing")
    }

    /// GET one of the server's own endpoints and decode its JSON answer.
    fn control(&self, path: &str) -> Value {
        let url = format!("{}{path}", self.server.origin);
        let mut response = ureq::Agent::new_with_defaults()
            .get(&url)
            .call()
            .unwrap_or_else(|e| panic!("GET {url}: {e}"));
        let body = response
            .body_mut()
            .read_to_vec()
            .unwrap_or_else(|e| panic!("GET {url}: reading the body: {e}"));
        serde_json::from_slice(&body).unwrap_or_else(|e| panic!("GET {url}: {e}"))
    }

    /// Hold every later request of `case_id` at the server, until the
    /// returned gate is dropped (or `open_gate`).
    fn close_gate(&self, case_id: &'static str) -> Gate<'_> {
        self.control(&format!("/_gate/close/s-{case_id}"));
        Gate { fx: self, case_id }
    }

    fn open_gate(&self, case_id: &str) {
        self.control(&format!("/_gate/open/s-{case_id}"));
    }

    /// Once `n` of `case_id`'s requests are parked at its gate: how many are.
    fn wait_parked(&self, case_id: &str, n: usize) -> u64 {
        self.control(&format!("/_gate/parked/s-{case_id}?n={n}"))["parked"]
            .as_u64()
            .expect("a parked count")
    }

    /// `case_id`'s logged requests, counted by "METHOD path", the path
    /// relative to the case's own segment (`/chtypes/v1/...`).
    fn requests(&self, case_id: &str) -> HashMap<String, usize> {
        let prefix = format!("/v2/s-{case_id}");
        let mut out = HashMap::new();
        for entry in self
            .control(&format!("/_log/s-{case_id}"))
            .as_array()
            .expect("a request log")
        {
            let method = entry["method"].as_str().expect("a method");
            let path = entry["path"].as_str().expect("a path");
            let path = path.strip_prefix(&prefix).unwrap_or(path);
            *out.entry(format!("{method} {path}")).or_insert(0) += 1;
        }
        out
    }
}

/// Open `request` on a thread of its own (which takes the test seam, per
/// thread), and send the answer.
fn spawn_open(registry: &Arc<Registry>, request: &'static str, answer: mpsc::Sender<Opened>) {
    let registry = Arc::clone(registry);
    std::thread::spawn(move || {
        let _overrides = channel::allow_overrides_for_tests();
        let _ = answer.send(registry.for_version(request));
    });
}

/// A registry over `case_id` whose `on_wait` hook sends once per open that
/// starts waiting on an attempt.
fn hooked(fx: &Fixture, case_id: &str) -> (Arc<Registry>, mpsc::Receiver<()>) {
    let (tx, rx) = mpsc::channel();
    let mut registry = fx.registry(case_id);
    registry.on_wait = Some(Box::new(move |_request: &str| {
        let _ = tx.send(());
    }));
    (Arc::new(registry), rx)
}

/// The issue's regression: with 26.3's fetch held at the gate, an open of 26.8,
/// which the cache answers, returns while 26.3's request is still parked.
#[test]
fn an_installed_line_never_waits_for_another_lines_fetch() {
    let _overrides = channel::allow_overrides_for_tests();
    let Some(fx) = Fixture::new("an_installed_line_never_waits_for_another_lines_fetch") else {
        return;
    };
    fx.registry("flight-install")
        .for_version("26.8")
        .expect("installing 26.8");
    let registry = Arc::new(fx.registry("flight-held"));
    let _gate = fx.close_gate("flight-held");
    let (held_tx, held_rx) = mpsc::channel();
    spawn_open(&registry, "26.3", held_tx);
    fx.wait_parked("flight-held", 1);

    let (installed_tx, installed_rx) = mpsc::channel();
    spawn_open(&registry, "26.8", installed_tx);
    let Ok(installed) = installed_rx.recv_timeout(BOUND) else {
        panic!("for_version(26.8), installed, waited {BOUND:?} for 26.3's fetch, held at the gate");
    };
    let library = installed.expect("the installed 26.8 opens while 26.3 fetches");
    assert_eq!(library.version(), "26.8.15.10");
    assert!(
        held_rx.try_recv().is_err(),
        "for_version(26.3) returned while its fetch was held at the gate"
    );
    assert_eq!(fx.wait_parked("flight-held", 1), 1, "26.3's one request");

    fx.open_gate("flight-held");
    let held = held_rx
        .recv_timeout(BOUND)
        .expect("for_version(26.3) after the gate opened");
    let err = held.expect_err("nothing serves 26.3");
    assert!(err.code().is_some(), "an artifact error, got {err:?}");
    let made: Vec<String> = fx
        .requests("flight-held")
        .into_keys()
        .filter(|r| r.contains("/manifests/26.8"))
        .collect();
    assert!(
        made.is_empty(),
        "the open of installed 26.8 made requests: {made:?}"
    );
}

/// N opens of one uninstalled request, all waiting while its fetch is held,
/// make one fetch and all get its Library.
#[test]
fn concurrent_opens_of_one_request_share_one_fetch() {
    let _overrides = channel::allow_overrides_for_tests();
    let Some(fx) = Fixture::new("concurrent_opens_of_one_request_share_one_fetch") else {
        return;
    };
    let (registry, waiting) = hooked(&fx, "flight-one-fetch");
    let _gate = fx.close_gate("flight-one-fetch");
    let (tx, rx) = mpsc::channel();
    for _ in 0..N {
        spawn_open(&registry, "26.8", tx.clone());
    }
    for i in 1..=N {
        waiting
            .recv_timeout(BOUND)
            .unwrap_or_else(|_| panic!("open {i} of {N} never waited"));
    }
    assert_eq!(
        fx.wait_parked("flight-one-fetch", 1),
        1,
        "{N} opens waiting on one attempt park one request"
    );
    fx.open_gate("flight-one-fetch");

    let libraries: Vec<Arc<Library>> = (1..=N)
        .map(|i| {
            rx.recv_timeout(BOUND)
                .unwrap_or_else(|_| panic!("open {i} of {N} never answered"))
                .unwrap_or_else(|e| panic!("open {i} of {N}: {e}"))
        })
        .collect();
    assert!(
        libraries.iter().all(|l| Arc::ptr_eq(l, &libraries[0])),
        "one request, one Library"
    );
    let requests = fx.requests("flight-one-fetch");
    assert_eq!(
        requests.get("GET /chtypes/v1/manifests/26.8"),
        Some(&1),
        "the tag, once: {requests:?}"
    );
    assert_eq!(
        requests.get(&format!("GET {}", fx.layer_path)),
        Some(&1),
        "the layer, once: {requests:?}"
    );
    let again = registry.for_version("26.8").expect("the memo");
    assert!(Arc::ptr_eq(&again, &libraries[0]));
    assert_eq!(registry.libraries().len(), 1);
}

/// N opens of a request nothing serves share one failing fetch and all get its
/// error; the next open makes a new request.
#[test]
fn a_failed_fetch_reaches_every_waiter_and_is_not_remembered() {
    let _overrides = channel::allow_overrides_for_tests();
    let Some(fx) = Fixture::new("a_failed_fetch_reaches_every_waiter_and_is_not_remembered") else {
        return;
    };
    let (registry, waiting) = hooked(&fx, "flight-fails");
    let _gate = fx.close_gate("flight-fails");
    let (tx, rx) = mpsc::channel();
    for _ in 0..N {
        spawn_open(&registry, "26.3", tx.clone());
    }
    for i in 1..=N {
        waiting
            .recv_timeout(BOUND)
            .unwrap_or_else(|_| panic!("open {i} of {N} never waited"));
    }
    fx.wait_parked("flight-fails", 1);
    fx.open_gate("flight-fails");

    let errors: Vec<crate::Error> = (1..=N)
        .map(|i| {
            rx.recv_timeout(BOUND)
                .unwrap_or_else(|_| panic!("open {i} of {N} never answered"))
                .expect_err("nothing serves 26.3")
        })
        .collect();
    let first = &errors[0];
    assert!(first.code().is_some(), "an artifact error, got {first:?}");
    assert!(
        errors.iter().all(|e| e == first),
        "one attempt, one error: {errors:?}"
    );
    let tag = "GET /chtypes/v1/manifests/26.3";
    assert_eq!(fx.requests("flight-fails").get(tag), Some(&1));
    let again = registry
        .for_version("26.3")
        .expect_err("nothing serves 26.3");
    assert_eq!(again.code(), first.code());
    assert_eq!(
        fx.requests("flight-fails").get(tag),
        Some(&2),
        "a failure is never remembered"
    );
}

/// `Registry::fetch` installs the build and returns its record, and opens
/// nothing: the library it installed cannot be loaded and no `Library` exists.
/// The installed lookup an open makes then answers with no request, this
/// process holds the build (a prune's claim of it is in use), and a second
/// fetch finds it already installed and requests no layer.
#[test]
fn a_fetch_installs_without_opening() {
    let _overrides = channel::allow_overrides_for_tests();
    let Some(fx) = Fixture::not_a_library("a_fetch_installs_without_opening") else {
        return;
    };
    let registry = fx.registry("fetch-installs");
    let resolved = registry.fetch("26.8").expect("fetch(26.8)");
    assert_eq!(
        (
            resolved.version.as_str(),
            resolved.build.as_str(),
            resolved.request.as_str()
        ),
        ("26.8.15.10", "20261001.183455", "26.8")
    );
    let hex = resolved
        .dir
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    assert_eq!(resolved.digests.manifest, format!("sha256:{hex}"));
    assert_eq!(
        std::fs::read(&resolved.library_path).ok().as_deref(),
        Some(NOT_A_LIBRARY),
        "the installed library at {}",
        resolved.library_path.display()
    );
    assert!(
        registry.libraries().is_empty(),
        "fetch opened a library; it must open nothing"
    );
    // The installed lookup an open makes now answers, with no request.
    let found = ensure::resolve_installed(
        "26.8",
        &resolved.platform,
        Options {
            cache_dir: Some(fx.cache.to_string_lossy().into_owned()),
            system_dirs: Vec::new(),
            ..Options::default()
        },
    )
    .expect("the installed lookup")
    .expect("the fetched build answers 26.8");
    assert_eq!(found.digests.manifest, resolved.digests.manifest);
    // This process holds the build: a prune's exclusive claim of it is in use.
    assert!(
        matches!(hold::claim(&resolved.dir), Claim::InUse),
        "the fetched build is not held"
    );
    assert_eq!(hold::hold(&resolved.dir), HoldState::Held);
    // A second fetch is answered again, and keeps the build.
    let again = registry.fetch("26.8").expect("the second fetch(26.8)");
    assert_eq!(again.digests.manifest, resolved.digests.manifest);
    assert!(again.already_installed, "the second fetch installed again");
    let requests = fx.requests("fetch-installs");
    assert_eq!(
        requests.get(&format!("GET {}", fx.layer_path)),
        Some(&1),
        "the layer, once over two fetches: {requests:?}"
    );
    assert!(registry.libraries().is_empty());
}

/// With the fetch held at the gate, a fetch and an open of the same request
/// both wait on ONE fetch; when it lands the fetch has the build, the open
/// goes on to its load (which fails: the bytes are no library), and the server
/// saw one tag request and one layer request.
#[test]
fn a_fetch_and_an_open_of_one_request_share_one_fetch() {
    let _overrides = channel::allow_overrides_for_tests();
    let Some(fx) = Fixture::not_a_library("a_fetch_and_an_open_of_one_request_share_one_fetch")
    else {
        return;
    };
    let (tx, waiting) = mpsc::channel();
    let mut registry = fx.registry("fetch-shared");
    registry.on_fetch_wait = Some(Box::new(move |_request: &str| {
        let _ = tx.send(());
    }));
    let registry = Arc::new(registry);
    let _gate = fx.close_gate("fetch-shared");

    let (fetched_tx, fetched_rx) = mpsc::channel();
    {
        let registry = Arc::clone(&registry);
        std::thread::spawn(move || {
            let _overrides = channel::allow_overrides_for_tests();
            let _ = fetched_tx.send(registry.fetch("26.8"));
        });
    }
    waiting
        .recv_timeout(BOUND)
        .expect("the fetch never waited on its fetch");
    let (opened_tx, opened_rx) = mpsc::channel();
    spawn_open(&registry, "26.8", opened_tx);
    waiting
        .recv_timeout(BOUND)
        .expect("the open never waited on the same fetch");
    assert_eq!(
        fx.wait_parked("fetch-shared", 1),
        1,
        "a fetch and an open waiting on one fetch park one request"
    );
    fx.open_gate("fetch-shared");

    let fetched = fetched_rx
        .recv_timeout(BOUND)
        .expect("the fetch never answered")
        .expect("the fetch sharing the fetch");
    assert_eq!(fetched.version, "26.8.15.10");
    let opened = opened_rx
        .recv_timeout(BOUND)
        .expect("the open never answered");
    let err = opened.expect_err("bytes that are no library never open");
    assert!(err.code().is_some(), "a load refusal, got {err:?}");
    let requests = fx.requests("fetch-shared");
    assert_eq!(
        requests.get("GET /chtypes/v1/manifests/26.8"),
        Some(&1),
        "the tag, once: {requests:?}"
    );
    assert_eq!(
        requests.get(&format!("GET {}", fx.layer_path)),
        Some(&1),
        "the layer, once: {requests:?}"
    );
    assert!(registry.libraries().is_empty());
}

/// A request nothing serves is the fetch layer's own code, a refused spelling
/// is misuse, and an offline fetch with nothing installed is
/// `CHTYPES_ARTIFACT_MISSING`.
#[test]
fn a_fetch_fails_with_the_fetch_codes() {
    let _overrides = channel::allow_overrides_for_tests();
    let Some(fx) = Fixture::not_a_library("a_fetch_fails_with_the_fetch_codes") else {
        return;
    };
    let registry = fx.registry("fetch-unpublished");
    let err = registry.fetch("26.3").expect_err("nothing serves 26.3");
    assert_eq!(
        err.code(),
        Some(crate::code::ARTIFACT_UNPUBLISHED),
        "{err:?}"
    );
    assert!(
        matches!(registry.fetch("v26.8"), Err(Error::Usage(_))),
        "a refused spelling is misuse"
    );
    let offline = Registry::new(RegistryOptions {
        fetch: FetchOptions {
            cache_dir: Some(fx.work.join("offline").to_string_lossy().into_owned()),
            system_dirs: Some(Vec::new()),
            offline: true,
            ..Default::default()
        },
        autofetch: Some(false),
        preload: Vec::new(),
    })
    .expect("construction opens nothing");
    let err = offline
        .fetch("26.8")
        .expect_err("nothing installed, and offline");
    assert_eq!(err.code(), Some(crate::code::ARTIFACT_MISSING), "{err:?}");
}
