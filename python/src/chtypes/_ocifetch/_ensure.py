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
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import (
    TrustedKey,
    env_trusted_keys,
    release_trusted_keys,
    verify_bundle,
)
from chtypes._ocifetch._errors import (
    ArtifactCorruptError,
    ArtifactMissingError,
    ArtifactPinnedError,
    ArtifactUnpublishedError,
    ArtifactUntrustedError,
    FetchError,
    SourceForbiddenError,
    SourceUnauthorizedError,
    SourceUnreachableError,
)
from chtypes._ocifetch._goldens import GoldensCandidate, select_goldens, statement_revision
from chtypes._ocifetch._http import (
    Clock,
    FetchPolicy,
    ForbiddenHttpError,
    NotFoundHttpError,
    OversizeHttpError,
    RetryPolicy,
    TransportError,
    UnauthorizedHttpError,
    UnreachableHttpError,
)
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
    """A floating or exact version spelling: `26.8`, `26.8.15` or
    `26.8.15.10` (constants: `spelling.regex`)."""

    spelling: str

    def __post_init__(self) -> None:
        validate_spelling(self.spelling)


@dataclass
class Options:
    """Everything `ensure`/`resolve_installed`/etc. need beyond the request.

    `platform` is the TARGET platform key (`"linux-arm64"` etc.);
    `ensure` defaults to the running host's when unset, which is the normal
    end-user case. `resolve_installed` takes platform as its own explicit
    argument instead, since cache introspection (and `--lock`, which checks
    every DECLARED platform, almost always not the host's own) is not
    limited to "the machine this call happens to run on".
    """

    platform: str | None = None
    bases: tuple[str, ...] = ()
    cache_dir: str | os.PathLike[str] | None = None
    # Read-only directories searched after the cache. None keeps the default
    # list (constants: cache.system_dirs); an empty tuple searches none.
    system_dirs: tuple[str | os.PathLike[str], ...] | None = None
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
        return env_trusted_keys() or release_trusted_keys()

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
    warnings: Sequence[str] = (),
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
            "bundle": record.bundle,
            "bundle_manifest": record.bundle_manifest,
        },
        predicate=record.predicate,
        signed_by=record.signed_by or "",
        source=source,
        already_installed=already_installed,
        warnings=tuple(warnings),
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


def _version_key(version: object) -> tuple[int, ...] | None:
    """A four-part version as integers, or None for anything else."""
    if not isinstance(version, str):
        return None
    try:
        return spelling_components(version)
    except ValueError:
        return None


def _newer_installed(
    roots: Sequence[Path], platform_key: str, predicate: dict, request_spelling: str
) -> tuple[Path, VerifiedRecord, str] | None:
    """PLAN "monotonic-warning": "an installed HIGHER build; the registry
    offers a LOWER one; ok plus a warning." The existing (newer) install is
    KEPT, not replaced by the one the registry just offered, so this returns
    the winning (dir, record) alongside the warning text rather than a
    warning alone; the caller resolves to it instead of installing what it
    just verified. It is the newest such install.

    Scoped to the REQUEST (docs/guides/fetch-v1.md §9; public issue #481),
    the rule Go, TypeScript and Rust follow too: only an install whose
    version lies within the request counts, and it counts when its (version,
    build) is newer. So a line request (`26.3`) keeps a newer build of that
    line and never sees another line's install, and an exact request
    (`26.3.4.1`) keeps only a newer BUILD of that exact version. A request
    that is not a version spelling (an arbitrary tag) names no range, so
    nothing is kept for it.
    """
    if not re.match(C.SPELLING_REGEX, request_spelling):
        return None
    new_version = predicate.get("clickhouse_version")
    new_build = predicate.get("build")
    new_key = _version_key(new_version)
    if new_key is None or not isinstance(new_build, str):
        return None
    best: tuple[Path, VerifiedRecord] | None = None
    best_key: tuple[tuple[int, ...], str] | None = None
    for dir_, record in list_verified_records(roots):
        if record.platform != platform_key:
            continue
        if not version_within_request(record.version, request_spelling):
            continue
        version_key = _version_key(record.version)
        if version_key is None:
            continue
        # build is a fixed-width UTC string: lexicographic == chronological.
        key = (version_key, record.build)
        if key <= (new_key, new_build):
            continue
        if best_key is None or key > best_key:
            best, best_key = (dir_, record), key
    if best is None:
        return None
    dir_, record = best
    warning = (
        f"monotonic warning: an already-installed build ({record.version}/"
        f"{record.build}) within {request_spelling} is newer than the one just "
        f"resolved ({new_version}/{new_build})"
    )
    return dir_, record, warning


