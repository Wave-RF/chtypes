"""The v1 goldens RUNNER for the Python binding (docs/guides/goldens-v1.md section 2).

It executes the cases of one release's goldens document and RECORDS what the
library returned. It never compares: `scripts/goldens-v1/compare.py` does that,
once, for all four bindings. Nothing here derives, re-serializes or interprets a
document: `document_b64` is the exact bytes the generated call layer returned.

THE PROCESS SPLIT. `chs_initialize` is process-once, so each `setups[]` entry runs
in a child process of its own: the parent (this test) fetches the release and the
goldens document, then, for every setup, re-executes THIS FILE with
`--child <setup id>` under the interpreter running the test. The child calls
`chtypes.setup(...)` (which becomes `chs_initialize` and `chs_set_defaults`, once,
at load), opens the library through the public `Registry`, and runs that setup's
cases; the parent merges the children's records into one report. A setup is never
run twice in a process, and two setups never share one.

INPUTS (the environment):

    CHTYPES_GOLDENS_REGISTRY_BASE   registry base, repository path included
    CHTYPES_GOLDENS_VERSION         the exact four-part ClickHouse version
    CHTYPES_GOLDENS_PLATFORM        linux-amd64 | linux-arm64 | darwin-arm64
    CHTYPES_GOLDENS_REPORT          where the report is written
    CHTYPES_GOLDENS_DOCUMENT        where the document's exact bytes are written

Without the first three the test SKIPS LOUDLY, by name, and exits 0. The fetch runs
in this process exactly as a user's process of this 2.0.0-dev SDK fetches: the ABI
v2 dev channel, its staging base and its staging key only, whatever base
CHTYPES_GOLDENS_REGISTRY_BASE names (spec/abi-v2/docs.md, rule r6; the base is
ignored with a warning). A published build that carries no goldens referrer (the
first v2-dev builds ship none) is the other skip, BY NAME and never a pass:
"SKIPPED: no v2-dev goldens published yet", which the workflows read.

LOCAL PROOF WITHOUT A RELEASE (the stub). Two more variables bypass the fetch, so
the runner can be driven against `scripts/abi-v1/build-stubs.sh`'s library:

    CHTYPES_GOLDENS_LIBRARY         a local library (e.g. <stubs>/ok.so), opened unverified
    CHTYPES_GOLDENS_DOCUMENT_IN     a goldens document file to run instead of a fetched one

In that mode `CHTYPES_GOLDENS_PLATFORM`, `_REPORT` and `_DOCUMENT` are optional
(the platform defaults to the host's). `tests/goldens_v1/data/stub-goldens.json`
is a small document the stub can answer; see this directory's data for the
comparator command that judges it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ENV_BASE = "CHTYPES_GOLDENS_REGISTRY_BASE"
ENV_VERSION = "CHTYPES_GOLDENS_VERSION"
ENV_PLATFORM = "CHTYPES_GOLDENS_PLATFORM"
ENV_REPORT = "CHTYPES_GOLDENS_REPORT"
ENV_DOCUMENT = "CHTYPES_GOLDENS_DOCUMENT"
ENV_LIBRARY = "CHTYPES_GOLDENS_LIBRARY"
ENV_DOCUMENT_IN = "CHTYPES_GOLDENS_DOCUMENT_IN"

_OS_ARCH = {
    "linux-amd64": "linux/amd64",
    "linux-arm64": "linux/arm64",
    "darwin-arm64": "darwin/arm64",
}


def _b64d(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ------------------------------------------------------------------ the child


def _open_library(version: str):  # noqa: ANN202 - chtypes.Library
    """The library for this process: the public Registry over the cache the parent
    filled (offline: `resolve_installed`, never the network), or an unverified open
    of a local build when the stub override is set."""
    import chtypes

    local = os.environ.get(ENV_LIBRARY)
    if local:
        os.environ["CHTYPES_ALLOW_UNVERIFIED_LIBRARY"] = "1"
        return chtypes.open_unverified(local, allow=True)
    return chtypes.Registry().for_version(version)


def _case_record(api: Any, case: dict, setup_id: str, platform: str) -> dict:
    """Run one case's call sequence through the generated call layer and record it."""
    from chtypes import _decode
    from chtypes._abi2 import _decls
    from chtypes.errors import CallError

    record: dict[str, Any] = {"id": case["id"], "setup": setup_id}
    if platform not in case["platforms"]:
        record["result"] = "skipped"
        record["skip_reason"] = f"the case's platforms {case['platforms']} exclude {platform}"
        return record
    record["result"] = "ran"

    def failed(at: str, err: CallError) -> dict:
        record["at"] = at
        record["decoded_ok"] = None  # no document to decode
        record["status"] = _decls.STATUS_BY_VALUE.get(err.status, "CHS_INTERNAL")
        record["error"] = {
            "ch_code": err.ch_code,
            "ch_name_b64": _b64e(err.ch_name.encode("ascii")),
            "message_b64": _b64e(err.message),
            "column_b64": _b64e(err.column),
        }
        return record

    def decoded(call: Any) -> bool:
        try:
            call()
        except Exception:  # any failure to decode is the answer, not an error here
            return False
        return True

    schema_in = case["schema"]
    schema = filt = None
    try:
        try:
            schema = api.schema_create(
                _b64d(schema_in["create_table_b64"]), _b64d(schema_in["settings_b64"])
            )
        except CallError as err:
            return failed("schema_create", err)
        call = case["call"]
        body = case.get("body")
        try:
            if call == "schema_create":
                document, export = api.schema_describe(schema), None
                decode = lambda: _decode.decode_schema_description(document)  # noqa: E731
            elif call == "preview_row":
                document, export = (
                    api.preview_row(
                        schema,
                        body["format"],
                        _b64d(body["body_b64"]),
                        _b64d(body["settings_b64"]),
                        _b64d(body["columns_b64"]) if "columns_b64" in body else None,
                    ),
                    None,
                )
                decode = lambda: _decode.decode_row_document(document)  # noqa: E731
            elif call == "preview_batch":
                requested = body["export_format"]
                document, export = api.preview_batch(
                    schema,
                    body["format"],
                    _b64d(body["body_b64"]),
                    _b64d(body["settings_b64"]),
                    _b64d(body["columns_b64"]) if "columns_b64" in body else None,
                    None,
                    requested,
                    body["doc_flags"],
                )
                if requested == -1:
                    export = None
                decode = lambda: _decode.decode_batch(document, export)  # noqa: E731
            elif call == "filter_eval_body":
                f = case["filter"]
                try:
                    filt = api.filter_create(
                        schema,
                        _b64d(f["expr_b64"]),
                        _b64d(f["query_params_b64"]),
                        _b64d(f["settings_b64"]),
                    )
                except CallError as err:
                    return failed("filter_create", err)
                document, export = (
                    api.filter_eval_body(
                        filt,
                        body["format"],
                        _b64d(body["body_b64"]),
                        _b64d(body["settings_b64"]),
                    ),
                    None,
                )
                decode = lambda: _decode.decode_filter_result(document)  # noqa: E731
            else:
                raise ValueError(f"unknown call {call!r}")
        except CallError as err:
            return failed("call", err)
        record["at"] = "call"
        record["status"] = "CHS_OK"
        record["document_b64"] = _b64e(document)
        if export is not None:
            record["export_b64"] = _b64e(export)
        record["decoded_ok"] = decoded(decode)
        return record
    finally:
        for handle in (filt, schema):
            if handle is not None:
                handle.close()


