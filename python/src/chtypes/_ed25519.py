"""Ed25519 signature VERIFICATION, pure Python, verify-only (RFC 8032 §5.1).

The standard library has no ed25519, and this binding is zero-dependency on
purpose, so the release signature over ``SHA256SUMS`` (docs/guides/fetch.md §4) is
checked here with Python integers over the twisted Edwards curve, exactly as
RFC 8032 spells it: point decoding with the sign-bit recovery, extended
coordinates for the group law, and the ``[S]B = R + [k]A`` equation.

Only verification. There is no signing here and never will be: the private
half lives in the core repository's publish step. Verification is not
secret-dependent, so the constant-time concerns of a signer do not apply.

A verifier costs two ~253-bit scalar multiplications — a few milliseconds
per signature, once per fetch. Tested against RFC 8032's own vectors and the
release key's reference vector in ``tests/test_ed25519.py``.
"""

from __future__ import annotations

import hashlib

__all__ = ["verify"]

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, -1, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)  # sqrt(-1) mod p

# The base point B in extended coordinates (X, Y, Z, T) with T = X*Y.
_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_BY = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_B = (_BX, _BY, 1, (_BX * _BY) % _P)
_IDENTITY = (0, 1, 1, 0)

_Point = tuple[int, int, int, int]


def _add(p: _Point, q: _Point) -> _Point:
    """RFC 8032 §5.1.4 — the unified add-2008-hwcd-3 formula; also doubles."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * t1 * t2 * _D) % _P
    d = (2 * z1 * z2) % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P


def _mul(s: int, p: _Point) -> _Point:
    """Scalar multiplication by double-and-add, most significant bit first."""
    acc = _IDENTITY
    for bit in bin(s)[2:]:
        acc = _add(acc, acc)
        if bit == "1":
            acc = _add(acc, p)
    return acc


def _equal(p: _Point, q: _Point) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _decode(raw: bytes) -> _Point | None:
    """RFC 8032 §5.1.3 — 32 little-endian bytes to a point, or None if invalid."""
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    y2 = (y * y) % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    # Candidate root: x = (u/v)^((p+3)/8) = u * v^3 * (u * v^7)^((p-5)/8).
    v3 = (v * v * v) % _P
    x = (u * v3 * pow(u * v3 * v3 * v % _P, (_P - 5) // 8, _P)) % _P
    vx2 = (v * x * x) % _P
    if vx2 == u:
        pass
    elif vx2 == (-u) % _P:
        x = (x * _SQRT_M1) % _P
    else:
        return None  # y is not on the curve
    if x == 0 and sign == 1:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x, y, 1, (x * y) % _P


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether ``signature`` (64 bytes) is a valid ed25519 signature by the
    32-byte raw ``public_key`` over the exact bytes of ``message``.

    Returns False — never raises — for a malformed key, a malformed or
    non-canonical signature (``S >= L``), a point off the curve, or a
    signature that simply does not verify. The check is the textbook
    ``[S]B == R + [k]A`` with ``k = SHA-512(R || A || M) mod L``.
    """
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _decode(public_key)
    r = _decode(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    digest = hashlib.sha512(signature[:32] + public_key + message).digest()
    k = int.from_bytes(digest, "little") % _L
    return _equal(_mul(s, _B), _add(r, _mul(k, a)))
