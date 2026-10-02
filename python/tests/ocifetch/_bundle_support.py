"""Build a Sigstore bundle v0.3 JSON document signed with a test key, for
unit tests that do not need the fixtures tree (which lane 0B owns)."""

from __future__ import annotations

import base64
import hashlib
import json

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import pae_encode

from ._sign_support import sign

__all__ = ["DEFAULT_PREDICATE", "build_bundle", "build_statement"]

DEFAULT_PREDICATE = {
    "abi": 1,
    "abi_fingerprint": "sha256:" + "a" * 64,
    "clickhouse_version": "26.8.15.10",
    "channel": "lts",
    "clickhouse_minor": "26.8",
    "clickhouse_commit": "b" * 40,
    "os": "linux",
    "arch": "arm64",
    "build": "20261001.183455",
    "core_commit": "c" * 40,
    "inputs_sha256": "d" * 64,
    "library": "libchtypes.so",
    "library_sha256": "e" * 64,
    "library_bytes": 241000,
    "glibc_floor": "2.17",
}


def build_statement(
    *,
    subject_sha256: str,
    predicate: dict | None = None,
    predicate_type: str = C.PREDICATE_TYPE_ARTIFACT,
) -> dict:
    return {
        "_type": C.STATEMENT_TYPE,
        "subject": [{"name": "artifact", "digest": {"sha256": subject_sha256}}],
        "predicateType": predicate_type,
        "predicate": predicate if predicate is not None else DEFAULT_PREDICATE,
    }


def build_bundle(
    seed: bytes,
    keyid: str,
    *,
    subject_sha256: str,
    predicate: dict | None = None,
    predicate_type: str = C.PREDICATE_TYPE_ARTIFACT,
    statement: dict | None = None,
    extra_signatures: list[dict] | None = None,
) -> dict:
    """A minimal key-only Sigstore bundle v0.3 document (layout-v2 spec §4.2):
    no certificate, no transparency log entry, just ``{"publicKey": {"hint":
    keyid}}`` and a DSSE envelope."""
    doc = statement if statement is not None else build_statement(
        subject_sha256=subject_sha256, predicate=predicate, predicate_type=predicate_type
    )
    payload = json.dumps(doc).encode("utf-8")
    pae = pae_encode(C.DSSE_PAYLOAD_TYPE, payload)
    sig = sign(seed, pae)
    signatures = [{"sig": base64.b64encode(sig).decode("ascii"), "keyid": keyid}]
    if extra_signatures:
        signatures.extend(extra_signatures)
    return {
        "mediaType": C.MEDIA_TYPE_BUNDLE,
        "verificationMaterial": {"publicKey": {"hint": keyid}},
        "dsseEnvelope": {
            "payload": base64.b64encode(payload).decode("ascii"),
            "payloadType": C.DSSE_PAYLOAD_TYPE,
            "signatures": signatures,
        },
    }


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
