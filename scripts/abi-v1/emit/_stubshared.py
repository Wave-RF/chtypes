"""_stubshared.py: facts shared between emit/stub.py, emit/cases.py and
build-stubs.sh, so the shipped stub libraries and the cases that describe
them can never silently drift apart.

Starts with "_": emit.discover() skips it. It defines no outputs() or
render_files() and is never an emitter by itself.

HEAD_BYTES is the number of leading bytes of a bytes_in argument the stub's
echo reports as `head_hex`, next to `len` and `sha256`; emit/stub.py (which
generates the C that computes these at run time) and emit/cases.py (which
precomputes the expected values with hashlib, at generation time) both read
this one constant.

THE VARIANT PLAN. plan(model) is the single list of stub libraries
build-stubs.sh must build: the happy path (ok, ok-b), each loader-refusal
shape from the plan's table (abi version, build_info, fingerprint, the
process-level traps), and one missing-<sym> per exported symbol. Three
consumers need the identical list and none of them is allowed to recompute
it independently:

  * build-stubs.sh (bash) gets it as "|"-delimited lines by running this
    file as a script: python3 scripts/abi-v1/emit/_stubshared.py
    --list-variants — one line per variant: name, a comma-joined list of
    `MACRO` or `MACRO=value` defines, the expected loader-refusal reason
    (sdk.json's vocabulary, or "accepted" for ok/ok-b), and
    link_allow_undefined ("0"/"1"). "|", not a tab: bash's `read` collapses
    a run of IFS whitespace (which a tab counts as), silently dropping
    ok/ok-b's empty `defines` field and shifting every later field by one;
    "|" is a strict single-character delimiter there;
  * emit/stub.py reads VARIANT_DEFINE_SYMBOL to know which preprocessor guard
    name a described symbol's omission uses (it emits the guard around every
    function's definition);
  * emit/cases.py's loader cases enumerate plan(model) directly, so a case
    exists for exactly the libraries build-stubs.sh actually produces.

Run standalone (`__main__`), this file adds its own parent directory
(scripts/abi-v1) to sys.path so it can `import model`, exactly as gen.py
does; imported as `emit._stubshared` from another emitter, that is already
on sys.path courtesy of gen.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent  # scripts/abi-v1/emit
SCRIPTS_ABI_V1 = HERE.parent  # scripts/abi-v1
ROOT = SCRIPTS_ABI_V1.parent.parent  # repository root

HEAD_BYTES = 16

# The symbol a loader checks before any other (step 3): it gets its own named
# variant ("no-abi-version", reason "not_v1") rather than folding into the
# generic missing-<sym> sweep, because a real loader distinguishes "this is
# not a v1 artifact at all" from "a v1 artifact is missing one symbol".
ABI_VERSION_SYMBOL = "chs_abi_version"

# PM ruling: chs_build_info (and every other handshake symbol except
# chs_abi_version — chs_clickhouse_version included) is NOT folded into
# not_v1 when absent. chs_abi_version answering 1 is what makes a library
# "an ABI v1+ artifact" at all (step 3); if it is present and correct, the
# library genuinely IS one, so refusing it as not_v1 over a DIFFERENT
# missing symbol would be a false message. sdk.json already has the reason
# that fits — missing_symbol plus the symbol's own name — raised at
# whichever step first needs to resolve that symbol (step 4 for
# chs_build_info, since step 4 is the first to call it), not only at step
# 6's generic sweep. not_v1 stays reserved for chs_abi_version alone.


def omit_define(symbol: str) -> str:
    """The preprocessor guard macro that omits `symbol`'s definition from the
    compiled stub. emit/stub.py wraps every function body in
    `#if !defined(<this>)`; build-stubs.sh defines exactly one of these per
    missing-<sym> (and no-abi-version) variant."""
    return f"CHS_STUB_OMIT_{symbol}"


@dataclass(frozen=True)
class Variant:
    name: str
    defines: tuple[tuple[str, str | None], ...]  # (MACRO, value-or-None)
    reason: str  # sdk.json loader-refusal vocabulary, or "accepted"
    predicate_overrides: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    link_allow_undefined: bool = False  # "unbound": the .so may link with an unresolved symbol

    def define_args(self) -> list[str]:
        """`-D` arguments for the C compiler, in a fixed order."""
        return [f"-D{k}" if v is None else f"-D{k}={v}" for k, v in self.defines]


def plan(model) -> list[Variant]:
    """Every stub library build-stubs.sh must produce, in a fixed,
    deterministic order: the two happy-path copies, the ten named
    loader-refusal and process-trap shapes, then one missing-<sym> per
    exported symbol (sorted), omitting chs_abi_version (already covered by
    "no-abi-version", the only symbol whose absence means not_v1). Every
    other missing symbol, chs_build_info and chs_clickhouse_version
    included, is missing_symbol:<name>."""
    out = [
        Variant("ok", (), "accepted"),
        Variant("ok-b", (), "accepted"),  # a second, byte-identical build: proves cross-image handling
        Variant("no-abi-version", ((omit_define(ABI_VERSION_SYMBOL), None),), "not_v1"),
        Variant("abi-version-2", (("CHS_STUB_ABI_VERSION_OVERRIDE", "2"),), "abi_version"),
        Variant("build-info-null", (("CHS_STUB_BUILD_INFO_MODE", "1"),), "build_info_malformed"),
        Variant("build-info-bad-json", (("CHS_STUB_BUILD_INFO_MODE", "2"),), "build_info_malformed"),
        Variant("build-info-dup-key", (("CHS_STUB_BUILD_INFO_MODE", "3"),), "build_info_malformed"),
        Variant("build-info-non-ascii", (("CHS_STUB_BUILD_INFO_MODE", "4"),), "build_info_malformed"),
        Variant("fingerprint-other", (("CHS_STUB_FINGERPRINT_OTHER", "1"),), "fingerprint"),
        Variant("unbound", (("CHS_STUB_UNBOUND", "1"),), "dlopen", link_allow_undefined=True),
        Variant(
            "ctor-marker",
            (("CHS_STUB_CTOR_MARKER", "1"),),
            "glibc_floor",
            predicate_overrides=(("glibc_floor", "99.0"),),
        ),
    ]
    for sym in model.symbols():
        if sym == ABI_VERSION_SYMBOL:
            continue
        out.append(Variant(f"missing-{sym}", ((omit_define(sym), None),), f"missing_symbol:{sym}"))
    return out


def echo_bytes_field(data: bytes) -> dict:
    """The {head_hex, len, sha256} object the stub's echo reports for one
    bytes_in argument, computed the same way emit/cases.py precomputes the
    expected value: hashlib.sha256, so it matches the stub's C sha256
    bit-for-bit only if that C implementation is correct RFC 6234 SHA-256 —
    which is exactly what stubtest.c proves against known test vectors."""
    import hashlib

    return {"head_hex": data[:HEAD_BYTES].hex(), "len": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _main(argv: list[str]) -> int:
    sys.path.insert(0, str(SCRIPTS_ABI_V1))
    import model as abimodel  # noqa: PLC0415

    if argv != ["--list-variants"]:
        print("usage: _stubshared.py --list-variants", file=sys.stderr)
        return 2
    m = abimodel.load(ROOT)
    for v in plan(m):
        defines = ",".join(f"{k}" if val is None else f"{k}={val}" for k, val in v.defines)
        # "|", not a tab: bash's `read` treats a tab as "IFS whitespace" and
        # COLLAPSES a run of them, silently dropping the empty `defines`
        # field "ok"/"ok-b" have (no -D flags) and shifting every field
        # after it by one — measured (build-stubs.sh wrote "reason": "0" for
        # "ok" in stubs.json, the link_allow_undefined flag, one field over).
        # "|" never appears in a name, a defines list or a reason string, and
        # bash's `read` does not collapse runs of a non-whitespace IFS
        # character, so an empty field stays empty.
        print("|".join([v.name, defines, v.reason, "1" if v.link_allow_undefined else "0"]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
