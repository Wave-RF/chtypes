"""`FetchOptions.trusted_keys` takes hex keys, as every binding's option and
`CHTYPES_TRUSTED_KEYS` do, and derives each key id (public issue #500)."""

from __future__ import annotations

import hashlib

import pytest

import chtypes
from chtypes import FetchOptions, UsageError
from chtypes._ocifetch import _constants as C


def test_a_hex_key_becomes_a_record_whose_key_id_is_derived() -> None:
    key = C.TEST_KEYS[0]
    (record,) = FetchOptions(trusted_keys=(key["ed25519_hex"],))._trusted_keys() or ()
    raw = bytes.fromhex(key["ed25519_hex"])
    assert record.public_key == raw
    assert record.keyid == hashlib.sha256(raw).hexdigest()[:16] == key["keyid"]


@pytest.mark.parametrize("bad", [("zz",), ("ab" * 31,), "ab" * 32])
def test_a_key_that_is_not_64_hex_digits_is_misuse(bad: object) -> None:
    with pytest.raises(UsageError):
        FetchOptions(trusted_keys=bad)._trusted_keys()  # type: ignore[arg-type]


def test_no_trusted_key_record_type_is_public() -> None:
    assert not hasattr(chtypes, "TrustedKey")
    assert FetchOptions()._trusted_keys() is None
