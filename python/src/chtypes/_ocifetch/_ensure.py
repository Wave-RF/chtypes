"""The seam (docs/guides/fetch-v1.md "The seam", PLAN §1.3): `ensure`,
`resolve_installed`, `list_installed`, `verify_installed`, `fetch_signed`.

Nothing outside `chtypes._ocifetch` calls anything but these five names (plus
the `Request`/`Options`/`Resolved` shapes below). Everything else in this
package is free to change until the v1 switch.

This module never dlopens, never interprets `abi_fingerprint`, never checks
glibc and never reads `chs_*` symbols — those are the FFI lane's job, built
against this seam once the ABI v1 design is approved.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform_module
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import (
    TrustedKey,
    release_trusted_keys,
    verify_bundle,
)
from chtypes._ocifetch._errors import (
    ArtifactCorruptError,
    ArtifactMissingError,
    ArtifactPinnedError,
    ArtifactUntrustedError,
    FetchError,
)
from chtypes._ocifetch._http import Clock, FetchPolicy, RetryPolicy, TransportError
from chtypes._ocifetch._layout import (
    VerifiedRecord,
    list_verified_records,
    read_verified_record,
    resolve_cache_root,
    search_roots,
    unpacked_dir_for,
    update_index_json,
    write_verified_install,
)
from chtypes._ocifetch._lock import Lock, LockPin, load_lock, new_lock, save_lock
from chtypes._ocifetch._oci import (
    Descriptor,
    fetch_blob_bytes,
    fetch_blob_to_path,
    fetch_manifest_by_digest,
    fetch_manifest_by_tag,
    manifest_config_descriptor,
    manifest_layer_descriptor,
    manifest_single_layer,
    parse_digest,
    resolve_platform_manifest,
    spelling_components,
    validate_spelling,
    version_within_request,
)
from chtypes._ocifetch._referrers import discover_referrers
from chtypes._ocifetch._unpack import unpack_tar_zst

__all__ = [
    "Options",
    "Request",
    "Resolved",
    "VerifyResult",
    "ensure",
    "fetch_signed",
    "list_installed",
    "resolve_installed",
    "verify_installed",
]


@dataclass(frozen=True)
class Request:
    """A floating or exact version spelling: ``"26.8"``, ``"26.8.15"`` or
    ``"26.8.15.10"`` (constants: ``spelling.regex``)."""

    spelling: str

    def __post_init__(self) -> None:
        validate_spelling(self.spelling)


@dataclass
class Options:
    """Everything `ensure`/`resolve_installed`/etc. need beyond the request.

    ``platform`` is the TARGET platform key (``"linux-arm64"`` etc.);
    `ensure` defaults to the running host's when unset, which is the normal
    end-user case. `resolve_installed` takes platform as its own explicit
    argument instead, since cache introspection (and `--lock`, which checks
    every DECLARED platform, almost always not the host's own) is not
    limited to "the machine this call happens to run on".
    """

    platform: str | None = None
    bases: tuple[str, ...] = ()
    cache_dir: str | os.PathLike[str] | None = None
    token: str | None = None
    trusted_keys: tuple[TrustedKey, ...] | None = None
    allow_unsigned: bool = False
    offline: bool = False
    frozen: bool = False
    lock_path: str | os.PathLike[str] | None = None
    lock_write: bool = False
    update: bool = False
    clock: Clock = field(default_factory=Clock)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    # Test-only: installed before the cache's index.json atomic rename, to
    # let the conformance runner inject a competing writer deterministically
    # (PLAN §3.2 "index-race-reapply"). Never set outside tests.
    before_index_rename: Callable[[], None] | None = None

    def resolved_bases(self) -> tuple[str, ...]:
        if self.bases:
            return self.bases
        env = os.environ.get(C.ENV_BASES_NAME)
        if env:
            bases = tuple(b.strip() for b in env.split(C.BASE_SEPARATOR) if b.strip())
            if bases:
                return bases
        return C.DEFAULT_BASES

    def resolved_trusted_keys(self) -> tuple[TrustedKey, ...]:
        if self.trusted_keys is not None:
            return self.trusted_keys
        return release_trusted_keys()

    def resolved_platform(self) -> str:
        return self.platform or detect_host_platform()


@dataclass(frozen=True)
class Resolved:
    """What a successful fetch or cache hit returns (the seam's one output shape)."""

    abi_generation: int
    platform: str
    request: str
    version: str
    channel: str | None
    build: str
    library_path: Path
    dir: Path
    digests: dict[str, str | None]
    predicate: dict
    signed_by: str
    source: str
    already_installed: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyResult:
    dir: Path
    platform: str
    version: str
    build: str
    ok: bool
    detail: str


_HOST_PLATFORM_MAP = {
    ("linux", "x86_64"): "linux-amd64",
    ("linux", "amd64"): "linux-amd64",
    ("linux", "aarch64"): "linux-arm64",
    ("linux", "arm64"): "linux-arm64",
    ("darwin", "arm64"): "darwin-arm64",
}


def detect_host_platform() -> str:
    system = _platform_module.system().lower()
    machine = _platform_module.machine().lower()
    key = _HOST_PLATFORM_MAP.get((system, machine))
    if key is None:
        raise ValueError(
            f"chtypes: no v1 artifact is built for this host ({system}-{machine}). "
            f"Known platforms: {', '.join(p['key'] for p in C.PLATFORMS)}."
        )
    return key


def _platform_spec(platform_key: str) -> dict:
    spec = next((p for p in C.PLATFORMS if p["key"] == platform_key), None)
    if spec is None:
        raise ValueError(f"chtypes: unknown platform {platform_key!r}")
    return spec


def _source_for_dir(dir_path: Path, roots: Sequence[Path]) -> str:
    cache_root_path = roots[0]
    try:
        dir_path.relative_to(cache_root_path)
        return "cache"
    except ValueError:
        pass
    for sys_dir in roots[1:]:
        try:
            dir_path.relative_to(sys_dir)
            return f"system:{sys_dir}"
        except ValueError:
            continue
    return "cache"


def _record_to_resolved(
    dest_dir: Path,
    record: VerifiedRecord,
    *,
    request_spelling: str,
    already_installed: bool,
    source: str,
) -> Resolved:
    return Resolved(
        abi_generation=C.ABI_GENERATION,
        platform=record.platform,
        request=request_spelling,
        version=record.version,
        channel=record.channel,
        build=record.build,
        library_path=dest_dir / record.library,
        dir=dest_dir,
        digests={
            "index": record.index,
            "manifest": record.manifest,
            "layer": record.layer,
            "bundle": record.bundle or None,
        },
        predicate=record.predicate,
        signed_by=record.signed_by,
        source=source,
        already_installed=already_installed,
        warnings=tuple(record.warnings),
    )


def _check_predicate_matches_request(predicate: dict, platform_key: str, spelling: str) -> None:
    spec = _platform_spec(platform_key)
    if predicate.get("abi") != C.ABI_GENERATION:
        raise ArtifactCorruptError(
            f"signed predicate abi={predicate.get('abi')!r}, want {C.ABI_GENERATION}"
        )
    if predicate.get("os") != spec["os"] or predicate.get("arch") != spec["architecture"]:
        raise ArtifactCorruptError(
            f"signed predicate platform {predicate.get('os')}-{predicate.get('arch')} "
            f"disagreed with requested {platform_key!r}"
        )
    version = predicate.get("clickhouse_version")
    if not isinstance(version, str) or not version_within_request(version, spelling):
        raise ArtifactCorruptError(
            f"signed predicate version {version!r} does not lie within requested {spelling!r}"
        )
    for name in ("build", "library", "library_sha256"):
        if not isinstance(predicate.get(name), str):
            raise ArtifactCorruptError(f"signed predicate missing string field {name!r}")
    if not isinstance(predicate.get("library_bytes"), int):
        raise ArtifactCorruptError("signed predicate missing integer field 'library_bytes'")


def _compare_version_build(v1: str, b1: str, v2: str, b2: str) -> int:
    c1, c2 = spelling_components(v1), spelling_components(v2)
    if c1 != c2:
        return -1 if c1 < c2 else 1
    if b1 == b2:
        return 0
    return -1 if b1 < b2 else 1  # build is a fixed-width UTC string: lexicographic == chronological


def _monotonic_warnings(roots: Sequence[Path], platform_key: str, predicate: dict) -> list[str]:
    new_version = predicate.get("clickhouse_version")
    new_build = predicate.get("build")
    out = []
    for _dir, record in list_verified_records(roots):
        if record.platform != platform_key:
            continue
        if _compare_version_build(record.version, record.build, new_version, new_build) > 0:
            out.append(
                f"an already-installed build ({record.version}/{record.build}) is newer "
                f"than the one just resolved ({new_version}/{new_build})"
            )
            break
    return out


def _tmp_dir_under(root: Path, subdir: str) -> str:
    """A temp directory on the SAME filesystem as ``root`` / ``subdir``, so
    the final install is a same-filesystem ``os.replace`` (never a copy)."""
    parent = root / subdir
    parent.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(dir=str(parent), prefix=".tmp-")


def _find_verified_signature(
    bases: Sequence[str],
    manifest_digest: str,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
    trusted_keys: tuple[TrustedKey, ...],
    scratch_root: Path,
):
    """Try every referrer bundle of ``manifest_digest`` until one verifies.

    Tolerates a malformed or wrongly-typed referrer and moves to the next —
    PLAN §3.2's "multiple-referrers-one-valid" and "hint-names-unknown-key-
    but-valid" cases both depend on one bad or irrelevant referrer never
    stopping the search for a good one. Returns `None`, never raises, when
    nothing verifies; the caller decides whether that is UNTRUSTED or an
    allow-unsigned fallback.
    """
    referrers = discover_referrers(
        bases, manifest_digest, C.MEDIA_TYPE_BUNDLE, policy=policy, retry=retry
    )
    for ref in referrers:
        try:
            referrer_doc, _raw = fetch_manifest_by_digest(
                bases, ref.digest, policy=policy, retry=retry
            )
            blob_desc = manifest_single_layer(
                referrer_doc, expected_media_type=C.MEDIA_TYPE_BUNDLE
            )
        except (TransportError, FetchError):
            continue
        tmp_dir = tempfile.mkdtemp(dir=str(scratch_root))
        try:
            bundle_path = os.path.join(tmp_dir, "bundle.json")
            fetch_blob_to_path(
                bases, blob_desc, bundle_path, policy=policy, retry=retry,
                max_bytes=C.BUNDLE_MAX_BYTES,
            )
            with open(bundle_path, "rb") as f:
                bundle_json = json.loads(f.read())
            verified = verify_bundle(bundle_json, trusted_keys)
        except (TransportError, FetchError, json.JSONDecodeError):
            continue
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if verified is not None:
            return verified, blob_desc.digest, ref.digest
    return None


def _scratch_root(cache_root_path: Path) -> Path:
    p = cache_root_path / "tmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _unpack_and_install(
    *,
    cache_root_path: Path,
    scratch_root: Path,
    bases: Sequence[str],
    layer_desc: Descriptor,
    manifest_digest: str,
    layer_digest: str,
    bundle_digest: str,
    index_digest: str | None,
    platform_key: str,
    predicate: dict,
    signed_by: str,
    policy: FetchPolicy,
    retry: RetryPolicy,
    warnings: list[str],
) -> Path:
    tmp_dir = tempfile.mkdtemp(dir=str(scratch_root))
    try:
        layer_path = os.path.join(tmp_dir, "layer")
        fetch_blob_to_path(
            bases, layer_desc, layer_path, policy=policy, retry=retry,
            max_bytes=C.MAX_UNPACKED_BYTES,
        )
        unpack_dest = os.path.join(tmp_dir, "unpacked")
        unpack_tar_zst(
            layer_path,
            unpack_dest,
            library_name=predicate["library"],
            library_sha256=predicate["library_sha256"],
            library_bytes=predicate["library_bytes"],
        )
        record = VerifiedRecord(
            schema=1,
            manifest=manifest_digest,
            layer=layer_digest,
            bundle=bundle_digest,
            index=index_digest,
            platform=platform_key,
            version=predicate["clickhouse_version"],
            build=predicate["build"],
            channel=predicate.get("channel"),
            predicate=predicate,
            signed_by=signed_by,
            library=predicate["library"],
            warnings=tuple(warnings),
        )
        return write_verified_install(
            cache_root_path, manifest_digest, record, unpacked_tmp_dir=unpack_dest
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _write_lock_pin(
    options: Options, lock: Lock | None, spelling: str, platform_key: str, resolved: Resolved
) -> None:
    if options.lock_path is None:
        raise ValueError("chtypes: lock_write requires options.lock_path")
    base = lock if lock is not None else new_lock()
    pin = LockPin(
        version=resolved.version,
        build=resolved.build,
        manifest=resolved.digests["manifest"],
        layer=resolved.digests["layer"],
        bundle=resolved.digests["bundle"] or "",
        index=resolved.digests.get("index"),
    )
    save_lock(options.lock_path, base.with_pin(spelling, platform_key, pin))


def _ensure_floating(
    request: Request, platform_key: str, options: Options, lock: Lock | None
) -> Resolved:
    bases = options.resolved_bases()
    policy = FetchPolicy(token=options.token, clock=options.clock)
    retry = options.retry
    roots = search_roots(options.cache_dir)
    cache_root_path = roots[0]
    scratch_root = _scratch_root(cache_root_path)

    index_doc, index_bytes, index_digest = fetch_manifest_by_tag(
        bases, request.spelling, policy=policy, retry=retry
    )
    platform_desc = resolve_platform_manifest(index_doc, platform_key)
    manifest_hex = parse_digest(platform_desc.digest)

    existing_dir = unpacked_dir_for(cache_root_path, manifest_hex)
    existing_record = read_verified_record(existing_dir)
    if existing_record is not None and existing_record.platform == platform_key:
        resolved = _record_to_resolved(
            existing_dir, existing_record, request_spelling=request.spelling,
            already_installed=True, source="cache",
        )
        if options.lock_write:
            _write_lock_pin(options, lock, request.spelling, platform_key, resolved)
        return resolved

    manifest_doc, _manifest_bytes = fetch_manifest_by_digest(
        bases, platform_desc.digest, policy=policy, retry=retry
    )
    layer_desc = manifest_layer_descriptor(manifest_doc)

    trusted_keys = options.resolved_trusted_keys()
    found = _find_verified_signature(
        bases, platform_desc.digest, policy=policy, retry=retry, trusted_keys=trusted_keys,
        scratch_root=scratch_root,
    )

    warnings: list[str] = []
    if found is None:
        if not options.allow_unsigned:
            raise ArtifactUntrustedError(
                f"chtypes: no trusted signature for {request.spelling} ({platform_key})"
            )
        warnings.append(
            "CHTYPES_ALLOW_UNSIGNED: no trusted signature found; proceeding unsigned"
        )
        config_desc = manifest_config_descriptor(manifest_doc)
        if config_desc is None:
            raise ArtifactCorruptError(
                "no trusted signature and no config blob to identify the library"
            )
        config_bytes = fetch_blob_bytes(
            bases, config_desc.digest, policy=policy, retry=retry, max_bytes=C.MANIFEST_MAX_BYTES
        )
        predicate = json.loads(config_bytes)
        signed_by = ""
        bundle_digest = ""
    else:
        verified, bundle_digest, _referrer_digest = found
        statement = verified.statement
        if statement.predicate_type != C.PREDICATE_TYPE_ARTIFACT:
            raise ArtifactCorruptError(
                f"signed predicateType was {statement.predicate_type!r}, want "
                f"{C.PREDICATE_TYPE_ARTIFACT!r}"
            )
        layer_hex = parse_digest(layer_desc.digest)
        if layer_hex not in statement.subject_sha256:
            raise ArtifactCorruptError("signed subject digest did not match the manifest's layer")
        predicate = statement.predicate
        signed_by = verified.signed_by

    _check_predicate_matches_request(predicate, platform_key, request.spelling)
    warnings.extend(_monotonic_warnings(roots, platform_key, predicate))

    dest_dir = _unpack_and_install(
        cache_root_path=cache_root_path, scratch_root=scratch_root, bases=bases,
        layer_desc=layer_desc, manifest_digest=platform_desc.digest, layer_digest=layer_desc.digest,
        bundle_digest=bundle_digest, index_digest=index_digest, platform_key=platform_key,
        predicate=predicate, signed_by=signed_by, policy=policy, retry=retry, warnings=warnings,
    )
    record = read_verified_record(dest_dir)
    assert record is not None

    def _add_entry(doc: dict) -> dict:
        return {**doc, "manifests": [*doc.get("manifests", []), {"digest": platform_desc.digest}]}

    try:
        update_index_json(cache_root_path, _add_entry, before_rename=options.before_index_rename)
    except OSError:
        pass  # index.json is an oras-interop convenience; correctness never depends on it.

    resolved = _record_to_resolved(
        dest_dir, record, request_spelling=request.spelling, already_installed=False,
        source=bases[0] if bases else "cache",
    )
    if options.lock_write:
        _write_lock_pin(options, lock, request.spelling, platform_key, resolved)
    return resolved


def _ensure_frozen(
    request: Request, platform_key: str, options: Options, lock: Lock | None
) -> Resolved:
    if lock is None:
        raise ArtifactPinnedError("chtypes: --frozen requires a lock file; none was given")
    pin = lock.pin_for(request.spelling, platform_key)
    if pin is None:
        raise ArtifactPinnedError(
            f"chtypes: --frozen: no pin for {request.spelling!r} ({platform_key}) in the lock"
        )
    bases = options.resolved_bases()
    policy = FetchPolicy(token=options.token, clock=options.clock)
    retry = options.retry
    roots = search_roots(options.cache_dir)
    cache_root_path = roots[0]
    scratch_root = _scratch_root(cache_root_path)

    manifest_hex = parse_digest(pin.manifest)
    existing_dir = unpacked_dir_for(cache_root_path, manifest_hex)
    existing_record = read_verified_record(existing_dir)
    if existing_record is not None and existing_record.platform == platform_key:
        return _record_to_resolved(
            existing_dir, existing_record, request_spelling=request.spelling,
            already_installed=True, source="cache",
        )

    manifest_doc, _raw = fetch_manifest_by_digest(bases, pin.manifest, policy=policy, retry=retry)
    layer_desc = manifest_layer_descriptor(manifest_doc)
    if layer_desc.digest != pin.layer:
        raise ArtifactPinnedError(
            f"chtypes: --frozen: the manifest's layer {layer_desc.digest} disagreed with "
            f"the lock's {pin.layer}"
        )

    bundle_bytes = fetch_blob_bytes(
        bases, pin.bundle, policy=policy, retry=retry, max_bytes=C.BUNDLE_MAX_BYTES
    )
    trusted_keys = options.resolved_trusted_keys()
    verified = verify_bundle(json.loads(bundle_bytes), trusted_keys)
    if verified is None:
        raise ArtifactUntrustedError(
            "chtypes: --frozen: the pinned bundle did not verify under a trusted key"
        )
    predicate = verified.statement.predicate
    _check_predicate_matches_request(predicate, platform_key, request.spelling)
    if predicate.get("clickhouse_version") != pin.version or predicate.get("build") != pin.build:
        raise ArtifactPinnedError(
            "chtypes: --frozen: the signed predicate's version/build disagreed with the lock"
        )
    layer_hex = parse_digest(layer_desc.digest)
    if layer_hex not in verified.statement.subject_sha256:
        raise ArtifactCorruptError(
            "chtypes: --frozen: signed subject digest did not match the pinned layer"
        )

    dest_dir = _unpack_and_install(
        cache_root_path=cache_root_path, scratch_root=scratch_root, bases=bases,
        layer_desc=layer_desc, manifest_digest=pin.manifest, layer_digest=pin.layer,
        bundle_digest=pin.bundle, index_digest=pin.index, platform_key=platform_key,
        predicate=predicate, signed_by=verified.signed_by, policy=policy, retry=retry, warnings=[],
    )
    record = read_verified_record(dest_dir)
    assert record is not None
    return _record_to_resolved(
        dest_dir, record, request_spelling=request.spelling, already_installed=False,
        source=bases[0] if bases else "cache",
    )


def ensure(request: Request, options: Options) -> Resolved:
    """Make sure a verified, unpacked library for ``request`` is on disk,
    fetching it if needed (docs/guides/fetch-v1.md "The seam").

    Lock precedence (`decided-here`; constants/PLAN §0's lock row states the
    two ends — "`--frozen` makes no lookups; `update` re-resolves" — but not
    the default in between, which the real conformance fixtures, not yet
    available in this worktree, will pin down): when a lock file exists AND
    pins this exact (request, platform) AND ``options.update`` is not set,
    a plain `ensure()` follows the pin by digest (the same path `--frozen`
    uses), rather than re-resolving the floating tag. ``--frozen`` differs
    from this default only in refusing outright when no pin exists, instead
    of falling back to a floating resolve. ``update=True`` always ignores
    any existing pin and resolves the floating tag fresh.
    """
    platform_key = options.resolved_platform()
    lock: Lock | None = None
    if options.lock_path is not None and os.path.exists(options.lock_path):
        lock = load_lock(options.lock_path)

    if options.offline:
        resolved = resolve_installed(request, platform_key, options)
        if resolved is None:
            raise ArtifactMissingError(
                f"chtypes: no installed artifact for {request.spelling} ({platform_key}); offline"
            )
        return resolved

    if options.frozen:
        return _ensure_frozen(request, platform_key, options, lock)

    if lock is not None and not options.update and lock.pin_for(request.spelling, platform_key):
        return _ensure_frozen(request, platform_key, options, lock)

    return _ensure_floating(request, platform_key, options, lock)


def resolve_installed(request: Request, platform: str, options: Options) -> Resolved | None:
    """Cache-only: never touches the network (docs/guides/fetch-v1.md "The seam").

    The source of truth is the immutable `unpacked/sha256/*/verified.json`
    records (PLAN §3.4), never `index.json` — a lost `index.json` race costs
    only `oras` interop, never correctness here.
    """
    roots = search_roots(options.cache_dir)
    candidates = [
        (dir_path, record)
        for dir_path, record in list_verified_records(roots)
        if record.platform == platform and version_within_request(record.version, request.spelling)
    ]
    if not candidates:
        return None
    dir_path, record = max(
        candidates, key=lambda item: (spelling_components(item[1].version), item[1].build)
    )
    return _record_to_resolved(
        dir_path, record, request_spelling=request.spelling, already_installed=True,
        source=_source_for_dir(dir_path, roots),
    )


def list_installed(options: Options) -> list[Resolved]:
    """Every verified install across the cache and the read-only system
    directories, cache-only (docs/guides/fetch-v1.md "The seam")."""
    roots = search_roots(options.cache_dir)
    return [
        _record_to_resolved(
            dir_path, record, request_spelling=record.version, already_installed=True,
            source=_source_for_dir(dir_path, roots),
        )
        for dir_path, record in list_verified_records(roots)
    ]


def verify_installed(options: Options) -> list[VerifyResult]:
    """Re-hash every installed library's on-disk bytes against its own
    `verified.json` record, cache-only."""
    roots = search_roots(options.cache_dir)
    out = []
    for dir_path, record in list_verified_records(roots):
        lib_path = dir_path / record.library
        try:
            data = lib_path.read_bytes()
        except OSError as e:
            out.append(
                VerifyResult(dir_path, record.platform, record.version, record.build, False, str(e))
            )
            continue
        expected_sha = record.predicate.get("library_sha256")
        expected_bytes = record.predicate.get("library_bytes")
        actual_sha = hashlib.sha256(data).hexdigest()
        if len(data) != expected_bytes or actual_sha != expected_sha:
            out.append(
                VerifyResult(
                    dir_path, record.platform, record.version, record.build, False,
                    "on-disk library no longer matches its verified record",
                )
            )
        else:
            out.append(
                VerifyResult(dir_path, record.platform, record.version, record.build, True, "")
            )
    return out


def fetch_signed(repository: str, ref: str, predicate_type: str, options: Options) -> dict:
    """A generic signed-artifact fetch: goldens (``ref`` is the referrer
    digest discovered against a platform manifest) and fetch fixtures
    (``ref`` is the pinned digest; §7.7 — no tag fallback is ever tried for
    fixtures). ``repository`` is a path suffix joined onto every configured
    base (``""`` for the same repository as the platform artifact,
    ``constants.FIXTURES_REPO_SUFFIX`` for fixtures).

    Returns ``{"path": <verified layer bytes on disk>, "statement": <predicate
    dict, or None under allow-unsigned>, "digests": {"manifest", "layer",
    "bundle"}}``. Never dlopens or interprets the content — that is for the
    caller.
    """
    bases = tuple(b.rstrip("/") + repository for b in options.resolved_bases())
    policy = FetchPolicy(token=options.token, clock=options.clock)
    retry = options.retry
    cache_root_path = resolve_cache_root(options.cache_dir)
    scratch_root = _scratch_root(cache_root_path)

    if ref.startswith("sha256:"):
        doc, _raw = fetch_manifest_by_digest(bases, ref, policy=policy, retry=retry)
        manifest_digest = ref
    else:
        doc, _raw, manifest_digest = fetch_manifest_by_tag(bases, ref, policy=policy, retry=retry)

    layer_desc = manifest_single_layer(doc)
    trusted_keys = options.resolved_trusted_keys()
    found = _find_verified_signature(
        bases, manifest_digest, policy=policy, retry=retry, trusted_keys=trusted_keys,
        scratch_root=scratch_root,
    )
    bundle_digest: str | None = None
    statement_predicate: dict | None = None
    if found is None:
        if not options.allow_unsigned:
            raise ArtifactUntrustedError(f"chtypes: no trusted signature for {repository}@{ref}")
    else:
        verified, bundle_digest, _referrer_digest = found
        if verified.statement.predicate_type != predicate_type:
            raise ArtifactCorruptError(
                f"signed predicateType was {verified.statement.predicate_type!r}, want "
                f"{predicate_type!r}"
            )
        layer_hex = parse_digest(layer_desc.digest)
        if layer_hex not in verified.statement.subject_sha256:
            raise ArtifactCorruptError("signed subject digest did not match the fetched layer")
        statement_predicate = verified.statement.predicate

    dest_dir = cache_root_path / "fetched-signed"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = str(dest_dir / parse_digest(layer_desc.digest))
    fetch_blob_to_path(
        bases, layer_desc, dest_path, policy=policy, retry=retry, max_bytes=C.MAX_UNPACKED_BYTES
    )
    return {
        "path": dest_path,
        "statement": statement_predicate,
        "digests": {
            "manifest": manifest_digest, "layer": layer_desc.digest, "bundle": bundle_digest,
        },
    }