def _tmp_dir_under(root: Path, subdir: str) -> str:
    """A temp directory on the SAME filesystem as `root` / `subdir`, so
    the final install is a same-filesystem `os.replace` (never a copy)."""
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
    """Try every referrer bundle of `manifest_digest` until one verifies.

    Tolerates a malformed or wrongly-typed referrer and moves to the next —
    PLAN §3.2's "multiple-referrers-one-valid" and "hint-names-unknown-key-
    but-valid" cases both depend on one bad or irrelevant referrer never
    stopping the search for a good one. Returns `None`, never raises, when
    nothing verifies and every failure was merely "didn't verify" or a
    transport hiccup; the caller then decides whether that is UNTRUSTED or
    an allow-unsigned fallback.

    A referrer whose bundle is itself STRUCTURALLY broken (bad base64, no
    envelope, a duplicate JSON key — `_dsse.verify_bundle`'s own
    `ArtifactCorruptError`) is not "try the next one" territory per that
    function's docstring: it is the same bundle either way, so that error
    propagates immediately rather than being swallowed into a misleading
    UNTRUSTED verdict (`statement-duplicate-key` expects
    `CHTYPES_ARTIFACT_CORRUPT`, not `CHTYPES_ARTIFACT_UNTRUSTED`).
    """
    referrers = discover_referrers(
        bases, manifest_digest, C.MEDIA_TYPE_BUNDLE, policy=policy, retry=retry
    )
    for ref in referrers:
        try:
            referrer_doc, _raw = fetch_manifest_by_digest(
                bases, ref.digest, policy=policy, retry=retry
            )
            blob_desc = manifest_single_layer(referrer_doc, expected_media_type=C.MEDIA_TYPE_BUNDLE)
        except ArtifactCorruptError:
            raise
        except (TransportError, FetchError):
            continue
        tmp_dir = tempfile.mkdtemp(dir=str(scratch_root))
        try:
            bundle_path = os.path.join(tmp_dir, "bundle.json")
            fetch_blob_to_path(
                bases,
                blob_desc,
                bundle_path,
                policy=policy,
                retry=retry,
                max_bytes=C.BUNDLE_MAX_BYTES,
            )
            with open(bundle_path, "rb") as f:
                bundle_json = json.loads(f.read())
            verified = verify_bundle(bundle_json, trusted_keys)
        except ArtifactCorruptError:
            raise
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
    bundle_digest: str | None,
    bundle_manifest_digest: str | None,
    index_digest: str | None,
    platform_key: str,
    predicate: dict,
    signed_by: str | None,
    policy: FetchPolicy,
    retry: RetryPolicy,
) -> Path:
    tmp_dir = tempfile.mkdtemp(dir=str(scratch_root))
    try:
        layer_path = os.path.join(tmp_dir, "layer")
        fetch_blob_to_path(
            bases,
            layer_desc,
            layer_path,
            policy=policy,
            retry=retry,
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
            manifest=manifest_digest,
            layer=layer_digest,
            bundle=bundle_digest,
            bundle_manifest=bundle_manifest_digest,
            index=index_digest,
            platform=platform_key,
            version=predicate["clickhouse_version"],
            build=predicate["build"],
            channel=predicate.get("channel"),
            predicate=predicate,
            signed_by=signed_by,
            library=predicate["library"],
            library_sha256=predicate["library_sha256"],
            library_bytes=predicate["library_bytes"],
        )
        return write_verified_install(
            cache_root_path, manifest_digest, record, unpacked_tmp_dir=unpack_dest
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _write_lock_pin(
    options: Options,
    lock: Lock | None,
    spelling: str,
    platform_key: str,
    resolved: Resolved,
    *,
    extra_pins: dict[str, LockPin] | None = None,
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
        # docs/guides/fetch-v1.md §6: "the index digest is informational
        # only" — never used for `--frozen` resolution, so the lock never
        # records it at all (the fixtures' expected lock_after documents
        # omit the field entirely, not just an unread one).
        index=None,
    )
    # `lock-write-all-platforms`'s expected fixture orders its "platforms"
    # array the same way docs/guides/fetch-v1.md's own reference table
    # does (`C.PLATFORMS`'s declared order), not "the host's platform
    # first, then whichever order the index happened to list the rest" —
    # applying every pin from one call in that canonical order (rather
    # than host-first) is what makes a from-scratch all-platforms write
    # match it; an incremental single-platform write is unaffected, since
    # `Lock.with_pin` only appends a platform the lock doesn't already
    # carry.
    all_pins = {platform_key: pin, **(extra_pins or {})}
    updated = base
    for p in C.PLATFORMS:
        key = p["key"]
        if key in all_pins:
            updated = updated.with_pin(spelling, key, all_pins[key])
    save_lock(options.lock_path, updated)


def _resolve_other_platform_pins(
    *,
    index_doc: dict,
    host_platform_key: str,
    request_spelling: str,
    index_digest: str | None,
    bases: Sequence[str],
    policy: FetchPolicy,
    retry: RetryPolicy,
    trusted_keys: tuple[TrustedKey, ...],
    scratch_root: Path,
) -> dict[str, LockPin]:
    """docs/guides/fetch-v1.md §6: "Writing a lock for every platform the
    index offers... downloads and verifies every platform's bundle but
    fetches the layer only for the host's own platform." For every OTHER
    platform in the index: fetch its manifest BY DIGEST (small JSON; never
    its layer blob), verify its own signature, and build a `LockPin` from
    digests alone."""
    pins: dict[str, LockPin] = {}
    for m in index_doc.get("manifests") or []:
        plat = m.get("platform") or {}
        key = next(
            (
                p["key"]
                for p in C.PLATFORMS
                if p["os"] == plat.get("os") and p["architecture"] == plat.get("architecture")
            ),
            None,
        )
        if key is None or key == host_platform_key:
            continue
        manifest_digest = m["digest"]
        other_manifest_doc, _raw = fetch_manifest_by_digest(
            bases, manifest_digest, policy=policy, retry=retry
        )
        layer_desc = manifest_layer_descriptor(other_manifest_doc)
        found = _find_verified_signature(
            bases,
            manifest_digest,
            policy=policy,
            retry=retry,
            trusted_keys=trusted_keys,
            scratch_root=scratch_root,
        )
        if found is None:
            raise ArtifactUntrustedError(
                f"chtypes: --lock: no trusted signature for {request_spelling} ({key})"
            )
        verified, bundle_digest, _referrer_digest = found
        statement = verified.statement
        if statement.predicate_type != C.PREDICATE_TYPE_ARTIFACT:
            raise ArtifactCorruptError(
                f"--lock: {key}'s signed predicateType was {statement.predicate_type!r}, want "
                f"{C.PREDICATE_TYPE_ARTIFACT!r}"
            )
        layer_hex = parse_digest(layer_desc.digest)
        if layer_hex not in statement.subject_sha256:
            raise ArtifactCorruptError(f"--lock: {key}'s signed subject did not match its layer")
        _check_predicate_matches_request(statement.predicate, key, request_spelling)
        pins[key] = LockPin(
            version=statement.predicate["clickhouse_version"],
            build=statement.predicate["build"],
            manifest=manifest_digest,
            layer=layer_desc.digest,
            bundle=bundle_digest,
            # never recorded — see _write_lock_pin.
            index=None,
        )
    return pins


def _maybe_write_lock(
    options: Options,
    lock: Lock | None,
    request: Request,
    platform_key: str,
    resolved: Resolved,
    *,
    index_doc: dict,
    index_digest: str | None,
    bases: Sequence[str],
    policy: FetchPolicy,
    retry: RetryPolicy,
    scratch_root: Path,
) -> None:
    """Writes the lock when `--lock` was asked for, OR when `update` just
    re-resolved an entry that must be rewritten in place (docs/guides/
    fetch-v1.md §6: "`update` re-resolves every locked request against the
    current index and rewrites the lock") — `update` alone, with no
    `lock_write`, still means a lock on disk gets a fresh entry. Only a
    `lock_write` ("`fetch --lock`") additionally writes every OTHER
    platform's pin; a plain `update` only touches the one (request,
    platform) it was asked about.
    """
    if not (options.lock_write or options.update):
        return
    extra_pins = None
    if options.lock_write:
        extra_pins = _resolve_other_platform_pins(
            index_doc=index_doc,
            host_platform_key=platform_key,
            request_spelling=request.spelling,
            index_digest=index_digest,
            bases=bases,
            policy=policy,
            retry=retry,
            trusted_keys=options.resolved_trusted_keys(),
            scratch_root=scratch_root,
        )
    _write_lock_pin(options, lock, request.spelling, platform_key, resolved, extra_pins=extra_pins)


def _ensure_floating(
    request: Request, platform_key: str, options: Options, lock: Lock | None
) -> Resolved:
    bases = options.resolved_bases()
    policy = FetchPolicy(token=options.token, clock=options.clock)
    retry = options.retry
    roots = search_roots(options.cache_dir, options.system_dirs)
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
            existing_dir,
            existing_record,
            request_spelling=request.spelling,
            already_installed=True,
            source="cache",
        )
        _maybe_write_lock(
            options,
            lock,
            request,
            platform_key,
            resolved,
            index_doc=index_doc,
            index_digest=index_digest,
            bases=bases,
            policy=policy,
            retry=retry,
            scratch_root=scratch_root,
        )
        return resolved

    manifest_doc, _manifest_bytes = fetch_manifest_by_digest(
        bases, platform_desc.digest, policy=policy, retry=retry
    )
    layer_desc = manifest_layer_descriptor(manifest_doc)

    trusted_keys = options.resolved_trusted_keys()
    found = _find_verified_signature(
        bases,
        platform_desc.digest,
        policy=policy,
        retry=retry,
        trusted_keys=trusted_keys,
        scratch_root=scratch_root,
    )

    warnings: list[str] = []
    if found is None:
        if not options.allow_unsigned:
            raise ArtifactUntrustedError(
                f"chtypes: no trusted signature for {request.spelling} ({platform_key})"
            )
        warnings.append("CHTYPES_ALLOW_UNSIGNED: no trusted signature found; proceeding unsigned")
        config_desc = manifest_config_descriptor(manifest_doc)
        if config_desc is None:
            raise ArtifactCorruptError(
                "no trusted signature and no config blob to identify the library"
            )
        config_bytes = fetch_blob_bytes(
            bases, config_desc.digest, policy=policy, retry=retry, max_bytes=C.MANIFEST_MAX_BYTES
        )
        predicate = json.loads(config_bytes)
        signed_by = None
        bundle_digest = None
        bundle_manifest_digest = None
    else:
        verified, bundle_digest, bundle_manifest_digest = found
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
    newer = _newer_installed(roots, platform_key, predicate, request.spelling)
    if newer is not None:
        newer_dir, newer_record, warning = newer
        resolved = _record_to_resolved(
            newer_dir,
            newer_record,
            request_spelling=request.spelling,
            already_installed=True,
            source="cache",
        )
        resolved = replace(resolved, warnings=(*resolved.warnings, warning))
        _maybe_write_lock(
            options,
            lock,
            request,
            platform_key,
            resolved,
            index_doc=index_doc,
            index_digest=index_digest,
            bases=bases,
            policy=policy,
            retry=retry,
            scratch_root=scratch_root,
        )
        return resolved

    dest_dir = _unpack_and_install(
        cache_root_path=cache_root_path,
        scratch_root=scratch_root,
        bases=bases,
        layer_desc=layer_desc,
        manifest_digest=platform_desc.digest,
        layer_digest=layer_desc.digest,
        bundle_digest=bundle_digest,
        bundle_manifest_digest=bundle_manifest_digest,
        index_digest=index_digest,
        platform_key=platform_key,
        predicate=predicate,
        signed_by=signed_by,
        policy=policy,
        retry=retry,
    )
    record = read_verified_record(dest_dir)
    assert record is not None

    def _add_entry(doc: dict) -> dict:
        entries = [*(doc.get("manifests") or []), {"digest": platform_desc.digest}]
        return {**doc, "manifests": entries}

    try:
        update_index_json(cache_root_path, _add_entry, before_rename=options.before_index_rename)
    except OSError:
        pass  # index.json is an oras-interop convenience; correctness never depends on it.

    resolved = _record_to_resolved(
        dest_dir,
        record,
        request_spelling=request.spelling,
        already_installed=False,
        source=bases[0] if bases else "cache",
        warnings=warnings,
    )
    _maybe_write_lock(
        options,
        lock,
        request,
        platform_key,
        resolved,
        index_doc=index_doc,
        index_digest=index_digest,
        bases=bases,
        policy=policy,
        retry=retry,
        scratch_root=scratch_root,
    )
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
    roots = search_roots(options.cache_dir, options.system_dirs)
    cache_root_path = roots[0]
    scratch_root = _scratch_root(cache_root_path)

    manifest_hex = parse_digest(pin.manifest)
    existing_dir = unpacked_dir_for(cache_root_path, manifest_hex)
    existing_record = read_verified_record(existing_dir)
    if existing_record is not None and existing_record.platform == platform_key:
        return _record_to_resolved(
            existing_dir,
            existing_record,
            request_spelling=request.spelling,
            already_installed=True,
            source="cache",
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
        cache_root_path=cache_root_path,
        scratch_root=scratch_root,
        bases=bases,
        layer_desc=layer_desc,
        manifest_digest=pin.manifest,
        layer_digest=pin.layer,
        bundle_digest=pin.bundle,
        bundle_manifest_digest=None,
        index_digest=pin.index,
        platform_key=platform_key,
        predicate=predicate,
        signed_by=verified.signed_by,
        policy=policy,
        retry=retry,
    )
    record = read_verified_record(dest_dir)
    assert record is not None
    return _record_to_resolved(
        dest_dir,
        record,
        request_spelling=request.spelling,
        already_installed=False,
        source=bases[0] if bases else "cache",
    )


