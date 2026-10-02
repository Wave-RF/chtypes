"""TestConformanceV1 — the Python v1 conformance runner (docs/guides/fetch-v1.md
"Conformance", PLAN §3.3): ``uv run --python <v> pytest -q
tests/ocifetch/test_conformance.py``.

Reads ``CHTYPES_V1_CONFORMANCE=<absolute tests/fixtures/fetch-v1 path>``.
Unset, or a fixtures tree with no ``cases.json`` yet, every case here is
skipped LOUDLY, by name — the lane 0B fixtures/server/parity lane owns that
tree and has not merged into this worktree, so a run here today skips
everything by design (PLAN §4 Lane Python: "conformance stays red until 0B
merges; that's expected" — RED here specifically because v1.yml's
`v1-constants`/`v1-fixtures`/`v1-parity` require real cases.json contents
``parity.py`` can check; an entirely-skipped run is what proves this file is
WIRED, not what makes the merge gate green).

Writes ``CHTYPES_V1_REPORT`` (schema `spec/fetch-v1/schema/report.schema.json`)
when both env vars are set, so a hosted CI run always leaves a report behind
for the parity gate even on a run this file cannot fully drive without
lane 0B's server and route trees.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from chtypes._ocifetch._dsse import TrustedKey, fixture_trusted_keys, release_trusted_keys
from chtypes._ocifetch._ensure import Options, Request, ensure
from chtypes._ocifetch._errors import FetchError
from chtypes._ocifetch._http import TransportError
from chtypes._ocifetch._lock import load_lock, new_lock, save_lock

FIXTURES_ENV = "CHTYPES_V1_CONFORMANCE"
REPORT_ENV = "CHTYPES_V1_REPORT"
BINDING = "python"


def _toolchain() -> str:
    return f"python{sys.version_info.major}.{sys.version_info.minor}"


def _fixtures_root() -> Path:
    raw = os.environ.get(FIXTURES_ENV)
    if not raw:
        pytest.skip(
            f"{FIXTURES_ENV} is not set. This is expected before lane 0B (fixtures, server, "
            f"parity) merges into this branch; once it has, set it to an absolute "
            f"tests/fixtures/fetch-v1 path to run this suite "
            f'(docs/guides/fetch-v1.md "Conformance").'
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
    """Starts `scripts/fetch-v1/server.py` once for the module. Yields
    `(port, second_origin_port)`, or `None` when the script does not exist
    yet (lane 0B), in which case every http-transport case is skipped."""
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
            raise RuntimeError(f"scripts/fetch-v1/server.py: unexpected startup line {line!r}")
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
) -> str:
    if template != "{base}":
        return template  # a second, non-templated mirror base, used verbatim.
    if transport == "file":
        return f"file://{fixtures_root}/trees/{tree}/v2/chtypes/v1"
    if transport == "http":
        if http_port is None:
            pytest.skip("scripts/fetch-v1/server.py is not available yet (lane 0B)")
        return f"http://127.0.0.1:{http_port}/s-{case_id}/chtypes/v1"
    if transport == "registry":
        pytest.skip("the registry transport runs only in the v1-network CI job")
    raise ValueError(f"unknown transport {transport!r}")


def _trusted_keys_for(trust: str) -> tuple[TrustedKey, ...]:
    if trust == "test":
        return fixture_trusted_keys()
    return release_trusted_keys()


def _run_case(
    case: dict,
    transport: str,
    *,
    fixtures_root: Path,
    tmp_path: Path,
    http_port: int | None,
) -> tuple[str, str]:
    """Drives one (case, transport) pair through `ensure()`. Returns
    (verdict, detail) for the report (schema: "pass"/"fail")."""
    req = case["request"]
    setup = case["setup"]
    expect = case["expect"]

    bases = tuple(
        _expand_base(
            b, transport=transport, fixtures_root=fixtures_root, tree=case["tree"],
            case_id=case["id"], http_port=http_port,
        )
        for b in req["bases"]
    )

    cache_dir = tmp_path / "cache"
    if setup.get("cache") and setup["cache"] != "empty":
        seed = fixtures_root / "layouts" / setup["cache"]
        if seed.is_dir():
            import shutil

            shutil.copytree(seed, cache_dir)

    lock_path = tmp_path / "chtypes.lock"
    if setup.get("lock"):
        lock_fixture = fixtures_root / "locks" / f"{setup['lock']}.json"
        if lock_fixture.is_file():
            save_lock(lock_path, load_lock(lock_fixture))
    elif req.get("lock_write"):
        save_lock(lock_path, new_lock())

    options = Options(
        platform=req["platform"],
        bases=bases,
        cache_dir=cache_dir,
        trusted_keys=_trusted_keys_for(req["trust"]),
        allow_unsigned=req["allow_unsigned"],
        offline=req["offline"],
        frozen=req["frozen"],
        lock_path=lock_path if (setup.get("lock") or req.get("lock_write")) else None,
        lock_write=req["lock_write"],
        update=req["update"],
    )

    try:
        resolved = ensure(Request(req["spelling"]), options)
    except (FetchError, TransportError) as e:
        code = getattr(e, "code", None)
        if not expect["ok"] and code == expect["code"]:
            return "pass", ""
        return (
            "fail",
            f"expected ok={expect['ok']} code={expect['code']!r}, got {type(e).__name__}: {e}",
        )
    except ValueError as e:
        # A client-side refusal (e.g. a bad spelling) has no `code`.
        if not expect["ok"] and expect["code"] is None:
            return "pass", ""
        return "fail", f"unexpected ValueError: {e}"

    if not expect["ok"]:
        return "fail", f"expected failure {expect['code']!r}, got a resolved artifact"
    mismatches = []
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
    if mismatches:
        return "fail", "; ".join(mismatches)
    return "pass", ""


def test_conformance_v1(
    fixtures_root: Path, cases_doc: dict, cases_sha256: str, http_server, tmp_path_factory
) -> None:
    results = []
    for case in cases_doc["cases"]:
        for transport in case["transports"]:
            case_tmp = tmp_path_factory.mktemp(f"{case['id']}-{transport}")
            http_port = http_server[0] if http_server else None
            try:
                verdict, detail = _run_case(
                    case, transport, fixtures_root=fixtures_root, tmp_path=case_tmp,
                    http_port=http_port,
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
