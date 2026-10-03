"""TestConformanceV1 — the Python v1 conformance runner (docs/guides/fetch-v1.md
"Conformance", PLAN §3.3): `uv run --python <v> pytest -q
tests/ocifetch/test_conformance.py`.

Reads `CHTYPES_V1_CONFORMANCE=<absolute tests/fixtures/fetch-v1 path>`.
Unset, or a fixtures tree with no `cases.json` yet, every case here is
skipped LOUDLY, by name, never a silent pass.

Writes `CHTYPES_V1_REPORT` (schema `spec/fetch-v1/schema/report.schema.json`)
when both env vars are set, one JSON file per toolchain leg, for the parity
gate to read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import chtypes._ocifetch._ensure as _ensure_module
import chtypes._ocifetch._http as _http_module
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import (
    TrustedKey,
    fixture_trusted_keys,
    release_trusted_keys,
    verify_bundle,
)
from chtypes._ocifetch._ensure import Clock, Options, Request, ensure, fetch_signed
from chtypes._ocifetch._errors import FetchError
from chtypes._ocifetch._http import TransportError
from chtypes._ocifetch._layout import VerifiedRecord, resolve_cache_root, write_verified_install
from chtypes._ocifetch._oci import manifest_layer_descriptor, manifest_single_layer, parse_digest
from chtypes._ocifetch._unpack import unpack_tar_zst

FIXTURES_ENV = "CHTYPES_V1_CONFORMANCE"
REPORT_ENV = "CHTYPES_V1_REPORT"
BINDING = "python"


def _toolchain() -> str:
    # Bare "3.11"/"3.13"/"3.14" — scripts/fetch-v1/parity.py's TOOLCHAINS
    # dict matches this exact string against v1.yml's matrix.toolchain
    # value (also bare); report.schema.json's own description shows a
    # "python3.13"-prefixed example, but parity.py is the actual gate and
    # it does a plain membership test against the bare version, not that
    # illustrative string.
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _fixtures_root() -> Path:
    raw = os.environ.get(FIXTURES_ENV)
    if not raw:
        pytest.skip(
            f"{FIXTURES_ENV} is not set. Set it to an absolute tests/fixtures/fetch-v1 path "
            f'to run this suite (docs/guides/fetch-v1.md "Conformance").'
        )
    root = Path(raw).resolve()
    if not (root / "cases.json").is_file():
        pytest.skip(f"{FIXTURES_ENV}={root} has no cases.json yet — nothing to run")
    return root


@pytest.fixture(scope="module")
def fixtures_root() -> Path:
    return _fixtures_root()


@pytest.fixture(scope="module")
def cases_doc(fixtures_root: Path) -> dict:
    with open(fixtures_root / "cases.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def cases_sha256(fixtures_root: Path) -> str:
    return hashlib.sha256((fixtures_root / "cases.json").read_bytes()).hexdigest()


def _repo_root(fixtures_root: Path) -> Path:
    # <repo>/tests/fixtures/fetch-v1 -> <repo>.
    return fixtures_root.parents[2]


@pytest.fixture(scope="module")
def http_server(fixtures_root: Path):
    """Starts the fixtures/server/parity lane's scripted HTTP server once
    for the module. Yields `(port, second_origin_port)`."""
    server_script = _repo_root(fixtures_root) / "scripts" / "fetch-v1" / "server.py"
    if not server_script.is_file():
        yield None
        return
    proc = subprocess.Popen(
        [sys.executable, str(server_script), "--fixtures", str(fixtures_root), "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        line = proc.stdout.readline() if proc.stdout else ""
        parts = line.split()
        if len(parts) < 3 or parts[0] != "LISTENING":
            proc.terminate()
            raise RuntimeError(f"{server_script.name}: unexpected startup line {line!r}")
        yield (int(parts[1]), int(parts[2]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _expand_base(
    template: str,
    *,
    transport: str,
    fixtures_root: Path,
    tree: str,
    case_id: str,
    http_port: int | None,
    http_port2: int | None = None,
) -> str:
    if "{base}" not in template and "{base2}" not in template:
        return template  # a second, non-templated mirror base, used verbatim.
    if transport == "file":
        base = f"file://{fixtures_root}/trees/{tree}/v2/chtypes/v1"
        base2 = base  # file transport has no second origin; unused by any file case.
    elif transport == "http":
        if http_port is None:
            pytest.skip("the fixtures/server/parity lane's HTTP server is not available")
        base = f"http://127.0.0.1:{http_port}/s-{case_id}/chtypes/v1"
        # docs/guides/fetch-v1.md "{base2}": server.py's SECOND origin, same
        # s-<case-id>/chtypes/v1 suffix, its own response-sequence cursor
        # (keyed on (case id, origin, method, path)) — for a case needing two
        # genuinely independent bases over one case id (mirror-failover-*).
        if "{base2}" in template:
            if http_port2 is None:
                pytest.skip("the fixtures/server/parity lane's second origin is not available")
            base2 = f"http://127.0.0.1:{http_port2}/s-{case_id}/chtypes/v1"
        else:
            base2 = base
    elif transport == "registry":
        pytest.skip("the registry transport runs only in the v1-network CI job")
        raise AssertionError("unreachable")  # pytest.skip always raises
    else:
        raise ValueError(f"unknown transport {transport!r}")
    # A suffix like "{base}/does-not-exist" (frozen-mirror) is a SUBSTRING
    # substitution, not an exact-match template — a deliberately unreachable
    # mirror ahead of the real one.
    return template.replace("{base2}", base2).replace("{base}", base)


def _trusted_keys_for(trust: str) -> tuple[TrustedKey, ...]:
    if trust == "test":
        return fixture_trusted_keys()
    return release_trusted_keys()


# ---------------------------------------------------------------------------
# Instrumentation: a fake clock (so retry cases run instantly and `sleeps` is
# asserted exactly) and a request log (so `requests.max`/`none_matching` can
# be checked) — both transport-agnostic, wired at `_http.py`'s own two real
# network/filesystem choke points (`_fetch_file`, `_http_get_once`), which
# `fetch_from_bases` always calls by bare name from its OWN module, so
# patching them here intercepts every real attempt regardless of which
# higher module imported `fetch_from_bases` by reference.
# ---------------------------------------------------------------------------


class _FakeClock(Clock):
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self._now = 1_700_000_000.0

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds


class _RequestLog:
    def __init__(self) -> None:
        self.entries: list[str] = []

    def clear(self) -> None:
        self.entries.clear()


@pytest.fixture
def request_log(monkeypatch: pytest.MonkeyPatch) -> _RequestLog:
    log = _RequestLog()
    real_fetch_file = _http_module._fetch_file
    real_http_get_once = _http_module._http_get_once

    def logging_fetch_file(*args, **kwargs):
        base, path = args[0], args[1]
        log.entries.append(f"GET {base}{path}")
        return real_fetch_file(*args, **kwargs)

    def logging_http_get_once(*args, **kwargs):
        log.entries.append(f"GET {args[0]}")
        return real_http_get_once(*args, **kwargs)

    monkeypatch.setattr(_http_module, "_fetch_file", logging_fetch_file)
    monkeypatch.setattr(_http_module, "_http_get_once", logging_http_get_once)
    return log


def _check_requests(log: _RequestLog, expect_requests: dict) -> str | None:
    if expect_requests["max"] is not None and len(log.entries) > expect_requests["max"]:
        return (
            f"requests.max={expect_requests['max']} but {len(log.entries)} request(s) were "
            f"made: {log.entries}"
        )
    for pattern in expect_requests["none_matching"]:
        for entry in log.entries:
            if re.search(pattern, entry):
                return f"requests.none_matching {pattern!r} matched {entry!r}"
    return None


# ---------------------------------------------------------------------------
# installed.json pre-install (docs/guides/fetch-v1.md §10 "Cache fixtures and
# installed.json"): a test-setup instruction, never a runtime format any
# binding parses. Driven entirely from the layout's own local blobs, never
# the network, before the case's own clock starts.
# ---------------------------------------------------------------------------


def _preinstall_from_layout(
    *,
    layout_dir: Path,
    manifest_digest: str,
    cache_root: Path,
    trusted_keys: tuple[TrustedKey, ...],
) -> None:
    manifest_hex = parse_digest(manifest_digest)
    blobs_dir = layout_dir / "blobs" / "sha256"
    manifest_doc = json.loads((blobs_dir / manifest_hex).read_bytes())
    layer_desc = manifest_layer_descriptor(manifest_doc)

    # A layout has no referrers/ index: find the signature referrer by
    # scanning every blob for one whose own `subject` names this manifest.
    verified = None
    bundle_digest = None
    for blob_path in blobs_dir.iterdir():
        try:
            doc = json.loads(blob_path.read_bytes())
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict) or doc.get("mediaType") != C.MEDIA_TYPE_MANIFEST:
            continue
        subject = doc.get("subject") or {}
        if subject.get("digest") != manifest_digest:
            continue
        try:
            referrer_layer = manifest_single_layer(doc, expected_media_type=C.MEDIA_TYPE_BUNDLE)
        except Exception:
            continue
        bundle_bytes = (blobs_dir / parse_digest(referrer_layer.digest)).read_bytes()
        candidate = verify_bundle(json.loads(bundle_bytes), trusted_keys)
        if candidate is not None:
            verified = candidate
            bundle_digest = referrer_layer.digest
            break
    if verified is None:
        raise RuntimeError(
            f"pre-install: no trusted signature for {manifest_digest} in {layout_dir}"
        )

    predicate = verified.statement.predicate
    platform_key = next(
        p["key"]
        for p in C.PLATFORMS
        if p["os"] == predicate["os"] and p["architecture"] == predicate["arch"]
    )
    layer_path = blobs_dir / parse_digest(layer_desc.digest)
    scratch = cache_root / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(dir=str(scratch))
    try:
        unpacked_dir = os.path.join(tmp_dir, "unpacked")
        unpack_tar_zst(
            str(layer_path),
            unpacked_dir,
            library_name=predicate["library"],
            library_sha256=predicate["library_sha256"],
            library_bytes=predicate["library_bytes"],
        )
        record = VerifiedRecord(
            schema=1,
            manifest=manifest_digest,
            layer=layer_desc.digest,
            bundle=bundle_digest or "",
            index=None,
            platform=platform_key,
            version=predicate["clickhouse_version"],
            build=predicate["build"],
            channel=predicate.get("channel"),
            predicate=predicate,
            signed_by=verified.signed_by,
            library=predicate["library"],
        )
        write_verified_install(cache_root, manifest_digest, record, unpacked_tmp_dir=unpacked_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _index_race_hook(cache_dir: Path):
    """ "index-race-reapply": simulates a competing writer landing between
    our temp-write and our rename (`_layout.update_index_json`'s own
    before-rename hook point), proving the real fetch still succeeds."""

    def hook() -> None:
        index_path = cache_dir / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        competing = {"schemaVersion": 2, "manifests": [{"digest": "sha256:" + "0" * 64}]}
        index_path.write_text(json.dumps(competing))

    return hook


_BEFORE_INDEX_RENAME_HOOKS = {"index-race-reapply": _index_race_hook}


def _chmod_tree(root: Path, *, dir_mode: int, file_mode: int) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), file_mode)
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        os.chmod(dirpath, dir_mode)


def _install_system_dirs(fixtures_root: Path, names: list[str], tmp_path: Path) -> tuple[Path, ...]:
    """cases.schema.json `setup.system_dirs` names `layouts/<name>/` fixtures
    to make available as docs/guides/fetch-v1.md §1's "read-only system
    directories, searched after [the cache], never written to" —
    `_constants.SYSTEM_CACHE_DIRS` is a pair of real absolute machine paths
    (`/usr/local/share/chtypes/v1`, `/opt/chtypes/v1`), so this harness
    never points at them directly: it copies the named layout into a scratch
    dir, chmods it read-only to actually enforce "never written to," and the
    caller monkeypatches `_ensure.search_roots` to return it in their place.
    """
    dirs = []
    for i, name in enumerate(names):
        src = fixtures_root / "layouts" / name
        dst = tmp_path / f"system-dir-{i}"
        shutil.copytree(src, dst)
        _chmod_tree(dst, dir_mode=0o555, file_mode=0o444)
        dirs.append(dst)
    return tuple(dirs)


def _run_case(
    case: dict,
    transport: str,
    *,
    fixtures_root: Path,
    tmp_path: Path,
    http_port: int | None,
    http_port2: int | None,
    request_log: _RequestLog,
) -> tuple[str, str]:
    """Drives one (case, transport) pair through `ensure()`. Returns
    (verdict, detail) for the report (schema: "pass"/"fail")."""
    req = case["request"]
    setup = case["setup"]
    expect = case["expect"]
    trusted_keys = _trusted_keys_for(req["trust"])

    bases = tuple(
        _expand_base(
            b,
            transport=transport,
            fixtures_root=fixtures_root,
            tree=case["tree"],
            case_id=case["id"],
            http_port=http_port,
            http_port2=http_port2,
        )
        for b in req["bases"]
    )

    cache_dir = tmp_path / "cache"
    if setup.get("cache") and setup["cache"] != "empty":
        seed = fixtures_root / "layouts" / setup["cache"]
        if seed.is_dir():
            shutil.copytree(seed, cache_dir)
            installed_doc_path = cache_dir / "installed.json"
            if installed_doc_path.is_file():
                installed = json.loads(installed_doc_path.read_text())
                for digest in installed.get("installed", []):
                    _preinstall_from_layout(
                        layout_dir=cache_dir,
                        manifest_digest=digest,
                        cache_root=cache_dir,
                        trusted_keys=trusted_keys,
                    )

    system_dir_names = setup.get("system_dirs") or []
    system_dirs = (
        _install_system_dirs(fixtures_root, system_dir_names, tmp_path) if system_dir_names else ()
    )

    lock_path = tmp_path / "chtypes.lock"
    if setup.get("lock"):
        lock_fixture = fixtures_root / "locks" / "inputs" / f"{setup['lock']}.json"
        if lock_fixture.is_file():
            # A raw copy, NEVER a load_lock()/save_lock() round-trip: several
            # input fixtures (invalid-abi-2, invalid-schema-2) are
            # DELIBERATELY invalid, and the refusal they drive is ensure()'s
            # own job to raise at the right moment with the right code, not
            # something this harness should pre-validate and crash on
            # before the case even starts.
            shutil.copyfile(lock_fixture, lock_path)

    hook_name = setup.get("before_index_rename_hook")
    before_index_rename = None
    if hook_name:
        hook_factory = _BEFORE_INDEX_RENAME_HOOKS.get(hook_name)
        if hook_factory is None:
            return "fail", f"unknown before_index_rename_hook {hook_name!r} — not implemented"
        before_index_rename = hook_factory(cache_dir)

    clock = _FakeClock()
    request_log.clear()
    options = Options(
        platform=req["platform"],
        bases=bases,
        cache_dir=cache_dir,
        trusted_keys=trusted_keys,
        allow_unsigned=req["allow_unsigned"],
        offline=req["offline"],
        frozen=req["frozen"],
        lock_path=lock_path if (setup.get("lock") or req.get("lock_write")) else None,
        lock_write=req["lock_write"],
        update=req["update"],
        clock=clock,
        before_index_rename=before_index_rename,
    )

    mismatches: list[str] = []
    resolved = None
    generic_result = None
    # docs/guides/fetch-v1.md §10 "The generic-fetch convention": a case id
    # starting goldens-/fixtures- exercises fetch_signed(), not ensure() —
    # cases.schema.json has no separate shape for this, so the convention is
    # keyed on the id prefix, same as the generator itself.
    is_generic = case["id"].startswith(("goldens-", "fixtures-"))
    saved_search_roots = _ensure_module.search_roots
    if system_dirs:

        def _fake_search_roots(cache_dir=None, _system_dirs=system_dirs):
            return (resolve_cache_root(cache_dir), *_system_dirs)

        _ensure_module.search_roots = _fake_search_roots
    try:
        if is_generic:
            is_goldens = case["id"].startswith("goldens-")
            predicate_type = C.PREDICATE_TYPE_GOLDENS if is_goldens else C.PREDICATE_TYPE_FIXTURES
            # _ensure.fetch_signed's own docstring: `repository` is a path
            # suffix onto every base — "" for goldens (same repository as
            # the platform artifact), C.FIXTURES_REPO_SUFFIX
            # ("/sdk-fetch-fixtures") for fixtures.
            repository = "" if is_goldens else C.FIXTURES_REPO_SUFFIX
            generic_result = fetch_signed(repository, req["spelling"], predicate_type, options)
        else:
            resolved = ensure(Request(req["spelling"]), options)
    except (FetchError, TransportError) as e:
        code = getattr(e, "code", None)
        if expect["ok"] or code != expect["code"]:
            mismatches.append(
                f"expected ok={expect['ok']} code={expect['code']!r}, got {type(e).__name__}: {e}"
            )
    except ValueError as e:
        # A client-side refusal (e.g. a bad spelling) has no `code`.
        if expect["ok"] or expect["code"] is not None:
            mismatches.append(f"unexpected ValueError: {e}")
    finally:
        _ensure_module.search_roots = saved_search_roots
        for d in system_dirs:
            _chmod_tree(d, dir_mode=0o755, file_mode=0o644)

    if generic_result is not None:
        if not expect["ok"]:
            mismatches.append(f"expected failure {expect['code']!r}, got a resolved artifact")
        else:
            actual_manifest = generic_result["digests"].get("manifest")
            if expect["manifest"] is not None and actual_manifest != expect["manifest"]:
                mismatches.append(f"manifest {actual_manifest!r} != {expect['manifest']!r}")
            if expect["library_sha256"] is not None:
                actual_sha = hashlib.sha256(Path(generic_result["path"]).read_bytes()).hexdigest()
                if actual_sha != expect["library_sha256"]:
                    mismatches.append(
                        f"library_sha256 {actual_sha!r} != {expect['library_sha256']!r}"
                    )

    if resolved is not None:
        if not expect["ok"]:
            mismatches.append(f"expected failure {expect['code']!r}, got a resolved artifact")
        else:
            if expect["version"] is not None and resolved.version != expect["version"]:
                mismatches.append(f"version {resolved.version!r} != {expect['version']!r}")
            if expect["build"] is not None and resolved.build != expect["build"]:
                mismatches.append(f"build {resolved.build!r} != {expect['build']!r}")
            actual_manifest = resolved.digests.get("manifest")
            if expect["manifest"] is not None and actual_manifest != expect["manifest"]:
                mismatches.append(f"manifest {actual_manifest!r} != {expect['manifest']!r}")
            if expect["library_sha256"] is not None:
                actual = resolved.predicate.get("library_sha256")
                if actual != expect["library_sha256"]:
                    mismatches.append(f"library_sha256 {actual!r} != {expect['library_sha256']!r}")
            for keyword in expect["warnings"]:
                if not any(keyword in w for w in resolved.warnings):
                    mismatches.append(
                        f"warnings: no warning contains {keyword!r}: {resolved.warnings}"
                    )

    if clock.sleeps != expect["sleeps"]:
        mismatches.append(f"sleeps {clock.sleeps} != {expect['sleeps']}")

    requests_mismatch = _check_requests(request_log, expect["requests"])
    if requests_mismatch:
        mismatches.append(requests_mismatch)

    if expect["lock_after"] is not None:
        expected_lock_path = fixtures_root / "locks" / "expected" / f"{expect['lock_after']}.json"
        expected_doc = json.loads(expected_lock_path.read_text())
        if not lock_path.is_file():
            mismatches.append(f"lock_after: expected a lock at {lock_path}, found none")
        else:
            actual_doc = json.loads(lock_path.read_text())
            if actual_doc != expected_doc:
                mismatches.append(
                    f"lock_after: lock file does not match {expect['lock_after']}.json: "
                    f"got {actual_doc}, want {expected_doc}"
                )

    if mismatches:
        return "fail", "; ".join(mismatches)
    return "pass", ""


def test_conformance_v1(
    fixtures_root: Path,
    cases_doc: dict,
    cases_sha256: str,
    http_server,
    tmp_path_factory,
    request_log: _RequestLog,
) -> None:
    results = []
    for case in cases_doc["cases"]:
        for transport in case["transports"]:
            case_tmp = tmp_path_factory.mktemp(f"{case['id']}-{transport}")
            http_port = http_server[0] if http_server else None
            http_port2 = http_server[1] if http_server else None
            try:
                verdict, detail = _run_case(
                    case,
                    transport,
                    fixtures_root=fixtures_root,
                    tmp_path=case_tmp,
                    http_port=http_port,
                    http_port2=http_port2,
                    request_log=request_log,
                )
            except pytest.skip.Exception:
                continue
            results.append(
                {"id": case["id"], "transport": transport, "verdict": verdict, "detail": detail}
            )

    report_path = os.environ.get(REPORT_ENV)
    if report_path:
        report = {
            "schema": 1,
            "binding": BINDING,
            "toolchain": _toolchain(),
            "cases_sha256": cases_sha256,
            "results": results,
        }
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)

    failures = [r for r in results if r["verdict"] == "fail"]
    if failures:
        detail = "\n".join(f"  {r['id']} ({r['transport']}): {r['detail']}" for r in failures)
        pytest.fail(f"{len(failures)}/{len(results)} v1 conformance cases failed:\n{detail}")
    if not results:
        pytest.fail("0 cases ran — the fixtures tree is present but produced no results")