def _translate_transport_error(e: TransportError) -> FetchError:
    """`_http.py`'s internal transport exceptions are untyped-by-meaning on
    purpose (its own docstring: the same HTTP outcome means different things
    depending on context) — but by the time one reaches a caller of the
    seam, the context has already been applied (`fetch_from_bases`'s own
    tag/digest 404 handling never lets a bare digest-404 escape as
    `NotFoundHttpError`), so a straight type-to-code mapping is safe here:
    a bare `NotFoundHttpError` can only come from a TAG lookup (every
    digest-mode caller already turns a persistent digest-404 into
    `UnreachableHttpError`), so it is always `CHTYPES_ARTIFACT_UNPUBLISHED`,
    never `SOURCE_UNREACHABLE`."""
    if isinstance(e, NotFoundHttpError):
        return ArtifactUnpublishedError(f"chtypes: {e}")
    if isinstance(e, UnauthorizedHttpError):
        return SourceUnauthorizedError(f"chtypes: {e}")
    if isinstance(e, ForbiddenHttpError):
        return SourceForbiddenError(f"chtypes: {e}")
    if isinstance(e, OversizeHttpError):
        return ArtifactCorruptError(f"chtypes: {e}")
    if isinstance(e, UnreachableHttpError):
        return SourceUnreachableError(f"chtypes: {e}", retryable=getattr(e, "retryable", True))
    return SourceUnreachableError(f"chtypes: {e}")


