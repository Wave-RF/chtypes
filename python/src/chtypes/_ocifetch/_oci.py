"""Resolve: image index -> platform manifest, plus OCI descriptor shapes
shared with `_referrers.py` (layout-v2 spec §5.1, §7.1).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._errors import (
    ArtifactCorruptError,
    ArtifactUnpublishedError,
    SourceIncompatibleError,
)
from chtypes._ocifetch._http import FetchPolicy, RetryPolicy, fetch_from_bases, get_json

__all__ = [
    "Descriptor",
    "fetch_blob_bytes",
    "fetch_blob_to_path",
    "fetch_manifest_by_digest",
    "fetch_manifest_by_tag",
    "manifest_config_descriptor",
    "manifest_layer_descriptor",
    "manifest_single_layer",
    "parse_digest",
    "resolve_platform_manifest",
    "spelling_components",
    "validate_spelling",
    "verify_body_matches_digest",
    "verify_json_descriptor",
    "version_within_request",
]

_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")


@dataclass(frozen=True)
class Descriptor:
    media_type: str
    digest: str  # "sha256:<hex>"
    size: int
    artifact_type: str | None = None
    platform: dict | None = None
    annotations: dict | None = None


def parse_digest(digest: str) -> str:
    """Validate `sha256:<64 hex>` and return the raw hex part."""
    m = _DIGEST_RE.match(digest)
    if not m:
        raise ArtifactCorruptError(f"malformed digest {digest!r}, want sha256:<64 hex>")
    return m.group(1)


def validate_spelling(spelling: str) -> None:
    """Refuse a v0-era, decorated spelling before any network call
    (constants: `refuse_hint_regex` — a leading `v` or a `-lts`/`-stable`
    suffix). This is a client-side input error, not a fetch-layer error
    code.

    `spelling.regex` (the dotted N/N.N/N.N.N/N.N.N.N shape) is NOT enforced
    here as a rejection gate: a request is, at the wire level, simply the
    tag `GET manifests/<tag>` names, and an opaque, non-version-shaped tag
    (a mirror's own `b<build>` convention, §7.7; this suite's own mnemonic
    fixture tags) is a legitimate tag to resolve — refusing it would be
    rejecting something the server might perfectly well serve. Component
    matching for such a tag is simply skipped (`version_within_request`
    below), the same way a digest-pinned `fetch_signed` ref has nothing to
    cross-check either."""
    # re.search, not re.match: the pattern is "^v|-(lts|stable)$" — each
    # alternative carries its OWN anchor, so re.match's own start-anchoring
    # would require the second alternative's leading "-" to sit at index 0,
    # which it never does ("26.8.15.10-lts" would silently NOT match).
    if re.search(C.SPELLING_REFUSE_HINT_REGEX, spelling):
        raise ValueError(
            f"chtypes: {spelling!r} is not a v1 version spelling. "
            f"Use the bare ClickHouse version, e.g. '26.8', '26.8.15' or "
            f"'26.8.15.10' — no leading 'v', no '-lts'/'-stable' suffix."
        )


def spelling_components(spelling: str) -> tuple[int, ...]:
    return tuple(int(p) for p in spelling.split("."))


def version_within_request(predicate_version: str, requested_spelling: str) -> bool:
    """layout-v2 spec §7.2 / PLAN M4: the predicate's version must lie
    within a floating request (a component prefix) and equal an exact one.
    A four-part request is exact; fewer parts is a prefix match.

    A request that is not itself a recognizable N/N.N/N.N.N/N.N.N.N spelling
    (an opaque tag — see `validate_spelling`) has no numeric structure to
    cross-check against, so this returns `True` unconditionally: trust for
    an opaque tag rests entirely on the signature, exactly as it does for a
    digest-pinned `fetch_signed` ref."""
    if not re.match(C.SPELLING_REGEX, requested_spelling):
        return True
    pred = spelling_components(predicate_version)
    req = spelling_components(requested_spelling)
    if len(req) == 4:
        return pred == req
    return pred[: len(req)] == req


def verify_json_descriptor(body: bytes, descriptor: Descriptor, *, what: str) -> None:
    """Check a downloaded JSON document's bytes against its own descriptor,
    BEFORE any further parsing — size and sha256, exactly as §7.3 requires
    for the layer, applied here to the smaller JSON objects too. Use this
    only when `descriptor.size` came from an independent source (an
    index's manifest descriptor, a layer descriptor) — never construct a
    descriptor from the body being checked, which would make the size check
    vacuous."""
    if len(body) != descriptor.size:
        raise ArtifactCorruptError(
            f"{what}: size {len(body)} disagreed with the descriptor's {descriptor.size}"
        )
    actual = hashlib.sha256(body).hexdigest()
    expected = parse_digest(descriptor.digest)
    if actual != expected:
        raise ArtifactCorruptError(f"{what}: sha256 {actual} disagreed with {expected}")


def verify_body_matches_digest(body: bytes, digest: str, *, what: str) -> None:
    """Content-addressing check with no separately known size: the fetch was
    BY that digest, so the only thing to confirm is that the bytes actually
    hash to it."""
    actual = hashlib.sha256(body).hexdigest()
    expected = parse_digest(digest)
    if actual != expected:
        raise ArtifactCorruptError(
            f"{what}: sha256 {actual} disagreed with the requested {expected}"
        )


def resolve_platform_manifest(index_doc: dict, platform_key: str) -> Descriptor:
    """Pick the one manifest descriptor in an OCI image index matching
    `platform_key` (e.g. "linux-arm64"), refusing a duplicate or absent
    platform (layout-v2 spec §7.1; PLAN §3.2 "index-duplicate-platform")."""
    media_type = index_doc.get("mediaType")
    if media_type != C.MEDIA_TYPE_INDEX:
        raise SourceIncompatibleError(
            f"manifest mediaType {media_type!r} is not a recognized image index "
            f"({C.MEDIA_TYPE_INDEX!r})"
        )
    platform_spec = next((p for p in C.PLATFORMS if p["key"] == platform_key), None)
    if platform_spec is None:
        raise ValueError(f"chtypes: unknown platform {platform_key!r}")
    manifests = index_doc.get("manifests")
    if not isinstance(manifests, list):
        raise ArtifactCorruptError("image index had no manifests array")
    matches = []
    offered: list[str] = []
    for m in manifests:
        plat = m.get("platform") or {}
        key = f"{plat.get('os')}-{plat.get('architecture')}"
        offered.append(key)
        if (
            plat.get("os") == platform_spec["os"]
            and plat.get("architecture") == platform_spec["architecture"]
        ):
            matches.append(m)
    if not matches:
        raise ArtifactUnpublishedError(
            f"no manifest for platform {platform_key!r}; index offers {sorted(set(offered))}"
        )
    if len(matches) > 1:
        raise ArtifactCorruptError(
            f"image index listed {len(matches)} manifests for platform {platform_key!r}"
        )
    m = matches[0]
    return Descriptor(
        media_type=m.get("mediaType", ""),
        digest=m.get("digest", ""),
        size=m.get("size", -1),
        artifact_type=m.get("artifactType"),
        platform=m.get("platform"),
        annotations=m.get("annotations"),
    )


def manifest_single_layer(
    manifest_doc: dict, *, expected_media_type: str | None = None
) -> Descriptor:
    """The single layer of any OCI image manifest — a platform artifact
    manifest (one tar+zstd layer) or a referrer manifest (one bundle/JSON
    layer carrying its actual payload). `expected_media_type` is checked
    only when given; a referrer's layer media type is not pinned by the
    generated constants, so callers that accept more than one shape pass
    `None`."""
    media_type = manifest_doc.get("mediaType")
    if media_type != C.MEDIA_TYPE_MANIFEST:
        raise SourceIncompatibleError(
            f"manifest mediaType {media_type!r} is not a recognized image manifest "
            f"({C.MEDIA_TYPE_MANIFEST!r})"
        )
    layers = manifest_doc.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        raise ArtifactCorruptError(
            f"manifest had {len(layers) if isinstance(layers, list) else 'no'} "
            f"layers, want exactly 1"
        )
    layer = layers[0]
    if expected_media_type is not None and layer.get("mediaType") != expected_media_type:
        raise SourceIncompatibleError(
            f"layer mediaType {layer.get('mediaType')!r} is not {expected_media_type!r}"
        )
    return Descriptor(
        media_type=layer.get("mediaType", ""),
        digest=layer["digest"],
        size=layer["size"],
        annotations=layer.get("annotations"),
    )


def manifest_layer_descriptor(manifest_doc: dict) -> Descriptor:
    """The single tar+zstd layer of a platform artifact manifest
    (layout-v2 spec §0: "one layer per platform manifest")."""
    return manifest_single_layer(manifest_doc, expected_media_type=C.MEDIA_TYPE_LAYER)


def manifest_config_descriptor(manifest_doc: dict) -> Descriptor | None:
    """The config blob descriptor, or `None` for the empty-config shape."""
    config = manifest_doc.get("config")
    if not isinstance(config, dict):
        return None
    if config.get("mediaType") == C.MEDIA_TYPE_EMPTY_CONFIG:
        return None
    return Descriptor(
        media_type=config.get("mediaType", ""), digest=config["digest"], size=config["size"]
    )


def fetch_manifest_by_tag(
    bases,
    tag: str,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
    max_bytes: int = C.MANIFEST_MAX_BYTES,
) -> tuple[dict, bytes, str]:
    """GET manifests/<tag>; returns (doc, raw_bytes, resolved_digest)."""
    accept = (C.MEDIA_TYPE_INDEX, C.MEDIA_TYPE_MANIFEST)
    doc, resp = get_json(
        bases,
        f"/manifests/{tag}",
        mode="tag",
        accept=accept,
        max_bytes=max_bytes,
        policy=policy,
        retry=retry,
    )
    digest = f"sha256:{hashlib.sha256(resp.body).hexdigest()}"
    return doc, resp.body, digest


def fetch_blob_to_path(
    bases,
    descriptor: Descriptor,
    dest_path: str,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
    max_bytes: int,
) -> None:
    """`GET blobs/<digest>`, verified against `descriptor` (size and
    sha256) BEFORE the caller does anything else with the bytes — in
    particular, before any decompression (layout-v2 spec §7.3: "Check size
    and sha256 before decompressing").

    Writes to `dest_path` by temp-then-rename within the same directory,
    and removes the temp file on any verification failure, so a tampered
    download never leaves a partial artifact for a later step to trip over
    (PLAN §3.2 "tampered-layer": "the temp is removed")."""
    resp = fetch_from_bases(
        bases,
        f"/blobs/{descriptor.digest}",
        mode="digest",
        accept=None,
        max_bytes=max_bytes,
        policy=policy,
        retry=retry,
    )
    directory = os.path.dirname(os.path.abspath(dest_path)) or "."
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".ocifetch-blob-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(resp.body)
        verify_json_descriptor(resp.body, descriptor, what=f"blob {descriptor.digest}")
        os.replace(tmp_name, dest_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def fetch_blob_bytes(
    bases, digest: str, *, policy: FetchPolicy, retry: RetryPolicy, max_bytes: int
) -> bytes:
    """`GET blobs/<digest>`, verified by content hash alone (no separately
    known size — used for `--frozen`, where the lock pins only the digest,
    never a size)."""
    resp = fetch_from_bases(
        bases,
        f"/blobs/{digest}",
        mode="digest",
        accept=None,
        max_bytes=max_bytes,
        policy=policy,
        retry=retry,
    )
    verify_body_matches_digest(resp.body, digest, what=f"blob {digest}")
    return resp.body


def fetch_manifest_by_digest(
    bases,
    digest: str,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
    max_bytes: int = C.MANIFEST_MAX_BYTES,
) -> tuple[dict, bytes]:
    """GET manifests/<digest>, verified against the digest requested."""
    accept = (C.MEDIA_TYPE_INDEX, C.MEDIA_TYPE_MANIFEST)
    doc, resp = get_json(
        bases,
        f"/manifests/{digest}",
        mode="digest",
        accept=accept,
        max_bytes=max_bytes,
        policy=policy,
        retry=retry,
    )
    verify_body_matches_digest(resp.body, digest, what="manifest")
    return doc, resp.body
