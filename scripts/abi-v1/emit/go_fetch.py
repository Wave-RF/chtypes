"""The Go fetch layer's own copy of the ABI fingerprint, generated from the
description like every other identity fact.

A dev SDK resolves `<tag>--fp-<its own fingerprint>` before `<tag>`
(docs/guides/fetch-v1.md section 3, "The dev channel's alias step"), so the
fetch layer needs the fingerprint this module speaks. It cannot import
go/internal/abiN, which is cgo, and the command line links the fetch layer
alone, cgo-free. This emitter therefore owns ONE file,
go/internal/ocifetch/abi_fingerprint_gen.go, in package ocifetch: the same
value as abiN.ChsAbiFingerprint, from the same model, so the two cannot drift
(`gen.py --check` holds both).

Generation 1 has no dev channel, so at ABI v1 this emitter produces nothing.
GOFMT-CLEAN BY CONSTRUCTION: one comment and one single-statement const.
"""

from __future__ import annotations

from . import Output, banner

BINDING = "go"  # runs for the ONE major spec/binding-majors.json gives go (emit/__init__.py)
MAJORS = (1, 2)

FETCH_FINGERPRINT_GEN = "go/internal/ocifetch/abi_fingerprint_gen.go"


def render(model) -> str:
    return (
        f"// {banner(model)}\n"
        "\n"
        "package ocifetch\n"
        "\n"
        f"// DevABIFingerprint is CHS_ABI_FINGERPRINT of ABI v{model.major}, the generation this\n"
        "// module speaks: the same value as the ABI layer's ChsAbiFingerprint. The dev\n"
        "// channel names its alias tags with it, as 64 lowercase hex characters\n"
        "// without the sha256: prefix (docs/guides/fetch-v1.md section 3).\n"
        f'const DevABIFingerprint = "{model.fingerprint}"\n'
    )


def outputs(model) -> list[Output]:
    if model.major == 1:
        return []
    return [Output(FETCH_FINGERPRINT_GEN, content=render(model))]
