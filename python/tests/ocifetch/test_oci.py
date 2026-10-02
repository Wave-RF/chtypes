"""Index/manifest resolution and digest bookkeeping (`_oci.py`)."""

from __future__ import annotations

import hashlib

import pytest

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._errors import (
    ArtifactCorruptError,
    ArtifactUnpublishedError,
    SourceIncompatibleError,
)
from chtypes._ocifetch._oci import (
    Descriptor,
    manifest_config_descriptor,
    manifest_layer_descriptor,
    manifest_single_layer,
    parse_digest,
    resolve_platform_manifest,
    spelling_components,
    validate_spelling,
    verify_body_matches_digest,
    verify_json_descriptor,
    version_within_request,
)


@pytest.mark.parametrize("spelling", ["26.8", "26.8.15", "26.8.15.10", "0.0", "1.2.3.4"])
def test_validate_spelling_accepts_v1_shapes(spelling: str) -> None:
    validate_spelling(spelling)  # does not raise


@pytest.mark.parametrize(
    "spelling",
    ["v26.8", "26.8.15.10-lts", "26.8-stable", "26.8.15.10-b20261001", "latest", "", "26.08"],
)
def test_validate_spelling_refuses_v0_and_decorated_shapes(spelling: str) -> None:
    with pytest.raises(ValueError, match="v1 version spelling"):
        validate_spelling(spelling)


def test_spelling_components() -> None:
    assert spelling_components("26.8.15.10") == (26, 8, 15, 10)
    assert spelling_components("26.8") == (26, 8)


@pytest.mark.parametrize(
    ("predicate_version", "requested", "expected"),
    [
        ("26.8.15.10", "26.8", True),
        ("26.8.15.10", "26.8.15", True),
        ("26.8.15.10", "26.8.15.10", True),
        ("26.8.15.10", "26.7", False),
        ("26.8.15.10", "26.8.16", False),
        ("26.8.15.10", "26.8.15.11", False),
        ("26.9.1.1", "26.8", False),
    ],
)
def test_version_within_request(predicate_version: str, requested: str, expected: bool) -> None:
    assert version_within_request(predicate_version, requested) is expected


def test_parse_digest_accepts_well_formed() -> None:
    hexpart = "a" * 64
    assert parse_digest(f"sha256:{hexpart}") == hexpart


@pytest.mark.parametrize("bad", ["sha256:abc", "md5:" + "a" * 32, "a" * 64, "sha256:" + "g" * 64])
def test_parse_digest_rejects_malformed(bad: str) -> None:
    with pytest.raises(ArtifactCorruptError):
        parse_digest(bad)


def _index(manifests: list[dict]) -> dict:
    return {"schemaVersion": 2, "mediaType": C.MEDIA_TYPE_INDEX, "manifests": manifests}


def _manifest_entry(os_: str, arch: str, digest: str = "sha256:" + "a" * 64) -> dict:
    return {
        "mediaType": C.MEDIA_TYPE_MANIFEST,
        "digest": digest,
        "size": 123,
        "platform": {"os": os_, "architecture": arch},
    }


def test_resolve_platform_manifest_picks_the_matching_platform() -> None:
    index = _index(
        [
            _manifest_entry("linux", "amd64", "sha256:" + "a" * 64),
            _manifest_entry("linux", "arm64", "sha256:" + "b" * 64),
            _manifest_entry("darwin", "arm64", "sha256:" + "c" * 64),
        ]
    )
    desc = resolve_platform_manifest(index, "linux-arm64")
    assert desc.digest == "sha256:" + "b" * 64


def test_resolve_platform_manifest_missing_platform_is_unpublished() -> None:
    index = _index([_manifest_entry("linux", "amd64")])
    with pytest.raises(ArtifactUnpublishedError, match="linux-amd64"):
        resolve_platform_manifest(index, "darwin-arm64")


def test_resolve_platform_manifest_duplicate_platform_is_corrupt() -> None:
    index = _index(
        [
            _manifest_entry("linux", "arm64", "sha256:" + "a" * 64),
            _manifest_entry("linux", "arm64", "sha256:" + "b" * 64),
        ]
    )
    with pytest.raises(ArtifactCorruptError, match="2 manifests"):
        resolve_platform_manifest(index, "linux-arm64")