def ensure(request: Request, options: Options) -> Resolved:
    """Make sure a verified, unpacked library for `request` is on disk,
    fetching it if needed (docs/guides/fetch-v1.md "The seam").

    Lock precedence (`decided-here`; constants/PLAN §0's lock row states the
    two ends — "`--frozen` makes no lookups; `update` re-resolves" — but not
    the default in between, which the real conformance fixtures, not yet
    available in this worktree, will pin down): when a lock file exists AND
    pins this exact (request, platform) AND `options.update` is not set,
    a plain `ensure()` follows the pin by digest (the same path `--frozen`
    uses), rather than re-resolving the floating tag. `--frozen` differs
    from this default only in refusing outright when no pin exists, instead
    of falling back to a floating resolve. `update=True` always ignores
    any existing pin and resolves the floating tag fresh.
    """
    platform_key = options.resolved_platform()
    lock: Lock | None = None
    if options.lock_path is not None and os.path.exists(options.lock_path):
        lock = load_lock(options.lock_path)

    try:
        if options.offline:
            resolved = resolve_installed(request, platform_key, options)
            if resolved is None:
                raise ArtifactMissingError(
                    f"chtypes: no installed artifact for {request.spelling} "
                    f"({platform_key}); offline"
                )
            return resolved

        if options.frozen:
            return _ensure_frozen(request, platform_key, options, lock)

        if lock is not None and not options.update and lock.pin_for(request.spelling, platform_key):
            return _ensure_frozen(request, platform_key, options, lock)

        return _ensure_floating(request, platform_key, options, lock)
    except TransportError as e:
        raise _translate_transport_error(e) from e


