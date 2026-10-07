"""The Rust fetch layer's own copy of the ABI fingerprint, generated from the
description like every other identity fact.

A dev SDK resolves `<tag>--fp-<its own fingerprint>` before `<tag>`
(docs/guides/fetch-v1.md section 3, "The dev channel's alias step"), so the
fetch layer needs the fingerprint this crate speaks. It cannot reach
`crate::abiN::decls`: the command line (src/main.rs) and the conformance suite
recompile src/ocifetch under their own crate roots with `#[path]`, where no
`abiN` module exists. This emitter therefore owns ONE file,
rust/src/ocifetch/abi_fingerprint.rs: the same value as
`abiN::decls::CHS_ABI_FINGERPRINT`, from the same model, so the two cannot
drift (`gen.py --check` holds both).

Generation 1 has no dev channel, so at ABI v1 this emitter produces nothing.
"""

from __future__ import annotations

from . import Output, banner
from .rust import _rustfmt

BINDING = "rust"  # runs for the ONE major spec/binding-majors.json gives rust (emit/__init__.py)
MAJORS = (1, 2)

FETCH_FINGERPRINT_PATH = "rust/src/ocifetch/abi_fingerprint.rs"


def render(model) -> str:
    return (
        f"// {banner(model)}\n"
        "\n"
        f"//! `CHS_ABI_FINGERPRINT` of ABI v{model.major}, the generation this crate speaks: the\n"
        "//! same value as the ABI layer's own constant. The dev channel names its\n"
        "//! alias tags with it, as 64 lowercase hex characters without the `sha256:`\n"
        "//! prefix (docs/guides/fetch-v1.md section 3).\n"
        "\n"
        "/// The ABI fingerprint this crate speaks, `sha256:` and 64 lowercase hex.\n"
        f'pub const DEV_ABI_FINGERPRINT: &str = "{model.fingerprint}";\n'
    )


def outputs(model) -> list[Output]:
    if model.major == 1:
        return []
    return [Output(FETCH_FINGERPRINT_PATH, content=_rustfmt(render(model)))]
