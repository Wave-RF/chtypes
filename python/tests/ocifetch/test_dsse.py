"""DSSE PAE encoding and key-only Sigstore bundle verification."""

from __future__ import annotations

import base64
import json

import pytest

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import (
    TrustedKey,
    loads_no_duplicate_keys,
    pae_encode,
    verify_bundle,
)
from chtypes._ocifetch._errors import ArtifactCorruptError

from ._bundle_support import DEFAULT_PREDICATE, build_bundle, build_statement, sha256_hex
from ._sign_support import generate_keypair


def test_pae_encode_matches_dsse_spec_example() -> None:
    # secure-systems-lab/dsse's own worked example.
    assert pae_encode("http://example.com/HelloWorld", b"hello world") == (
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    )


def test_pae_encode_empty_body() -> None:
    assert pae_encode("t", b"") == b"DSSEv1 1 t 0 "


def test_loads_no_duplicate_keys_accepts_normal_json() -> None:
    doc = loads_no_duplicate_keys(b'{"a": 1, "b": {"c": 2}}')
    assert doc == {"a": 1, "b": {"c": 2}}


def test_loads_no_duplicate_keys_rejects_top_level_duplicate() -> None:
    with pytest.raises(ArtifactCorruptError, match="duplicate"):
        loads_no_duplicate_keys(b'{"a": 1, "a": 2}')


def test_loads_no_duplicate_keys_rejects_nested_duplicate() -> None:
    with pytest.raises(ArtifactCorruptError, match="duplicate"):
        loads_no_duplicate_keys(b'{"predicate": {"abi": 1, "abi": 2}}')


def test_loads_no_duplicate_keys_rejects_bad_utf8() -> None:
    with pytest.raises(ArtifactCorruptError):
        loads_no_duplicate_keys(b"\xff\xfe")


def test_loads_no_duplicate_keys_rejects_bad_json() -> None:
    with pytest.raises(ArtifactCorruptError):
        loads_no_duplicate_keys(b"{not json")


def _trusted(public_key: bytes, keyid: str = "test-key") -> tuple[TrustedKey, ...]:
    return (TrustedKey(keyid=keyid, public_key=public_key),)


def test_verify_bundle_accepts_a_genuine_signature() -> None:
    seed, pub = generate_keypair()
    subject = sha256_hex(b"layer bytes")
    bundle = build_bundle(seed, "test-key", subject_sha256=subject)
    result = verify_bundle(bundle, _trusted(pub, "test-key"))
    assert result is not None
    assert result.signed_by == "test-key"
    assert result.statement.predicate == DEFAULT_PREDICATE
    assert result.statement.subject_sha256 == (subject,)
    assert result.statement.predicate_type == C.PREDICATE_TYPE_ARTIFACT


def test_verify_bundle_rejects_wrong_key() -> None:
    seed, _pub = generate_keypair()
    _other_seed, other_pub = generate_keypair()
    bundle = build_bundle(seed, "test-key", subject_sha256=sha256_hex(b"x"))
    assert verify_bundle(bundle, _trusted(other_pub)) is None


def test_verify_bundle_ignores_the_hint_and_trusts_by_real_verification() -> None:
    """layout-v2 spec §4.2/§4.3: the hint is never consulted for trust. A
    bundle naming an unrelated/unknown hint still verifies when its actual
    signature checks out under a trusted key (PLAN "hint-names-unknown-key-
    but-valid")."""
    seed, pub = generate_keypair()
    bundle = build_bundle(seed, "some-unknown-hint-nobody-trusts", subject_sha256=sha256_hex(b"x"))
    result = verify_bundle(bundle, _trusted(pub, "the-actual-trusted-keyid"))
    assert result is not None
    assert result.signed_by == "the-actual-trusted-keyid"


def test_verify_bundle_tampered_signature_is_untrusted() -> None:
    seed, pub = generate_keypair()
    bundle = build_bundle(seed, "test-key", subject_sha256=sha256_hex(b"x"))
    sig = base64.b64decode(bundle["dsseEnvelope"]["signatures"][0]["sig"])
    tampered = bytes([sig[0] ^ 0xFF]) + sig[1:]
    bundle["dsseEnvelope"]["signatures"][0]["sig"] = base64.b64encode(tampered).decode("ascii")
    assert verify_bundle(bundle, _trusted(pub)) is None


