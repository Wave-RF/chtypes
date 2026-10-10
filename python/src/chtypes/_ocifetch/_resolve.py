"""`chtypes resolve` (docs/guides/fetch-v1.md §3, "What a request resolves to";
public issue #493).

What a line, a patch or an exact version resolves to on every platform the
registry offers, each verified exactly as a fetch verifies it (§4), and never
a layer downloaded. Under offline it is the cache's answer instead, the lookup
an open makes. Go, TypeScript and Rust give the same answer.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._ensure import (
    Options,
    Request,
    _cached_own_build,
    _check_predicate_matches_request,
    _find_verified_signature,
    _translate_transport_error,
    missing_notes,
    resolve_installed,
    with_notes,
)
from chtypes._ocifetch._errors import (
    ArtifactCorruptError,
    ArtifactMissingError,
    ArtifactUnpublishedError,
    ArtifactUntrustedError,
    FetchError,
)
from chtypes._ocifetch._faults import unwritable
from chtypes._ocifetch._http import FetchPolicy, TransportError
from chtypes._ocifetch._layout import search_roots
from chtypes._ocifetch._oci import (
    fetch_blob_bytes,
    fetch_manifest_by_digest,
    fetch_manifest_by_tag,
    manifest_config_descriptor,
    manifest_layer_descriptor,
    parse_digest,
    resolve_platform_manifest,
)

__all__ = ["PlatformOutcome", "Resolution", "resolve", "resolve_each"]


@dataclass(frozen=True)
class Resolution:
    """What a request resolves to on one platform: the signed version and
    build, and the platform manifest's digest. `predicate` is the predicate the
    answer was read from (the verified statement's); it is carried for a caller
    that reads a field the command line does not print, such as `glibc_floor`,
    and takes no part in equality."""

    platform: str
    version: str
    build: str
    manifest: str
    predicate: Mapping[str, object] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class PlatformOutcome:
    """One platform's answer from `resolve_each`: exactly one of `resolution`
    (verified), `error` (the index offered the platform and its statement or
    manifest failed to verify or parse) or neither (the index does not offer
    this platform)."""

    platform: str
    resolution: Resolution | None = None
    error: FetchError | None = None


def resolve(request: Request, options: Options) -> tuple[list[Resolution], list[str]]:
    """What `request` resolves to, one `Resolution` per platform, in the
    constants' platform order, and allow-unsigned's warnings. Online it
    resolves the index (through the dev channel's alias first, §3), and for
    each platform the index offers it fetches the manifest by digest and
    verifies its signed statement against the request (§4); no layer is
    requested and nothing is written to the cache. A platform the index does
    not offer is left out. Offline it answers from the cache: each platform's
    `resolve_installed`, and `ArtifactMissingError` when no platform has an
    installed build."""
    _channel.enforce(options)
    roots = search_roots(options.cache_dir, options.system_dirs)
    try:
        if options.resolved_offline():
            return _resolve_offline(request, options), []
        return _resolve_online(request, options, roots)
    except TransportError as exc:
        raise _translate_transport_error(exc) from exc
    except OSError as exc:
        typed = unwritable(exc, roots)
        if typed is None:
            raise
        raise typed from exc


def _resolve_offline(request: Request, options: Options) -> list[Resolution]:
    out: list[Resolution] = []
    for p in C.PLATFORMS:
        resolved = resolve_installed(request, p["key"], options)
        if resolved is not None:
            out.append(
                Resolution(
                    platform=p["key"],
                    version=resolved.version,
                    build=resolved.build,
                    manifest=str(resolved.digests["manifest"]),
                )
            )
    if not out:
        raise ArtifactMissingError(
            with_notes(
                f"chtypes: no installed artifact for {request.spelling} on any platform, "
                "and --offline forbids a network fetch",
                missing_notes(options),
            )
        )
    return out


def _resolve_online(
    request: Request, options: Options, roots: list[Path] | tuple[Path, ...]
) -> tuple[list[Resolution], list[str]]:
    out: list[Resolution] = []
    warnings: list[str] = []
    for outcome in _online_outcomes(request, options, roots, warnings, raise_on_error=True):
        if outcome.resolution is not None:
            out.append(outcome.resolution)
    if not out:
        raise ArtifactUnpublishedError(
            f"chtypes: the index for {request.spelling} offers no platform this SDK knows"
        )
    return out, warnings


def resolve_each(request: Request, options: Options) -> list[PlatformOutcome]:
    """`resolve`, online, with each platform's failure kept apart: one
    `PlatformOutcome` per platform in the constants' order, a verified
    `Resolution`, the `FetchError` that platform's statement or manifest
    raised, or neither when the index does not offer the platform. The index
    itself failing (a missing line, an unreachable registry) still raises, as
    in `resolve`; nothing is requested beyond what `resolve` requests, and a
    platform's failure never stops the next from being verified. Verification
    is `resolve`'s own, line for line."""
    _channel.enforce(options)
    roots = search_roots(options.cache_dir, options.system_dirs)
    try:
        return list(_online_outcomes(request, options, roots, [], raise_on_error=False))
    except TransportError as exc:
        raise _translate_transport_error(exc) from exc
    except OSError as exc:
        typed = unwritable(exc, roots)
        if typed is None:
            raise
        raise typed from exc


def _online_outcomes(
    request: Request,
    options: Options,
    roots: list[Path] | tuple[Path, ...],
    warnings: list[str],
    *,
    raise_on_error: bool,
) -> list[PlatformOutcome]:
    bases = options.resolved_bases()
    policy = FetchPolicy(token=options.token, clock=options.clock)
    retry = options.retry
    index_doc, _index_bytes, _index_digest, alias_absent = fetch_manifest_by_tag(
        bases,
        request.spelling,
        policy=policy,
        retry=retry,
        alias=_channel.alias_tag(request.spelling),
    )
    out: list[PlatformOutcome] = []
    # Bundles are read through a scratch directory OUTSIDE the cache: a resolve
    # writes nothing there.
    with tempfile.TemporaryDirectory(prefix="resolve-scratch-") as scratch:
        for p in C.PLATFORMS:
            platform_key = p["key"]
            try:
                desc = resolve_platform_manifest(index_doc, platform_key)
            except ArtifactUnpublishedError:
                out.append(PlatformOutcome(platform_key))  # the index does not offer it
                continue
            if raise_on_error:
                resolution = _resolve_platform(
                    request,
                    options,
                    roots,
                    warnings,
                    bases,
                    policy,
                    retry,
                    index_doc,
                    alias_absent,
                    scratch,
                    platform_key,
                    desc,
                )
                out.append(PlatformOutcome(platform_key, resolution))
                continue
            try:
                resolution = _resolve_platform(
                    request,
                    options,
                    roots,
                    warnings,
                    bases,
                    policy,
                    retry,
                    index_doc,
                    alias_absent,
                    scratch,
                    platform_key,
                    desc,
                )
            except TransportError as exc:
                out.append(PlatformOutcome(platform_key, error=_translate_transport_error(exc)))
            except FetchError as exc:
                out.append(PlatformOutcome(platform_key, error=exc))
            else:
                out.append(PlatformOutcome(platform_key, resolution))
    return out


def _resolve_platform(
    request: Request,
    options: Options,
    roots: list[Path] | tuple[Path, ...],
    warnings: list[str],
    bases: tuple[str, ...],
    policy: FetchPolicy,
    retry,  # noqa: ANN001
    index_doc,  # noqa: ANN001
    alias_absent,  # noqa: ANN001
    scratch: str,
    platform_key: str,
    desc,  # noqa: ANN001
) -> Resolution:
    manifest_doc, _manifest_bytes = fetch_manifest_by_digest(
        bases, desc.digest, policy=policy, retry=retry
    )
    layer_desc = manifest_layer_descriptor(manifest_doc)
    found = _find_verified_signature(
        bases,
        desc.digest,
        policy=policy,
        retry=retry,
        trusted_keys=options.resolved_trusted_keys(),
        scratch_root=Path(scratch),
    )
    if found is None:
        if not options.resolved_allow_unsigned():
            raise ArtifactUntrustedError(
                f"chtypes: no trusted signature for {request.spelling} ({platform_key})"
            )
        warnings.append("CHTYPES_ALLOW_UNSIGNED: no trusted signature found; proceeding unsigned")
        config_desc = manifest_config_descriptor(manifest_doc)
        if config_desc is None:
            raise ArtifactCorruptError(
                "no trusted signature and no config blob to identify the library"
            )
        predicate = json.loads(
            fetch_blob_bytes(
                bases,
                config_desc.digest,
                policy=policy,
                retry=retry,
                max_bytes=C.MANIFEST_MAX_BYTES,
            )
        )
    else:
        verified, _bundle_digest, _bundle_manifest_digest = found
        statement = verified.statement
        if statement.predicate_type != C.PREDICATE_TYPE_ARTIFACT:
            raise ArtifactCorruptError(
                f"signed predicateType was {statement.predicate_type!r}, want "
                f"{C.PREDICATE_TYPE_ARTIFACT!r}"
            )
        if parse_digest(layer_desc.digest) not in statement.subject_sha256:
            raise ArtifactCorruptError("signed subject digest did not match the manifest's layer")
        predicate = statement.predicate
    _check_predicate_matches_request(predicate, platform_key, request.spelling)
    if found is not None:
        _channel.ahead_of_registry(
            alias_absent,
            predicate,
            cached_own_build=lambda key=platform_key: _cached_own_build(
                roots, key, request.spelling
            ),
        )
    return Resolution(
        platform=platform_key,
        version=str(predicate.get("clickhouse_version")),
        build=str(predicate.get("build")),
        manifest=desc.digest,
        predicate=predicate,
    )