def _verify_and_install_from_local_blobs(
    *,
    source_root: Path,
    manifest_digest: str,
    cache_write_root: Path,
    trusted_keys: tuple[TrustedKey, ...],
) -> VerifiedRecord | None:
    """docs/guides/fetch-v1.md §1/§6: "A pre-seeded layout has entries in
    `index.json` with no corresponding `unpacked/` directory yet; the first
    request for one verifies it against its signature exactly as a freshly
    downloaded layer would, then unpacks it" — and for `--offline`
    specifically, "a pre-seeded `index.json` entry with no corresponding
    `unpacked/` directory is the ONE case `--offline` still verifies and
    unpacks before answering." Entirely local: every byte comes from
    `source_root/blobs/sha256/*`, never the network. Returns `None` — never
    raises — for a missing, unreadable or untrusted entry, the same
    tolerant shape `_find_verified_signature` uses for a network referrer:
    a bad pre-seed is "nothing usable here," not a crash."""
    blobs_dir = source_root / "blobs" / "sha256"
    if not blobs_dir.is_dir():
        return None
    manifest_hex = parse_digest(manifest_digest)
    manifest_path = blobs_dir / manifest_hex
    if not manifest_path.is_file():
        return None
    try:
        manifest_doc = json.loads(manifest_path.read_bytes())
        layer_desc = manifest_layer_descriptor(manifest_doc)
    except (ValueError, OSError, FetchError):
        return None

    verified = None
    bundle_digest = None
    bundle_manifest_digest = None
    for blob_path in blobs_dir.iterdir():
        try:
            doc = json.loads(blob_path.read_bytes())
        except (ValueError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict) or doc.get("mediaType") != C.MEDIA_TYPE_MANIFEST:
            continue
        subject = doc.get("subject") or {}
        if subject.get("digest") != manifest_digest:
            continue
        try:
            referrer_layer = manifest_single_layer(doc, expected_media_type=C.MEDIA_TYPE_BUNDLE)
            bundle_bytes = (blobs_dir / parse_digest(referrer_layer.digest)).read_bytes()
            candidate = verify_bundle(json.loads(bundle_bytes), trusted_keys)
        except (ValueError, OSError, FetchError):
            continue
        if candidate is not None:
            verified = candidate
            bundle_digest = referrer_layer.digest
            bundle_manifest_digest = "sha256:" + blob_path.name
            break
    if verified is None:
        return None

    predicate = verified.statement.predicate
    layer_hex = parse_digest(layer_desc.digest)
    if layer_hex not in verified.statement.subject_sha256:
        return None
    platform_key = next(
        (
            p["key"]
            for p in C.PLATFORMS
            if p["os"] == predicate.get("os") and p["architecture"] == predicate.get("arch")
        ),
        None,
    )
    if platform_key is None:
        return None
    layer_path = blobs_dir / layer_hex
    if not layer_path.is_file():
        return None

    scratch = _scratch_root(cache_write_root)
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
            manifest=manifest_digest,
            layer=layer_desc.digest,
            bundle=bundle_digest,
            bundle_manifest=bundle_manifest_digest,
            index=None,
            platform=platform_key,
            version=predicate["clickhouse_version"],
            build=predicate["build"],
            channel=predicate.get("channel"),
            predicate=predicate,
            signed_by=verified.signed_by,
            library=predicate["library"],
            library_sha256=predicate["library_sha256"],
            library_bytes=predicate["library_bytes"],
        )
        write_verified_install(
            cache_write_root, manifest_digest, record, unpacked_tmp_dir=unpacked_dir
        )
        return record
    except (ArtifactCorruptError, OSError):
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _verify_preseeded_entries(roots: Sequence[Path], trusted_keys: tuple[TrustedKey, ...]) -> None:
    """Scans every root's `index.json` for an entry with no `verified.json`
    anywhere yet, and verifies+unpacks it from that root's OWN local blobs,
    writing the result into `roots[0]` (the writable cache) regardless of
    which root — including a read-only system directory — the blobs came
    from."""
    cache_write_root = roots[0]
    already_verified = {record.manifest for _dir, record in list_verified_records(roots)}
    for root in roots:
        index_path = root / "index.json"
        try:
            index_doc = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for m in index_doc.get("manifests") or []:
            digest = m.get("digest")
            if not digest or digest in already_verified:
                continue
            record = _verify_and_install_from_local_blobs(
                source_root=root,
                manifest_digest=digest,
                cache_write_root=cache_write_root,
                trusted_keys=trusted_keys,
            )
            if record is not None:
                already_verified.add(digest)


