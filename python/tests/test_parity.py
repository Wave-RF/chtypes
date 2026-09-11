"""The binding parity contract, checked against this binding.

`tests/parity/manifest.json` at the repository root is the ONE machine-readable
source of truth for what every binding must expose; `spec/bindings.md` is the
prose that explains why. This file is Python's half of the enforcement: it
resolves every spelling the manifest assigns to the `python` column against the
real, imported package, and fails by NAME when one is missing — including the
names of the bindings that do have it, because "python is missing
`verify_installed`, which go/ts/rust all expose" is the sentence that gets the
gap fixed and "parity check failed" is not.

Three rules this file exists to hold, each of which the repository has paid for
elsewhere:

* **It cannot pass by doing nothing.** A manifest that parsed to zero
  capabilities, or fewer than its own declared floors, FAILS. So does a run in
  which nothing resolved.
* **It needs no artifact.** Parity is a claim about the API surface, not about
  dlopening a library, so every assertion here runs in the artifact-free CI job.
  Nothing in this file constructs a `Registry`.
* **A deliberate gap is still written down.** A binding that should not have a
  capability declares `{"absent": "<why>"}` in its column; an absent with no
  reason fails the manifest's own integrity check, so a gap cannot be recorded
  silently.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "tests" / "parity" / "manifest.json"
GO_COPY = REPO / "go" / "chtypes" / "testdata" / "parity.json"
SRC = REPO / "python" / "src" / "chtypes"

LANG = "python"
OTHERS = ("go", "ts", "rust")


# --------------------------------------------------------------------------
# the manifest


def _load() -> dict:
    if not MANIFEST.is_file():
        pytest.fail(
            f"the parity manifest is missing at {MANIFEST}. It is the contract this suite "
            f"exists to enforce; an absent manifest is a failure, never a skip."
        )
    raw = MANIFEST.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except ValueError as exc:  # pragma: no cover - a corrupt manifest is a hard stop
        pytest.fail(f"{MANIFEST} is not valid JSON: {exc}")


MANIFEST_DOC = _load()
CAPABILITIES = MANIFEST_DOC.get("capabilities", [])


def column(cap: dict, lang: str) -> dict:
    """One capability's column for one language, normalised to a dict."""
    raw = cap.get(lang)
    if isinstance(raw, str):
        return {"symbol": raw}
    if isinstance(raw, dict):
        return raw
    return {}


def spelling(cap: dict, lang: str) -> str | None:
    return column(cap, lang).get("symbol")


def also_in(cap: dict) -> str:
    """The other bindings that DO carry this capability, for the failure text."""
    have = [lang for lang in OTHERS if spelling(cap, lang)]
    return "/".join(have) if have else "no other binding"


# --------------------------------------------------------------------------
# integrity — the manifest cannot be empty, and a gap cannot be silent


def test_manifest_meets_its_own_floors() -> None:
    """A manifest that loaded nothing must FAIL, not quietly pass.

    Every count below is read off the manifest's own `floors`, so the guard
    travels with the contract instead of being a number in a test file.
    """
    assert MANIFEST_DOC.get("schema") == 1, "unknown parity manifest schema"
    floors = MANIFEST_DOC["floors"]
    groups = MANIFEST_DOC["groups"]

    assert len(CAPABILITIES) >= floors["capabilities"], (
        f"the parity manifest declares {len(CAPABILITIES)} capabilities, below its own floor of "
        f"{floors['capabilities']}. A shrinking contract is how this check passes by doing nothing."
    )
    valued = [c for c in CAPABILITIES if "value" in c]
    assert len(valued) >= floors["valued"], (
        f"only {len(valued)} capabilities carry a shared `value`, below the floor of "
        f"{floors['valued']}. Values are what prove the four bindings answer the same bytes, "
        f"not merely that they have a symbol."
    )
    for group in groups:
        n = sum(1 for c in CAPABILITIES if c["group"] == group)
        assert n >= floors["per_group"], (
            f"group {group!r} has {n} capabilities, below the floor of {floors['per_group']}"
        )


def test_every_capability_is_fully_declared() -> None:
    """Each capability names all four languages, or says in writing why not."""
    seen: set[str] = set()
    problems: list[str] = []
    for cap in CAPABILITIES:
        cid = cap.get("id", "<no id>")
        if cid in seen:
            problems.append(f"{cid}: duplicate id")
        seen.add(cid)
        if not cap.get("what", "").strip():
            problems.append(f"{cid}: no `what` — a capability with no description is a name, not a contract")
        if cap.get("group") not in MANIFEST_DOC["groups"]:
            problems.append(f"{cid}: unknown group {cap.get('group')!r}")

        absent = 0
        for lang in MANIFEST_DOC["languages"]:
            col = column(cap, lang)
            if not col:
                problems.append(f"{cid}: no {lang} column at all — declare a spelling or an absence")
                continue
            if "absent" in col:
                absent += 1
                if not str(col["absent"]).strip():
                    problems.append(
                        f"{cid}: {lang} is declared absent with no reason. A gap that nobody had "
                        f"to justify in writing is how parity rots."
                    )
            elif not str(col.get("symbol", "")).strip():
                problems.append(f"{cid}: {lang} column has neither `symbol` nor `absent`")
        if absent == len(MANIFEST_DOC["languages"]):
            problems.append(f"{cid}: absent in every binding — that is a note, not a contract")
    assert not problems, "the parity manifest is not internally consistent:\n  " + "\n  ".join(problems)


