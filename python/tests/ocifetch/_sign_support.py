"""A test-only Ed25519 SIGNER (RFC 8032 §5.1.6), used only to build signed
fixtures for this test suite.

`chtypes._ed25519` is verify-only, on purpose, forever (production code never
signs; the private half of any real key is held offline). This module is the
other half of RFC 8032's arithmetic, reusing the curve operations
`chtypes._ed25519` already implements (`_add`, `_mul`, the base point `_B`,
the group order `_L`) rather than re-deriving them, so these tests are
signing with the exact same curve the binding verifies against — not a
second, independently-sourced implementation that could disagree with it by
coincidence.
"""

from __future__ import annotations

import hashlib
import os

from chtypes._ed25519 import _B, _L, _P, _mul, verify

__all__ = ["generate_keypair", "sign"]


def _encode_point(point: tuple[int, int, int, int]) -> bytes:
    x, y, z, _t = point
    z_inv = pow(z, -1, _P)
    x = (x * z_inv) % _P
    y = (y * z_inv) % _P
    out = bytearray(y.to_bytes(32, "little"))
    if x & 1:
        out[31] |= 0x80
    return bytes(out)


def _clamp(seed_hash_first_half: bytes) -> int:
    a = bytearray(seed_hash_first_half)
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(bytes(a), "little")


def _public_key_for(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = _clamp(h[:32])
    return _encode_point(_mul(a, _B))


def generate_keypair() -> tuple[bytes, bytes]:
    """Returns ``(seed, public_key)``, 32 raw bytes each."""
    seed = os.urandom(32)
    return seed, _public_key_for(seed)


def sign(seed: bytes, message: bytes) -> bytes:
    """The 64-byte ed25519 signature of ``message`` under ``seed``.

    Self-checks with `chtypes._ed25519.verify` before returning, so a bug
    here fails the test that calls it rather than silently producing a bundle
    that would pass for the wrong reason.
    """
    h = hashlib.sha512(seed).digest()
    a = _clamp(h[:32])
    prefix = h[32:]
    public_key = _encode_point(_mul(a, _B))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    r_point_enc = _encode_point(_mul(r, _B))
    k = int.from_bytes(hashlib.sha512(r_point_enc + public_key + message).digest(), "little") % _L
    s = (r + k * a) % _L
    signature = r_point_enc + s.to_bytes(32, "little")
    assert verify(public_key, message, signature), "internal: self-check of the test signer failed"
    return signature