def resolve_installed(request: Request, platform: str, options: Options) -> Resolved | None:
    """Cache-only: never touches the network (docs/guides/fetch-v1.md "The seam").

    The source of truth is the immutable `unpacked/sha256/*/verified.json`
    records (PLAN §3.4), never `index.json` — a lost `index.json` race costs
    only `oras` interop, never correctness here. The ONE exception
    (docs/guides/fetch-v1.md §1/§6): a pre-seeded `index.json` entry with no
    `verified.json` of its own yet is verified-then-unpacked from local
    blobs before matching, so `--offline` still answers for a cache that
    was only ever pre-seeded (`oras copy --to-oci-layout`), never fetched.
    """
    roots = search_roots(options.cache_dir, options.system_dirs)
    _verify_preseeded_entries(roots, options.resolved_trusted_keys())
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
        dir_path,
        record,
        request_spelling=request.spelling,
        already_installed=True,
        source=_source_for_dir(dir_path, roots),
    )


def list_installed(options: Options) -> list[Resolved]:
    """Every verified install across the cache and the read-only system
    directories, cache-only (docs/guides/fetch-v1.md "The seam")."""
    roots = search_roots(options.cache_dir, options.system_dirs)
    return [
        _record_to_resolved(
            dir_path,
            record,
            request_spelling=record.version,
            already_installed=True,
            source=_source_for_dir(dir_path, roots),
        )
        for dir_path, record in list_verified_records(roots)
    ]


def verify_installed(options: Options) -> list[VerifyResult]:
    """Re-hash every installed library's on-disk bytes against its own
    `verified.json` record, cache-only."""
    roots = search_roots(options.cache_dir, options.system_dirs)
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
                    dir_path,
                    record.platform,
                    record.version,
                    record.build,
                    False,
                    "on-disk library no longer matches its verified record",
                )
            )
        else:
            out.append(
                VerifyResult(dir_path, record.platform, record.version, record.build, True, "")
            )
    return out