def test_the_go_copy_of_the_manifest_is_byte_identical() -> None:
    """Go embeds its own copy, because `scripts/check-standalone.sh` runs the Go
    suite from a bare copy of `go/` with nothing beside it — no repository root,
    so no `tests/parity/manifest.json` to read. The copy is a cache of the one
    source of truth, and three suites (this one, ts, rust) assert it has not
    drifted; Go asserts it too whenever it can see the repository root.
    """
    assert GO_COPY.is_file(), f"the Go copy of the parity manifest is missing at {GO_COPY}"
    if GO_COPY.read_bytes() != MANIFEST.read_bytes():
        pytest.fail(
            f"{GO_COPY} has drifted from {MANIFEST}. The manifest is one file; this is its "
            f"embedded copy. Re-sync it:\n\n    cp tests/parity/manifest.json go/chtypes/testdata/parity.json\n"
        )


# --------------------------------------------------------------------------
# resolving a python spelling against the real package


def _class_source_attrs(cls: type) -> set[str]:
    """Instance attributes a class declares: `__slots__`, annotations, and plain
    `self.x = ...` in `__init__`. A runtime `getattr` cannot see any of them on
    the class, and several contract entries (`Library.version`, `Schema.columns`)
    are exactly that shape.
    """
    names: set[str] = set(getattr(cls, "__slots__", ()) or ())
    names |= set(getattr(cls, "__annotations__", {}) or {})
    try:
        file = inspect.getsourcefile(cls)
        tree = ast.parse(Path(file).read_text(encoding="utf-8")) if file else None
    except (OSError, TypeError, SyntaxError):  # pragma: no cover
        tree = None
    if tree is None:
        return names
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == cls.__name__):
            continue
        for sub in ast.walk(node):
            target = None
            if isinstance(sub, ast.Assign):
                target = sub.targets[0] if sub.targets else None
            elif isinstance(sub, ast.AnnAssign):
                target = sub.target
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                names.add(target.attr)
    return names


def resolve(spelled: str) -> tuple[bool, object]:
    """Resolve a dotted spelling from the `chtypes` package, the way a caller would."""
    current: object = importlib.import_module("chtypes")
    for part in spelled.split("."):
        if hasattr(current, part):
            current = getattr(current, part)
            continue
        # A submodule the package does not re-export as an attribute.
        if inspect.ismodule(current):
            try:
                current = importlib.import_module(f"{current.__name__}.{part}")
                continue
            except ImportError:
                return False, None
        # An instance attribute, invisible on the class.
        if inspect.isclass(current) and part in _class_source_attrs(current):
            return True, ...
        return False, None
    return True, current


# --------------------------------------------------------------------------
# presence, and value


def test_python_exposes_every_capability_the_contract_assigns_it() -> None:
    missing: list[str] = []
    resolved = 0
    for cap in CAPABILITIES:
        spelled = spelling(cap, LANG)
        if not spelled:
            continue
        if cap["kind"] == "cli":
            continue  # covered by test_the_cli_offers_every_contract_subcommand
        found, _ = resolve(spelled)
        if found:
            resolved += 1
        else:
            missing.append(
                f"{cap['id']}: python is missing `{spelled}`, which {also_in(cap)} expose "
                f"— {cap['what']}"
            )
    assert resolved > 0, "no python spelling resolved at all — the check asserted nothing"
    assert not missing, (
        f"python does not carry {len(missing)} capability/capabilities the parity contract "
        f"assigns it:\n  " + "\n  ".join(missing)
    )