def _child(doc_path: str, setup_id: str, out_path: str, platform: str) -> None:
    import chtypes

    doc = json.loads(Path(doc_path).read_bytes())
    setup = next(s for s in doc["setups"] if s["id"] == setup_id)
    zone = _b64d(setup["image_zone_b64"]).decode("utf-8")
    defaults_raw = _b64d(setup["defaults_b64"])
    # setup() records; the first open commits it (chs_initialize, then
    # chs_set_defaults when there are defaults), once, in this process only.
    chtypes.setup(
        timezone=zone or None,
        defaults=json.loads(defaults_raw) if defaults_raw else None,
    )
    library = _open_library(doc["clickhouse_version"])
    cases = [_case_record(library._api, case, setup_id, platform) for case in setup["cases"]]
    info = library.build_info
    Path(out_path).write_text(
        json.dumps(
            {
                "cases": cases,
                "build_info_b64": _b64e(info.raw),
                "abi_fingerprint": info.abi_fingerprint,
            }
        ),
        encoding="utf-8",
    )


# ----------------------------------------------------------------- the parent


def _fetch(base: str, version: str, platform_key: str, cache: Path) -> tuple[bytes, dict]:
    """Fetch the release, then the goldens document of its platform manifest, in
    process and under the default trust. `fetch_signed` takes the PLATFORM
    manifest's digest and does the referrer selection itself."""
    import pytest

    from chtypes._ocifetch import Request, _channel, ensure, fetch_signed
    from chtypes._ocifetch import _constants as C
    from chtypes._ocifetch._errors import ArtifactUnpublishedError
    from chtypes.registry import FetchOptions

    options = FetchOptions(bases=(base,), cache_dir=cache)._to_options(platform_key)
    resolved = ensure(Request(version), options)
    manifest = resolved.digests["manifest"]
    try:
        fetched = fetch_signed("", manifest, C.PREDICATE_TYPE_GOLDENS, options)
    except ArtifactUnpublishedError as exc:
        if _channel.channel_name() != "v2-dev":
            raise
        # The build is published (ensure above succeeded) and carries no goldens
        # referrer. A skip BY NAME, never a pass: the workflows read this line.
        pytest.skip(
            f"SKIPPED: no v2-dev goldens published yet: {version} {platform_key} ({manifest}) "
            f"has no goldens referrer on {_channel.DEV_CHANNEL_BASE}; the goldens did not run "
            f"({exc})"
        )
    return Path(fetched["path"]).read_bytes(), fetched