def test_resolve_platform_manifest_unknown_media_type_is_incompatible() -> None:
    index = {"mediaType": "application/vnd.something.else", "manifests": []}
    with pytest.raises(SourceIncompatibleError):
        resolve_platform_manifest(index, "linux-arm64")


def test_resolve_platform_manifest_rejects_unknown_platform_key() -> None:
    with pytest.raises(ValueError, match="unknown platform"):
        resolve_platform_manifest(_index([]), "windows-amd64")


def test_manifest_layer_descriptor_happy_path() -> None:
    layer_digest = "sha256:" + "d" * 64
    manifest = {
        "mediaType": C.MEDIA_TYPE_MANIFEST,
        "layers": [{"mediaType": C.MEDIA_TYPE_LAYER, "digest": layer_digest, "size": 42}],
    }
    desc = manifest_layer_descriptor(manifest)
    assert desc.digest == layer_digest
    assert desc.size == 42


def test_manifest_layer_descriptor_wrong_count_is_corrupt() -> None:
    manifest = {"mediaType": C.MEDIA_TYPE_MANIFEST, "layers": []}
    with pytest.raises(ArtifactCorruptError, match="layers"):
        manifest_layer_descriptor(manifest)


def test_manifest_layer_descriptor_wrong_media_type_is_incompatible() -> None:
    layer = {"mediaType": "application/octet-stream", "digest": "sha256:" + "a" * 64, "size": 1}
    manifest = {"mediaType": C.MEDIA_TYPE_MANIFEST, "layers": [layer]}
    with pytest.raises(SourceIncompatibleError):
        manifest_layer_descriptor(manifest)


def test_manifest_single_layer_accepts_any_media_type_when_unconstrained() -> None:
    manifest = {
        "mediaType": C.MEDIA_TYPE_MANIFEST,
        "layers": [{"mediaType": C.MEDIA_TYPE_BUNDLE, "digest": "sha256:" + "a" * 64, "size": 1}],
    }
    desc = manifest_single_layer(manifest)
    assert desc.media_type == C.MEDIA_TYPE_BUNDLE


def test_manifest_config_descriptor_none_for_empty_config() -> None:
    config = {"mediaType": C.MEDIA_TYPE_EMPTY_CONFIG, "digest": "sha256:" + "a" * 64, "size": 2}
    assert manifest_config_descriptor({"config": config}) is None


def test_manifest_config_descriptor_present() -> None:
    config = {"mediaType": C.MEDIA_TYPE_CONFIG, "digest": "sha256:" + "a" * 64, "size": 99}
    desc = manifest_config_descriptor({"config": config})
    assert desc is not None
    assert desc.size == 99


def test_verify_json_descriptor_detects_size_mismatch() -> None:
    body = b"hello"
    desc = Descriptor(media_type="x", digest=f"sha256:{hashlib.sha256(body).hexdigest()}", size=999)
    with pytest.raises(ArtifactCorruptError, match="size"):
        verify_json_descriptor(body, desc, what="test")


def test_verify_json_descriptor_detects_hash_mismatch() -> None:
    body = b"hello"
    desc = Descriptor(media_type="x", digest="sha256:" + "0" * 64, size=len(body))
    with pytest.raises(ArtifactCorruptError, match="sha256"):
        verify_json_descriptor(body, desc, what="test")


def test_verify_json_descriptor_accepts_matching_body() -> None:
    body = b"hello"
    digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
    desc = Descriptor(media_type="x", digest=digest, size=len(body))
    verify_json_descriptor(body, desc, what="test")  # does not raise


def test_verify_body_matches_digest() -> None:
    body = b"abc"
    verify_body_matches_digest(body, f"sha256:{hashlib.sha256(body).hexdigest()}", what="test")
    with pytest.raises(ArtifactCorruptError):
        verify_body_matches_digest(body, "sha256:" + "0" * 64, what="test")
