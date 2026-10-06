"""The ABI identity compiled into the layer the public API actually binds.

It prints one line, `chtypes_abi_identity binding=python abi=<n> fingerprint=<fp>`,
read from the generated declarations `chtypes.library` and the loader imported,
never from spec/binding-majors.json. CI runs it with `-s` and hands the output to
`scripts/abi-v1/majors.py assert-identity python`, which refuses unless that is
the major the map gives python and that major's description's fingerprint: the
check that a job on `v2` is not testing ABI v1 after this binding converted.
"""

from __future__ import annotations

from chtypes import library
from chtypes._abi2 import _loader


def test_abi_identity() -> None:
    decls = library._decls
    assert _loader._decls is decls, "the loader and the public API bind different layers"
    assert decls.CHS_ABI_FINGERPRINT.startswith("sha256:")
    print(
        f"chtypes_abi_identity binding=python abi={decls.CHS_ABI_VERSION} "
        f"fingerprint={decls.CHS_ABI_FINGERPRINT}"
    )
