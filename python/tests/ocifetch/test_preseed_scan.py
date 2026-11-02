"""Public issue #690: an offline lookup reads a bounded number of blobs,
whatever the number of builds the cache holds.

The verdicts come from the blob reads the lookup itself made (every
`Path.read_bytes` under the layout's `blobs/sha256/`), never from a timing.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from chtypes._ocifetch._dsse import TrustedKey
from chtypes._ocifetch._ensure import Options, Request, resolve_installed

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "fetch-v1"
LAYOUT = FIXTURES / "layouts" / "offline-hit"
# The offline-hit layout's one build: 26.1.1.1 on linux-arm64.
BUILD_MANIFEST = "sha256:b461315c30649fec6d9cc988330ed3081ffd6a2a45aa21acda6ff212413f8637"
BUILD_LAYER_HEX = "df72d6f06f8ed738ecdd8e080dbd71244b66024daee20c85f53d3f822a49463b"
BUNDLE_REF_HEX = "887b88511be68e82d653c734f57fc0e2c9c2e64bda4f23f25fe0ae1648f6244b"


def _key(name: str) -> TrustedKey:
    public = bytes.fromhex((FIXTURES / name / "public.hex").read_text().strip())
    return TrustedKey(keyid=hashlib.sha256(public).hexdigest()[:16], public_key=public)


def _seed_clones(tmp_path: Path, n: int) -> tuple[Path, list[str]]:
    """The offline-hit layout plus n-1 more pre-seeded entries of the same
    signed build: each its own manifest (the original plus one annotation),
    its own signature referrer manifest naming it, and its own copy of the
    bundle (the original bytes plus trailing whitespace, which the signature
    does not cover). The layer stays the one signed layer."""
    if not LAYOUT.is_dir():
        pytest.skip(f"fetch fixtures not available at {LAYOUT}")
    cache = tmp_path / f"cache-{n}"
    shutil.copytree(LAYOUT, cache)
    blobs = cache / "blobs" / "sha256"

    def put(b: bytes) -> tuple[str, int]:
        h = hashlib.sha256(b).hexdigest()
        (blobs / h).write_bytes(b)
        return "sha256:" + h, len(b)

    index = json.loads((cache / "index.json").read_text())
    first = index["manifests"][0]
    digests = [first["digest"]]
    manifest = json.loads((blobs / BUILD_MANIFEST.removeprefix("sha256:")).read_bytes())
    ref = json.loads((blobs / BUNDLE_REF_HEX).read_bytes())
    bundle_desc = ref["layers"][0]
    bundle = (blobs / bundle_desc["digest"].removeprefix("sha256:")).read_bytes()
    for i in range(1, n):
        manifest["annotations"] = {"test.clone": str(i)}
        md, msize = put(json.dumps(manifest).encode())
        bd, bsize = put(bundle + b" " * i)
        bundle_desc["digest"], bundle_desc["size"] = bd, bsize
        ref["subject"] = {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": md,
            "size": msize,
        }
        put(json.dumps(ref).encode())
        entry = {k: v for k, v in first.items() if k != "annotations"}
        entry["digest"], entry["size"] = md, msize
        index["manifests"].append(entry)
        digests.append(md)
    (cache / "index.json").write_text(json.dumps(index))
    return cache, digests


def _blob_reads(monkeypatch: pytest.MonkeyPatch, cache: Path, fn: Callable[[], object]) -> Counter:
    """Every blob of `cache` read while fn runs, by name."""
    reads: Counter = Counter()
    blobs = cache / "blobs" / "sha256"
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        data = real(self)
        if self.parent == blobs:
            reads[self.name] += 1
        return data

    with monkeypatch.context() as m:
        m.setattr(Path, "read_bytes", counting)
        fn()
    return reads


def _options(cache: Path, key: str) -> Options:
    return Options(cache_dir=cache, system_dirs=(), trusted_keys=(_key(key),))


@pytest.mark.parametrize("n", [2, 20])
def test_lookup_over_unverifiable_entries_reads_no_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    """N pre-seeded entries that no lookup installs (their signature verifies
    under no trusted key), so every lookup meets them again: no layer is
    read, no blob more than twice, and at most 5 reads per entry plus 2, at
    N=2 and N=20 alike. The old scan read every blob of the layout, the layer
    included, once per entry."""
    cache, _ = _seed_clones(tmp_path, n)
    options = _options(cache, "other-key")
    results: list[object] = []
    reads = _blob_reads(
        monkeypatch,
        cache,
        lambda: results.append(resolve_installed(Request("26.1"), "linux-arm64", options)),
    )
    assert results == [None]
    total, most = sum(reads.values()), max(reads.values(), default=0)
    print(f"N={n}: {total} blob reads, {len(reads)} distinct, most {most}, "
          f"layer {reads[BUILD_LAYER_HEX]}")
    assert reads[BUILD_LAYER_HEX] == 0, f"N={n}: the lookup read the layer"
    assert most <= 2, f"N={n}: one blob was read {most} times in one lookup"
    assert total <= 5 * n + 2, f"N={n}: {total} blob reads in one lookup"


@pytest.mark.parametrize("n", [2, 20])
def test_miss_over_installed_builds_reads_no_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    """The consumer's case: every entry is installed, so none is pre-seeded
    (fetch-v1.md §6), and a miss reads no blob at all."""
    cache, digests = _seed_clones(tmp_path, n)
    options = _options(cache, "test-key")
    # The first lookup installs every pre-seeded entry of the build.
    first = resolve_installed(Request("26.1"), "linux-arm64", options)
    assert first is not None
    assert all((cache / "unpacked" / "sha256" / d.removeprefix("sha256:")).is_dir() for d in digests)
    results: list[object] = []
    reads = _blob_reads(
        monkeypatch,
        cache,
        lambda: results.append(resolve_installed(Request("26.3"), "linux-arm64", options)),
    )
    assert results == [None]
    assert sum(reads.values()) == 0, f"N={n}: the miss read {dict(reads)}"


def test_preseeded_install_still_verifies_the_layer(tmp_path: Path) -> None:
    """The install is as strict as before: with the layer's bytes altered,
    nothing is installed."""
    cache, digests = _seed_clones(tmp_path, 20)
    layer = cache / "blobs" / "sha256" / BUILD_LAYER_HEX
    b = bytearray(layer.read_bytes())
    b[-1] ^= 0xFF
    layer.write_bytes(bytes(b))
    assert resolve_installed(Request("26.1"), "linux-arm64", _options(cache, "test-key")) is None
    assert not (cache / "unpacked" / "sha256" / digests[0].removeprefix("sha256:")).exists()