def test_verify_bundle_tampered_payload_is_untrusted() -> None:
    seed, pub = generate_keypair()
    bundle = build_bundle(seed, "test-key", subject_sha256=sha256_hex(b"x"))
    payload = bytearray(base64.b64decode(bundle["dsseEnvelope"]["payload"]))
    payload[0] ^= 0xFF
    bundle["dsseEnvelope"]["payload"] = base64.b64encode(bytes(payload)).decode("ascii")
    assert verify_bundle(bundle, _trusted(pub)) is None


def test_verify_bundle_multiple_signatures_one_valid() -> None:
    """A referrer manifest MUST never carry two signatures in one envelope
    per the spec (a rotation adds a second bundle instead), but the verifier
    still tries every signature DSSE allows rather than assuming exactly
    one, in case a mirror re-packages a bundle."""
    seed, pub = generate_keypair()
    _bad_seed, bad_pub = generate_keypair()
    statement = build_statement(subject_sha256=sha256_hex(b"x"))
    payload = json.dumps(statement).encode("utf-8")
    from chtypes._ocifetch._dsse import pae_encode as _pae

    from ._sign_support import sign as _sign

    good_pae = _pae(C.DSSE_PAYLOAD_TYPE, payload)
    bad_sig = _sign(_bad_seed, b"wrong message entirely")
    bundle = {
        "mediaType": C.MEDIA_TYPE_BUNDLE,
        "verificationMaterial": {"publicKey": {"hint": "whatever"}},
        "dsseEnvelope": {
            "payload": base64.b64encode(payload).decode("ascii"),
            "payloadType": C.DSSE_PAYLOAD_TYPE,
            "signatures": [
                {"sig": base64.b64encode(bad_sig).decode("ascii"), "keyid": "bad"},
                {"sig": base64.b64encode(_sign(seed, good_pae)).decode("ascii"), "keyid": "good"},
            ],
        },
    }
    result = verify_bundle(bundle, _trusted(pub, "good"))
    assert result is not None


def test_verify_bundle_too_many_signatures_raises_corrupt() -> None:
    seed, pub = generate_keypair()
    bundle = build_bundle(seed, "test-key", subject_sha256=sha256_hex(b"x"))
    one_sig = bundle["dsseEnvelope"]["signatures"][0]
    bundle["dsseEnvelope"]["signatures"] = [one_sig] * (C.DSSE_MAX_SIGNATURES + 1)
    with pytest.raises(ArtifactCorruptError, match="signatures"):
        verify_bundle(bundle, _trusted(pub))


def test_verify_bundle_wrong_payload_type_raises_corrupt() -> None:
    seed, pub = generate_keypair()
    bundle = build_bundle(seed, "test-key", subject_sha256=sha256_hex(b"x"))
    bundle["dsseEnvelope"]["payloadType"] = "application/something-else"
    with pytest.raises(ArtifactCorruptError, match="payloadType"):
        verify_bundle(bundle, _trusted(pub))


def test_verify_bundle_no_envelope_raises_corrupt() -> None:
    with pytest.raises(ArtifactCorruptError, match="dsseEnvelope"):
        verify_bundle({"mediaType": C.MEDIA_TYPE_BUNDLE}, ())


def test_verify_bundle_duplicate_statement_key_raises_corrupt() -> None:
    seed, pub = generate_keypair()
    payload = b'{"_type": "x", "_type": "y"}'
    pae = pae_encode(C.DSSE_PAYLOAD_TYPE, payload)
    from ._sign_support import sign as _sign

    sig = _sign(seed, pae)
    bundle = {
        "mediaType": C.MEDIA_TYPE_BUNDLE,
        "verificationMaterial": {"publicKey": {"hint": "k"}},
        "dsseEnvelope": {
            "payload": base64.b64encode(payload).decode("ascii"),
            "payloadType": C.DSSE_PAYLOAD_TYPE,
            "signatures": [{"sig": base64.b64encode(sig).decode("ascii"), "keyid": "k"}],
        },
    }
    with pytest.raises(ArtifactCorruptError, match="duplicate"):
        verify_bundle(bundle, _trusted(pub, "k"))


def test_verify_bundle_subject_not_layer() -> None:
    """The caller (not `_dsse`) is responsible for checking the subject
    against the actual layer digest; this test only confirms the subject
    comes through for that check to use."""
    seed, pub = generate_keypair()
    bundle = build_bundle(seed, "test-key", subject_sha256="f" * 64)
    result = verify_bundle(bundle, _trusted(pub, "test-key"))
    assert result is not None
    assert result.statement.subject_sha256 == ("f" * 64,)