def test_goldens_v1_runner(tmp_path: Path) -> None:
    import pytest

    import chtypes

    local_library = os.environ.get(ENV_LIBRARY)
    local_document = os.environ.get(ENV_DOCUMENT_IN)
    if bool(local_library) != bool(local_document):
        pytest.fail(f"{ENV_LIBRARY} and {ENV_DOCUMENT_IN} are set together or not at all")
    base = os.environ.get(ENV_BASE)
    version = os.environ.get(ENV_VERSION)
    platform_key = os.environ.get(ENV_PLATFORM)
    stub = bool(local_library)
    if not stub and not (base and version and platform_key):
        pytest.skip(
            f"goldens runner not run: set {ENV_BASE}, {ENV_VERSION} and {ENV_PLATFORM} "
            f"(or {ENV_LIBRARY} with {ENV_DOCUMENT_IN} for the stub)"
        )

    from chtypes._ocifetch._ensure import detect_host_platform

    host = detect_host_platform()
    platform_key = platform_key or host
    assert platform_key in _OS_ARCH, f"{ENV_PLATFORM}={platform_key!r} is not a known platform"
    assert platform_key == host, f"{ENV_PLATFORM}={platform_key} but this host is {host}"

    cache = tmp_path / "cache"
    child_env = dict(os.environ)
    if stub:
        assert local_document is not None
        document_bytes = Path(local_document).read_bytes()
    else:
        assert base and version
        document_bytes, _ = _fetch(base, version, platform_key, cache)
        child_env.update(CHTYPES_ARTIFACTS_URL=base, CHTYPES_CACHE=str(cache))
    document_path = Path(os.environ.get(ENV_DOCUMENT) or tmp_path / "goldens-document.json")
    document_path.write_bytes(document_bytes)  # the EXACT bytes, never re-serialized
    doc = json.loads(document_bytes)
    version = version or doc["clickhouse_version"]
    child_env[ENV_VERSION] = version

    cases: list[dict] = []
    artifact: dict[str, str] | None = None
    for setup in doc["setups"]:
        out = tmp_path / f"setup-{setup['id']}.json"
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                str(document_path),
                setup["id"],
                str(out),
                _OS_ARCH[platform_key],
            ],
            env=child_env,
            check=True,
            timeout=1800,
        )
        got = json.loads(out.read_text(encoding="utf-8"))
        seen = {"build_info_b64": got["build_info_b64"], "abi_fingerprint": got["abi_fingerprint"]}
        assert artifact in (None, seen), "two setups loaded different libraries"
        artifact = seen
        cases.extend(got["cases"])

    expected = [c["id"] for s in doc["setups"] for c in s["cases"]]
    assert sorted(c["id"] for c in cases) == sorted(expected), "a case is missing from the report"
    assert artifact is not None
    report = {
        "report_version": 1,
        "binding": "python",
        "binding_version": chtypes.__version__,
        "platform": _OS_ARCH[platform_key],
        "goldens": {
            "clickhouse_version": doc["clickhouse_version"],
            "build": doc["build"],
            "revision": doc["revision"],
            "sha256": hashlib.sha256(document_bytes).hexdigest(),
        },
        "artifact": artifact,
        "cases": cases,
    }
    report_path = Path(os.environ.get(ENV_REPORT) or tmp_path / "goldens-report.json")
    report_path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    ran = [c for c in cases if c["result"] == "ran"]
    assert ran, "the runner ran zero cases"
    print(f"goldens runner: {len(ran)} ran, {len(cases) - len(ran)} skipped; report {report_path}")


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--child":
        _child(*sys.argv[2:])
    else:
        sys.exit("usage: test_runner.py --child <document> <setup id> <out> <os/arch>")
