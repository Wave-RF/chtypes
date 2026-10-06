"""Package the stub library as a signed OCI layout (a `file://` route tree) under
the SDK test key, so a registry test drives the REAL fetch derivation: tag,
index, platform manifest, layer, referrer bundle, signature, unpack, and
`resolve_installed` handing the loader the predicate the signature covered.

The tree's shape is the one python/tests/ocifetch/_registry_support.py writes;
this differs only in carrying the stub's own build_info as the signed predicate,
so loader step 5 (build_info against the predicate) can pass.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import TrustedKey, fixture_trusted_keys
from ocifetch._bundle_support import build_bundle
from ocifetch._registry_support import EMPTY_CONFIG_DIGEST, _digest, _tar_zst, _write

TEST_KEY_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "fetch-v1" / "test-key"


def test_key() -> tuple[bytes, TrustedKey]:
    """The fixtures' ed25519 test key: its raw seed (the last 32 bytes of the
    PKCS#8 DER) and the TrustedKey a test opts into trusting."""
    pem = (TEST_KEY_DIR / "private.pem").read_text()
    der = base64.b64decode("".join(line for line in pem.splitlines() if not line.startswith("-")))
    (trusted,) = fixture_trusted_keys()
    assert trusted.public_key.hex() == (TEST_KEY_DIR / "public.hex").read_text().strip()
    return der[-32:], trusted


@dataclass(frozen=True)
class StubTree:
    base_url: str
    predicate: dict
    trusted: TrustedKey
    library_bytes: bytes


def build_stub_tree(root: Path, stub_path: str, stub_predicate: dict, tag: str) -> StubTree:
    seed, trusted = test_key()
    library_bytes = Path(stub_path).read_bytes()
    name = "libchtypes.so"
    repo = root / "v2" / "chtypes" / "v1"
    for sub in ("manifests", "blobs", "referrers"):
        (repo / sub).mkdir(parents=True)

    layer = _tar_zst(name, library_bytes)
    layer_digest = _digest(layer)
    _write(repo / "blobs" / layer_digest, layer)
    config = {"mediaType": C.MEDIA_TYPE_EMPTY_CONFIG, "digest": EMPTY_CONFIG_DIGEST, "size": 2}
    manifest = {
        "schemaVersion": 2,
        "mediaType": C.MEDIA_TYPE_MANIFEST,
        "artifactType": C.ARTIFACT_TYPE,
        "config": config,
        "layers": [{"mediaType": C.MEDIA_TYPE_LAYER, "digest": layer_digest, "size": len(layer)}],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    manifest_digest = _digest(manifest_bytes)
    _write(repo / "manifests" / manifest_digest, manifest_bytes)
    index = {
        "schemaVersion": 2,
        "mediaType": C.MEDIA_TYPE_INDEX,
        "manifests": [
            {
                "mediaType": C.MEDIA_TYPE_MANIFEST,
                "digest": manifest_digest,
                "size": len(manifest_bytes),
                "platform": {
                    "os": stub_predicate["os"],
                    "architecture": stub_predicate["arch"],
                },
            }
        ],
    }
    _write(repo / "manifests" / tag, json.dumps(index, sort_keys=True).encode())

    predicate = dict(
        stub_predicate,
        library=name,
        library_sha256=hashlib.sha256(library_bytes).hexdigest(),
        library_bytes=len(library_bytes),
    )
    bundle = build_bundle(
        seed,
        trusted.keyid,
        subject_sha256=layer_digest.split(":", 1)[1],
        predicate=predicate,
    )
    bundle_bytes = json.dumps(bundle).encode()
    bundle_digest = _digest(bundle_bytes)
    _write(repo / "blobs" / bundle_digest, bundle_bytes)
    referrer = {
        "schemaVersion": 2,
        "mediaType": C.MEDIA_TYPE_MANIFEST,
        "artifactType": C.MEDIA_TYPE_BUNDLE,
        "config": config,
        "layers": [
            {"mediaType": C.MEDIA_TYPE_BUNDLE, "digest": bundle_digest, "size": len(bundle_bytes)}
        ],
        "subject": {
            "mediaType": C.MEDIA_TYPE_MANIFEST,
            "digest": manifest_digest,
            "size": len(manifest_bytes),
        },
    }
    referrer_bytes = json.dumps(referrer, sort_keys=True).encode()
    referrer_digest = _digest(referrer_bytes)
    _write(repo / "manifests" / referrer_digest, referrer_bytes)
    referrers = {
        "schemaVersion": 2,
        "mediaType": C.MEDIA_TYPE_INDEX,
        "manifests": [
            {
                "mediaType": C.MEDIA_TYPE_MANIFEST,
                "digest": referrer_digest,
                "size": len(referrer_bytes),
                "artifactType": C.MEDIA_TYPE_BUNDLE,
            }
        ],
    }
    referrers_bytes = json.dumps(referrers, sort_keys=True).encode()
    _write(repo / "referrers" / manifest_digest, referrers_bytes)
    _write(repo / "manifests" / f"sha256-{manifest_digest.split(':', 1)[1]}", referrers_bytes)
    return StubTree(
        base_url=f"file://{repo}", predicate=predicate, trusted=trusted, library_bytes=library_bytes
    )
