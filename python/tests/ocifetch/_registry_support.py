"""Build a minimal, hand-written `file://` OCI route tree for integration
tests of `_ensure.py`, independent of the fixtures tree lane 0B owns.

This is deliberately NOT a reimplementation of that fixtures generator: it
writes just enough of a repository (`manifests/`, `blobs/`, `referrers/`)
for `ensure()` to resolve, verify and unpack a single platform/version, so
the seam's wiring is exercised end to end without a network and without
0B's trees. It is not a substitute for the real conformance suite.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

try:
    from compression import zstd
except ImportError:
    from backports import zstd  # type: ignore[no-redef]

from chtypes._ocifetch import _constants as C

from ._bundle_support import build_bundle
from ._sign_support import _public_key_for, generate_keypair

EMPTY_CONFIG_DIGEST = "sha256:" + hashlib.sha256(b"{}").hexdigest()


@dataclass(frozen=True)
class BuiltTree:
    root: Path  # the repository root (the path a `file://` base names)
    manifest_digest: str
    layer_digest: str
    index_digest: str
    bundle_digest: str
    referrer_manifest_digest: str
    library_name: str
    library_bytes: bytes
    seed: bytes
    public_key: bytes
    keyid: str
    predicate: dict

    @property
    def base_url(self) -> str:
        return f"file://{self.root}"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _tar_zst(library_name: str, library_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=library_name)
        info.size = len(library_bytes)
        tar.addfile(info, io.BytesIO(library_bytes))
    return zstd.compress(buf.getvalue())


def build_tree(
    tmp_path: Path,
    *,
    tag: str = "26.8",
    version: str = "26.8.15.10",
    build: str = "20261001.183455",
    channel: str = "lts",
    os_name: str = "linux",
    arch: str = "arm64",
    library_name: str = "libchtypes.so",
    library_bytes: bytes = b"fake chtypes native library bytes for integration tests",
    keyid: str = "test-key",
    seed: bytes | None = None,
    extra_index_manifests: list[dict] | None = None,
) -> BuiltTree:
    """Write ``<tmp_path>/v2/chtypes/v1/…`` and return its digests."""
    root = tmp_path / "v2" / "chtypes" / "v1"
    (root / "manifests").mkdir(parents=True)
    (root / "blobs").mkdir(parents=True)
    (root / "referrers").mkdir(parents=True)

    layer_bytes = _tar_zst(library_name, library_bytes)
    layer_digest = _digest(layer_bytes)
    _write(root / "blobs" / layer_digest, layer_bytes)

    config_descriptor = {
        "mediaType": C.MEDIA_TYPE_EMPTY_CONFIG,
        "digest": EMPTY_CONFIG_DIGEST,
        "size": 2,
    }
    layer_descriptor = {
        "mediaType": C.MEDIA_TYPE_LAYER,
        "digest": layer_digest,
        "size": len(layer_bytes),
    }
    manifest_doc = {
        "schemaVersion": 2,
        "mediaType": C.MEDIA_TYPE_MANIFEST,
        "artifactType": C.ARTIFACT_TYPE,
        "config": config_descriptor,
        "layers": [layer_descriptor],
    }
    manifest_bytes = json.dumps(manifest_doc, sort_keys=True).encode("utf-8")
    manifest_digest = _digest(manifest_bytes)
    _write(root / "manifests" / manifest_digest, manifest_bytes)

    index_manifests = [
        {
            "mediaType": C.MEDIA_TYPE_MANIFEST,
            "digest": manifest_digest,
            "size": len(manifest_bytes),
            "platform": {"os": os_name, "architecture": arch},
        }
    ]
    if extra_index_manifests:
        index_manifests.extend(extra_index_manifests)
    index_doc = {"schemaVersion": 2, "mediaType": C.MEDIA_TYPE_INDEX, "manifests": index_manifests}
    index_bytes = json.dumps(index_doc, sort_keys=True).encode("utf-8")
    index_digest = _digest(index_bytes)
    _write(root / "manifests" / tag, index_bytes)

    if seed is None:
        seed, public_key = generate_keypair()
    else:
        public_key = _public_key_for(seed)

    predicate = {
        "abi": C.ABI_GENERATION,
        "abi_fingerprint": "sha256:" + "a" * 64,
        "clickhouse_version": version,
        "channel": channel,
        "clickhouse_minor": ".".join(version.split(".")[:2]),
        "clickhouse_commit": "b" * 40,
        "os": os_name,
        "arch": arch,
        "build": build,
        "core_commit": "c" * 40,
        "inputs_sha256": "d" * 64,
        "library": library_name,
        "library_sha256": hashlib.sha256(library_bytes).hexdigest(),
        "library_bytes": len(library_bytes),
        "glibc_floor": "2.17" if os_name == "linux" else None,
    }
    if predicate["glibc_floor"] is None:
        del predicate["glibc_floor"]

    bundle = build_bundle(
        seed, keyid, subject_sha256=layer_digest.split(":", 1)[1], predicate=predicate
    )
    bundle_bytes = json.dumps(bundle).encode("utf-8")
    bundle_digest = _digest(bundle_bytes)
    _write(root / "blobs" / bundle_digest, bundle_bytes)

    bundle_layer_descriptor = {
        "mediaType": C.MEDIA_TYPE_BUNDLE,
        "digest": bundle_digest,
        "size": len(bundle_bytes),
    }
    subject_descriptor = {
        "mediaType": C.MEDIA_TYPE_MANIFEST,
        "digest": manifest_digest,
        "size": len(manifest_bytes),
    }
    referrer_manifest_doc = {
        "schemaVersion": 2,
        "mediaType": C.MEDIA_TYPE_MANIFEST,
        "artifactType": C.MEDIA_TYPE_BUNDLE,
        "config": config_descriptor,
        "layers": [bundle_layer_descriptor],
        "subject": subject_descriptor,
    }
    referrer_manifest_bytes = json.dumps(referrer_manifest_doc, sort_keys=True).encode("utf-8")
    referrer_manifest_digest = _digest(referrer_manifest_bytes)
    _write(root / "manifests" / referrer_manifest_digest, referrer_manifest_bytes)

    referrers_index_doc = {
        "schemaVersion": 2,
        "mediaType": C.MEDIA_TYPE_INDEX,
        "manifests": [
            {
                "mediaType": C.MEDIA_TYPE_MANIFEST,
                "digest": referrer_manifest_digest,
                "size": len(referrer_manifest_bytes),
                "artifactType": C.MEDIA_TYPE_BUNDLE,
            }
        ],
    }
    referrers_index_bytes = json.dumps(referrers_index_doc, sort_keys=True).encode("utf-8")
    _write(root / "referrers" / manifest_digest, referrers_index_bytes)
    # The tag-schema fallback (layout-v2 spec §7.2/§10 A4): the same
    # referrers-index SHAPE, served at the `sha256-<hex>` tag.
    hex_only = manifest_digest.split(":", 1)[1]
    _write(root / "manifests" / f"sha256-{hex_only}", referrers_index_bytes)

    return BuiltTree(
        root=root,
        manifest_digest=manifest_digest,
        layer_digest=layer_digest,
        index_digest=index_digest,
        bundle_digest=bundle_digest,
        referrer_manifest_digest=referrer_manifest_digest,
        library_name=library_name,
        library_bytes=library_bytes,
        seed=seed,
        public_key=public_key,
        keyid=keyid,
        predicate=predicate,
    )