def fetch_signed(repository: str, ref: str, predicate_type: str, options: Options) -> dict:
    """A generic signed-artifact fetch, for goldens and fetch fixtures.
    `repository` is a path suffix joined onto every configured base (`""`
    for the same repository as the platform artifact,
    `constants.FIXTURES_REPO_SUFFIX` for fixtures).

    What `ref` means depends on `predicate_type` (`decided-here`, since the
    frozen seam does not spell this split itself): goldens are an OCI
    REFERRER of a platform manifest (D7), never fetched by their own
    digest/tag directly, so when `predicate_type` is
    `constants.PREDICATE_TYPE_GOLDENS`, `ref` is the SUBJECT (the platform
    manifest)'s digest. The registry is append-only, so a corrected set is a
    SECOND goldens referrer of it: this verifies every candidate and returns
    the one with the highest predicate `revision` (§9; `_goldens.py`).
    For every other predicate type (fixtures today), `ref` is the artifact's
    OWN digest or tag to fetch directly — fixtures are pinned by digest with
    no tag fallback ever tried (§7.7).

    Returns `{"path": <verified layer bytes on disk>, "statement": <predicate
    dict, or None under allow-unsigned>, "digests": {"manifest", "layer",
    "bundle"}}`. Never dlopens or interprets the content — that is for the
    caller.
    """
    try:
        return _fetch_signed_impl(repository, ref, predicate_type, options)
    except TransportError as e:
        raise _translate_transport_error(e) from e


def _fetch_signed_impl(repository: str, ref: str, predicate_type: str, options: Options) -> dict:
    bases = tuple(b.rstrip("/") + repository for b in options.resolved_bases())
    policy = FetchPolicy(token=options.token, clock=options.clock)
    retry = options.retry
    cache_root_path = resolve_cache_root(options.cache_dir)
    scratch_root = _scratch_root(cache_root_path)
    trusted_keys = options.resolved_trusted_keys()

    if predicate_type == C.PREDICATE_TYPE_GOLDENS:
        return _fetch_goldens(
            bases,
            ref,
            policy=policy,
            retry=retry,
            trusted_keys=trusted_keys,
            allow_unsigned=options.allow_unsigned,
            cache_root_path=cache_root_path,
            scratch_root=scratch_root,
        )
    if ref.startswith("sha256:"):
        doc, _raw = fetch_manifest_by_digest(bases, ref, policy=policy, retry=retry)
        manifest_digest = ref
    else:
        doc, _raw, manifest_digest = fetch_manifest_by_tag(bases, ref, policy=policy, retry=retry)

    layer_desc = manifest_single_layer(doc)
    found = _find_verified_signature(
        bases,
        manifest_digest,
        policy=policy,
        retry=retry,
        trusted_keys=trusted_keys,
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
            "manifest": manifest_digest,
            "layer": layer_desc.digest,
            "bundle": bundle_digest,
        },
    }


