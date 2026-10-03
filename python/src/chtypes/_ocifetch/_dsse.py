"""DSSE + in-toto statement verification for a key-only Sigstore bundle v0.3.

layout-v2 spec §4.2/§4.3: sigstore-python cannot verify a certificate-less
bundle, so each SDK implements the small verifier itself — a DSSE PAE
encoding plus one ed25519 check, reusing the primitive the binding already
ships. Here that primitive is `chtypes._ed25519` (imported, never copied).

Trust is a brute-force check against every trusted key's raw bytes, not a
lookup by the envelope's own `keyid`/`publicKey.hint`: those are hints a
bundle author chooses, and whether the producer's hint derivation matches
this SDK's `KEYID_ALGORITHM` is still an open question on the delivery side
(layout-v2 spec §6 A15). A bundle that *names* an unrecognized hint but
verifies under a key we do trust is still trusted (the "hint-names-unknown-
key-but-valid" case) — the hint is never consulted for the trust decision,
only reported back as `signed_by` once a real verification succeeds.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from chtypes import _ed25519
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._errors import ArtifactCorruptError

__all__ = [
    "Statement",
    "TrustedKey",
    "VerifiedBundle",
    "loads_no_duplicate_keys",
    "pae_encode",
    "release_trusted_keys",
    "verify_bundle",
]


@dataclass(frozen=True)
class TrustedKey:
    keyid: str
    public_key: bytes  # raw 32 bytes


@dataclass(frozen=True)
class Statement:
    statement_type: str
    predicate_type: str
    subject_sha256: tuple[str, ...]
    predicate: dict


@dataclass(frozen=True)
class VerifiedBundle:
    statement: Statement
    signed_by: str  # the TrustedKey.keyid that verified, never the bundle's own hint
    payload: bytes = b""  # the authenticated DSSE payload: the exact bytes the signature covers


def _hex_to_bytes(s: str) -> bytes:
    b = bytes.fromhex(s)
    if len(b) != 32:
        raise ValueError(f"expected a 32-byte ed25519 key, got {len(b)} bytes")
    return b


def release_trusted_keys() -> tuple[TrustedKey, ...]:
    """The default trust list: the release key(s) from the generated
    constants, and nothing else. A `test_keys` entry is never in here —
    `v1-constants` asserts that in the source JSON (PLAN §3.1)."""
    return tuple(
        TrustedKey(keyid=k["keyid"], public_key=_hex_to_bytes(k["ed25519_hex"]))
        for k in C.RELEASE_KEYS
    )


def fixture_trusted_keys() -> tuple[TrustedKey, ...]:
    """The fixture test key(s) — trusted ONLY when a conformance case opts in
    (`request.trust == "test"`); never part of the default trust list."""
    return tuple(
        TrustedKey(keyid=k["keyid"], public_key=_hex_to_bytes(k["ed25519_hex"]))
        for k in C.TEST_KEYS
    )


def pae_encode(payload_type: str, payload: bytes) -> bytes:
    """DSSE's Pre-Authentication Encoding (secure-systems-lab/dsse):
    `"DSSEv1" SP LEN(type) SP type SP LEN(body) SP body`, all as raw bytes
    with a single ASCII space as separator, lengths spelled in decimal."""
    type_bytes = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(type_bytes)).encode("ascii")
        + b" "
        + type_bytes
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: set[str] = set()
    out: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ArtifactCorruptError(f"duplicate JSON key {key!r} in a signed statement")
        seen.add(key)
        out[key] = value
    return out


def loads_no_duplicate_keys(data: bytes) -> dict:
    """`json.loads` that refuses a duplicate key anywhere in the document
    (recursively, since `object_pairs_hook` runs for every nested object).
    PLAN "Lane Python": the predicate's own fields are covered by the same
    pass, because `predicate` is a nested object of the statement."""
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except UnicodeDecodeError as e:
        raise ArtifactCorruptError(f"a signed statement was not valid UTF-8: {e}") from e
    except json.JSONDecodeError as e:
        raise ArtifactCorruptError(f"a signed statement was not valid JSON: {e}") from e


def _parse_statement(payload: bytes) -> Statement:
    doc = loads_no_duplicate_keys(payload)
    if not isinstance(doc, dict):
        raise ArtifactCorruptError("a signed statement's payload was not a JSON object")
    statement_type = doc.get("_type")
    predicate_type = doc.get("predicateType")
    subject = doc.get("subject")
    predicate = doc.get("predicate")
    if statement_type != C.STATEMENT_TYPE:
        raise ArtifactCorruptError(
            f"a signed statement's _type was {statement_type!r}, want {C.STATEMENT_TYPE!r}"
        )
    if not isinstance(subject, list) or not subject:
        raise ArtifactCorruptError("a signed statement's subject was missing or empty")
    digests = []
    for entry in subject:
        if not isinstance(entry, dict) or "sha256" not in entry.get("digest", {}):
            raise ArtifactCorruptError("a signed statement's subject entry had no sha256 digest")
        digests.append(entry["digest"]["sha256"])
    if not isinstance(predicate_type, str):
        raise ArtifactCorruptError("a signed statement had no string predicateType")
    if not isinstance(predicate, dict):
        raise ArtifactCorruptError("a signed statement had no predicate object")
    return Statement(
        statement_type=statement_type,
        predicate_type=predicate_type,
        subject_sha256=tuple(digests),
        predicate=predicate,
    )


def verify_bundle(bundle: dict, trusted_keys: tuple[TrustedKey, ...]) -> VerifiedBundle | None:
    """Verify a Sigstore bundle v0.3 JSON document against `trusted_keys`.

    Returns `None` — never raises for an untrusted or malformed signature —
    when no signature in the envelope verifies under any trusted key, so a
    caller can try the next referrer bundle (a key rotation attaches a
    SECOND bundle, never a second signature in one envelope, but this still
    checks every signature DSSE allows in case a mirror re-packs one). A
    structurally broken bundle (bad base64, no envelope, too many
    signatures) raises `ArtifactCorruptError` directly, since that is never
    "try the next bundle" territory — it is the same bundle either way.
    """
    envelope = bundle.get("dsseEnvelope")
    if not isinstance(envelope, dict):
        raise ArtifactCorruptError("a signature bundle had no dsseEnvelope")
    payload_type = envelope.get("payloadType")
    payload_b64 = envelope.get("payload")
    signatures = envelope.get("signatures")
    if payload_type != C.DSSE_PAYLOAD_TYPE:
        raise ArtifactCorruptError(
            f"a signature bundle's payloadType was {payload_type!r}, want {C.DSSE_PAYLOAD_TYPE!r}"
        )
    if not isinstance(payload_b64, str):
        raise ArtifactCorruptError("a signature bundle's DSSE payload was not a string")
    if not isinstance(signatures, list) or not signatures:
        raise ArtifactCorruptError("a signature bundle's DSSE envelope had no signatures")
    if len(signatures) > C.DSSE_MAX_SIGNATURES:
        raise ArtifactCorruptError(
            f"a signature bundle carried {len(signatures)} signatures, "
            f"more than the {C.DSSE_MAX_SIGNATURES} this fetcher accepts"
        )
    try:
        payload = base64.b64decode(payload_b64, validate=True)
    except (ValueError, Exception) as e:  # binascii.Error is a ValueError subclass
        raise ArtifactCorruptError(f"a signature bundle's DSSE payload was not base64: {e}") from e

    pae = pae_encode(payload_type, payload)
    for sig_entry in signatures:
        if not isinstance(sig_entry, dict):
            continue
        sig_b64 = sig_entry.get("sig")
        if not isinstance(sig_b64, str):
            continue
        try:
            sig = base64.b64decode(sig_b64, validate=True)
        except ValueError:
            continue
        for key in trusted_keys:
            if _ed25519.verify(key.public_key, pae, sig):
                statement = _parse_statement(payload)
                return VerifiedBundle(statement=statement, signed_by=key.keyid, payload=payload)
    return None
