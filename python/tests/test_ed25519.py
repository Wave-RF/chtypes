"""The pure-Python ed25519 verifier, against vectors nobody here chose.

No fixtures, no artifacts: RFC 8032 §7.1's own test vectors, the release
key's reference vector from docs/fetch.md §4, and the negatives that matter
for a verifier (a flipped message byte, a flipped signature byte, a
non-canonical scalar, a point off the curve, wrong lengths).
"""

from __future__ import annotations

import hashlib

import pytest

from chtypes import fetch as fetch_module
from chtypes._ed25519 import verify

# RFC 8032 §7.1 — TEST 1 (empty message), TEST 2 (one byte), TEST 3 (two bytes).
RFC_VECTORS = [
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]

# docs/fetch.md §4: the release key and its reference vector (openssl -rawin).
RELEASE_KEY = bytes.fromhex(fetch_module.RELEASE_PUBLIC_KEY)
RELEASE_MESSAGE = b"hello\n"
RELEASE_SIGNATURE = bytes.fromhex(
    "0fee686f7ed7c64b86a7dce0ffd66b15d1504178153c3b0cc118e2c9456afa6d"
    "3e2e55019eca8f75e44ab507d65b0714523e92c7f92452821930691212e76c04"
)


@pytest.mark.parametrize(("pk", "msg", "sig"), RFC_VECTORS)
def test_rfc8032_vectors_verify(pk: str, msg: str, sig: str) -> None:
    assert verify(bytes.fromhex(pk), bytes.fromhex(msg), bytes.fromhex(sig)) is True


@pytest.mark.parametrize(("pk", "msg", "sig"), RFC_VECTORS)
def test_rfc8032_vectors_reject_a_changed_message(pk: str, msg: str, sig: str) -> None:
    assert verify(bytes.fromhex(pk), bytes.fromhex(msg) + b"\x00", bytes.fromhex(sig)) is False


def test_the_release_key_reference_vector() -> None:
    """The exact vector docs/fetch.md §4 publishes — the same check every SDK
    embeds — and that flipping one byte of the message fails it."""
    assert verify(RELEASE_KEY, RELEASE_MESSAGE, RELEASE_SIGNATURE) is True
    assert verify(RELEASE_KEY, b"hellp\n", RELEASE_SIGNATURE) is False
    assert verify(RELEASE_KEY, b"hello", RELEASE_SIGNATURE) is False


def test_the_embedded_key_id_is_sha256_of_the_raw_key() -> None:
    assert hashlib.sha256(RELEASE_KEY).hexdigest()[:16] == fetch_module.RELEASE_KEY_ID
    assert fetch_module.key_id(RELEASE_KEY) == "deb275922dbff76e"


def test_every_bit_of_the_signature_matters() -> None:
    # Flipping any of a sample of bits in R or S must fail; a verifier that
    # ignored part of the signature would pass some of these.
    for byte in (0, 7, 31, 32, 40, 63):
        for bit in (0, 1, 7):
            mutated = bytearray(RELEASE_SIGNATURE)
            mutated[byte] ^= 1 << bit
            assert verify(RELEASE_KEY, RELEASE_MESSAGE, bytes(mutated)) is False, (byte, bit)


def test_non_canonical_and_malformed_inputs_are_refused_not_raised() -> None:
    order = 2**252 + 27742317777372353535851937790883648493
    r = RELEASE_SIGNATURE[:32]
    s = int.from_bytes(RELEASE_SIGNATURE[32:], "little")
    # S + L encodes the same scalar mod L; RFC 8032 requires S < L, and
    # accepting the alias would make signatures malleable.
    assert verify(RELEASE_KEY, RELEASE_MESSAGE, r + (s + order).to_bytes(32, "little")) is False
    # Wrong lengths.
    assert verify(RELEASE_KEY, RELEASE_MESSAGE, RELEASE_SIGNATURE[:63]) is False
    assert verify(RELEASE_KEY[:31], RELEASE_MESSAGE, RELEASE_SIGNATURE) is False
    assert verify(b"", b"", b"") is False
    # y >= p is not a point; neither is a y with no square root for x.
    assert verify(b"\xff" * 32, RELEASE_MESSAGE, RELEASE_SIGNATURE) is False
    assert verify(RELEASE_KEY, RELEASE_MESSAGE, b"\xff" * 32 + RELEASE_SIGNATURE[32:]) is False
    # An all-zero key decodes (y = 0 is on the curve with x = ±sqrt(-1)) but
    # signs nothing.
    assert verify(b"\x00" * 32, RELEASE_MESSAGE, RELEASE_SIGNATURE) is False