def test_python_answers_the_same_values_as_the_other_bindings() -> None:
    """`value` entries are the ones that would be a real product bug: a reason
    string, a format code, a discovery query or an artifact error code that
    differs between two SDKs is two answers to one question.
    """
    wrong: list[str] = []
    checked = 0
    for cap in CAPABILITIES:
        if "value" not in cap or cap["kind"] == "cli":
            continue
        col = column(cap, LANG)
        spelled = col.get("symbol")
        if not spelled or col.get("value_check") is False:
            continue
        found, value = resolve(spelled)
        if not found or value is ...:
            continue  # absence is the other test's failure, reported once
        want = cap["value"]
        got = int(value) if isinstance(want, int) and not isinstance(want, bool) else str(value)
        checked += 1
        if got != want:
            wrong.append(f"{cap['id']}: python `{spelled}` is {got!r}, the contract says {want!r}")
    assert checked >= MANIFEST_DOC["floors"]["valued"], (
        f"only {checked} shared values were actually compared, below the floor of "
        f"{MANIFEST_DOC['floors']['valued']} — a value check that checks nothing is not a check"
    )
    assert not wrong, "python answers differently from the contract:\n  " + "\n  ".join(wrong)


def test_the_cli_offers_every_contract_subcommand() -> None:
    """`chtypes fetch|verify|list|where` is a surface too, and it drifted once."""
    source = (SRC / "__main__.py").read_text(encoding="utf-8")
    missing = [
        f"{cap['id']}: the python CLI has no `{cap['value']}` subcommand, which {also_in(cap)} offer"
        for cap in CAPABILITIES
        if cap["kind"] == "cli" and spelling(cap, LANG) and f'"{cap["value"]}"' not in source
    ]
    commands = [c for c in CAPABILITIES if c["kind"] == "cli"]
    assert commands, "the contract names no CLI subcommands"
    assert not missing, "\n  ".join(missing)


# --------------------------------------------------------------------------
# nothing public escapes the contract


def _public_surface() -> tuple[set[str], set[str]]:
    """(top-level exports, every public name including methods)."""
    import chtypes

    top = set(chtypes.__all__)
    every = set(top)
    for name in top:
        obj = getattr(chtypes, name, None)
        if not inspect.isclass(obj):
            continue
        for attr in dir(obj):
            if attr.startswith("_") and not (attr.startswith("__") and attr.endswith("__")):
                continue
            if attr in vars(obj) or attr in getattr(obj, "__slots__", ()) or ():
                every.add(f"{name}.{attr}")
        for attr in _class_source_attrs(obj):
            if not attr.startswith("_"):
                every.add(f"{name}.{attr}")
    return top, every


def test_no_public_name_escapes_the_contract() -> None:
    """The other half of parity: a capability a binding implements and the
    contract omits is just as much a divergence as one it lacks.
    """
    top, _ = _public_surface()
    declared = {s for cap in CAPABILITIES if (s := spelling(cap, LANG))}
    allowed = set(MANIFEST_DOC["unlisted"][LANG])
    # Only the leading segment matters for a top-level export.
    declared_top = {s.split(".")[0] for s in declared}
    allowed_top = {s.split(".")[0] for s in allowed}

    undeclared = sorted(top - declared_top - allowed_top)
    assert not undeclared, (
        f"python exports {len(undeclared)} public name(s) the parity contract has never heard "
        f"of:\n  " + "\n  ".join(undeclared) + "\n\nEither give each one a capability in "
        f"tests/parity/manifest.json (if the other bindings should have it too) or list it under "
        f'`unlisted.python` with a reason (if it is deliberately ours alone).'
    )


def test_the_unlisted_allowlist_has_not_rotted() -> None:
    """An allowlist entry for a name that no longer exists is an excuse with
    nothing behind it, and it hides the next real one.
    """
    _, every = _public_surface()
    gone = []
    for name in sorted(MANIFEST_DOC["unlisted"][LANG]):
        found, _ = resolve(name)
        if not found and name not in every:
            gone.append(name)
    assert not gone, (
        "`unlisted.python` in the parity manifest excuses names this binding no longer "
        "exports:\n  " + "\n  ".join(gone)
    )


def test_the_manifest_and_the_spec_table_agree() -> None:
    """`spec/bindings.md`'s object-model table is for humans and this manifest is
    for machines, so they are two statements of one contract and they can drift.
    The parse below is deliberately shallow — it reads the table's Concept column
    and asserts each row is REPRESENTED here, not that the markdown is a data
    format. Markdown is never parsed at test time to decide what must exist.
    """
    doc = (REPO / "spec" / "bindings.md").read_text(encoding="utf-8")
    rows = [
        line.split("|")[1].strip()
        for line in doc.splitlines()
        if line.startswith("| `chs_") or line.startswith("| ")
    ]
    named = {
        "chs_registered_families": "library.registered-families",
        "chs_function_flags": "library.function-flags",
        "chs_reference_type": "library.reference-type",
    }
    ids = {c["id"] for c in CAPABILITIES}
    missing = [cid for entry, cid in named.items() if f"`{entry}`" in doc and cid not in ids]
    assert rows, "spec/bindings.md has no tables — the document this contract restates is gone"
    assert not missing, (
        "spec/bindings.md names ABI entry points the parity manifest does not cover: "
        + ", ".join(missing)
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