def _fetch_goldens(
    bases: Sequence[str],
    subject: str,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
    trusted_keys: tuple[TrustedKey, ...],
    allow_unsigned: bool,
    cache_root_path: Path,
    scratch_root: Path,
) -> dict:
    """`fetch_signed` for the goldens predicate type: the highest-revision
    verified goldens referrer of the platform manifest `subject`.

    Every candidate is verified on its own (its blob against its descriptor,
    its own signature referrer, the statement's subject against the blob
    digest, the predicateType). A candidate that fails is skipped: never
    chosen, never a tie. If none verifies, the first failure's error is
    raised. A VERIFIED candidate whose `revision` is unusable is not skipped:
    it could be the newest document, and quietly choosing an older set is the
    stale pick this rule exists to prevent."""
    if not subject.startswith("sha256:"):
        raise ArtifactCorruptError(
            f"a goldens fetch names a platform manifest by digest, not {subject!r}"
        )
    listed = discover_referrers(bases, subject, C.GOLDENS_ARTIFACT_TYPE, policy=policy, retry=retry)
    if not listed:
        raise ArtifactUnpublishedError(f"chtypes: no goldens referrer for {subject}")

    verified: list[GoldensCandidate] = []
    unsigned: list[tuple[str, str, str]] = []  # (manifest digest, blob digest, scratch path)
    first_failure: Exception | None = None
    seen: set[str] = set()
    tmp_dir = tempfile.mkdtemp(dir=str(scratch_root))
    try:
        for referrer in listed:
            if referrer.digest in seen:
                continue
            seen.add(referrer.digest)
            try:
                doc, _raw = fetch_manifest_by_digest(
                    bases, referrer.digest, policy=policy, retry=retry
                )
                blob_desc = manifest_single_layer(doc)
                blob_path = os.path.join(tmp_dir, parse_digest(blob_desc.digest))
                fetch_blob_to_path(
                    bases,
                    blob_desc,
                    blob_path,
                    policy=policy,
                    retry=retry,
                    max_bytes=C.MAX_UNPACKED_BYTES,
                )
            except (TransportError, FetchError) as e:
                first_failure = first_failure or e
                continue
            unsigned.append((referrer.digest, blob_desc.digest, blob_path))
            found = _verify_goldens_signature(
                bases,
                referrer.digest,
                blob_desc.digest,
                policy=policy,
                retry=retry,
                trusted_keys=trusted_keys,
                scratch_root=scratch_root,
            )
            if isinstance(found, Exception):
                first_failure = first_failure or found
                continue
            if found is None:
                first_failure = first_failure or ArtifactUntrustedError(
                    f"chtypes: no goldens signature of {referrer.digest} verifies "
                    "under a trusted key"
                )
                continue
            verified_bundle, bundle_digest = found
            revision = statement_revision(verified_bundle.payload)
            verified.append(
                GoldensCandidate(
                    manifest=referrer.digest,
                    blob=blob_desc.digest,
                    revision=revision,
                    path=blob_path,
                    predicate=verified_bundle.statement.predicate,
                    signed_by=verified_bundle.signed_by,
                    bundle=bundle_digest,
                )
            )

        statement: dict | None
        bundle_out: str | None
        if verified:
            best = select_goldens(verified)
            manifest_digest, blob_digest, blob_path = best.manifest, best.blob, best.path
            statement, bundle_out = best.predicate, best.bundle
        elif allow_unsigned and len(unsigned) == 1:
            manifest_digest, blob_digest, blob_path = unsigned[0]
            statement, bundle_out = None, None
        elif allow_unsigned and len(unsigned) > 1:
            raise ArtifactCorruptError(
                f"{len(unsigned)} unsigned goldens documents of {subject}: no signature to "
                "read a revision from, so none can be chosen"
            )
        elif first_failure is not None:
            raise first_failure
        else:
            raise ArtifactUntrustedError(
                f"chtypes: no goldens referrer of {subject} verifies under a trusted key"
            )

        dest_dir = cache_root_path / "fetched-signed"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = str(dest_dir / parse_digest(blob_digest))
        os.replace(blob_path, dest_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {
        "path": dest_path,
        "statement": statement,
        "digests": {"manifest": manifest_digest, "layer": blob_digest, "bundle": bundle_out},
    }


def _verify_goldens_signature(
    bases: Sequence[str],
    goldens_manifest_digest: str,
    blob_digest: str,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
    trusted_keys: tuple[TrustedKey, ...],
    scratch_root: Path,
):
    """The first signature referrer of one goldens manifest that verifies
    under a trusted key AND whose statement is a goldens statement about
    `blob_digest`. Returns `(VerifiedBundle, bundle blob digest)`; `None`
    when no bundle verifies at all; or the first `ArtifactCorruptError` a
    bundle that did verify (or could not be parsed) produced, for the caller
    to skip this candidate with."""
    failure: Exception | None = None
    referrers = discover_referrers(
        bases, goldens_manifest_digest, C.MEDIA_TYPE_BUNDLE, policy=policy, retry=retry
    )
    for ref in referrers:
        tmp_dir = tempfile.mkdtemp(dir=str(scratch_root))
        try:
            referrer_doc, _raw = fetch_manifest_by_digest(
                bases, ref.digest, policy=policy, retry=retry
            )
            bundle_desc = manifest_single_layer(
                referrer_doc, expected_media_type=C.MEDIA_TYPE_BUNDLE
            )
            bundle_path = os.path.join(tmp_dir, "bundle.json")
            fetch_blob_to_path(
                bases,
                bundle_desc,
                bundle_path,
                policy=policy,
                retry=retry,
                max_bytes=C.BUNDLE_MAX_BYTES,
            )
            with open(bundle_path, "rb") as f:
                bundle_json = json.loads(f.read())
            verified = verify_bundle(bundle_json, trusted_keys)
        except ArtifactCorruptError as e:
            failure = failure or e
            continue
        except (TransportError, FetchError, json.JSONDecodeError):
            continue
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if verified is None:
            continue
        if verified.statement.predicate_type != C.PREDICATE_TYPE_GOLDENS:
            failure = failure or ArtifactCorruptError(
                f"signed predicateType was {verified.statement.predicate_type!r}, want "
                f"{C.PREDICATE_TYPE_GOLDENS!r}"
            )
            continue
        if parse_digest(blob_digest) not in verified.statement.subject_sha256:
            failure = failure or ArtifactCorruptError(
                "signed subject digest did not match the goldens blob"
            )
            continue
        return verified, bundle_desc.digest
    return failure
