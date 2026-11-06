#!/usr/bin/env python3
r"""regen-where-settings.py: write spec/abi-v2/where-settings/ from the artifact producer's files.

    regen-where-settings.py --from <dir>             copy the lists and the TSV, derive the union
    regen-where-settings.py --from <dir> --control   prove the union's derivation; writes nothing
    regen-where-settings.py --lines-document <file> --lines-source <text>
                                                     write lines.json from a channel document
    regen-where-settings.py --selftest               prove each of the above on a temporary tree

The directory is never edited by hand. <dir> holds the producer's `<line>.json`
lists, its documented-pathological.tsv and (for --control) its union.json.

--from reads lines.json and takes exactly those lines:
  - each `<line>.json` is copied byte for byte; a line with no list in <dir> stops
    the run before anything is written;
  - a `<line>.json` here that lines.json does not name is deleted;
  - documented-pathological.tsv is the producer's bytes without the data rows on a
    line lines.json does not name (each dropped row is printed);
  - union.json is derived from the lists (check-where-settings.py derive_union),
    because the producer's copy can carry a line lines.json does not name;
  - the README's sha256 list is rewritten, and the result must pass
    check-where-settings.py's rules, or the exit status is 1.

--control derives the union over EVERY list in <dir> and compares it with <dir>'s
own union.json, byte for byte: that is the proof the derivation is the producer's
algorithm. Exit status 1 on any difference; fewer than two lists is a failure too,
since a union of one proves nothing.

--lines-document takes a channel document, the predicate of the channel's signed
statement (scripts/support-v2/gen.py's header has its contract, and its
`--print-document` prints the verified one; the first copy is the dry-run fixture
under tests/fixtures/support-v2/). lines.json's `lines` are its `supported` lines
in numeric order, and `source` is the given text (the document's tag, or a note
for the fixture).

Python standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "spec" / "abi-v2" / "where-settings"

_spec = importlib.util.spec_from_file_location(
    "check_where_settings", Path(__file__).resolve().parent / "check-where-settings.py"
)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

LIST_NAME = re.compile(r"[0-9]+\.[0-9]+\.json")


class Refused(Exception):
    """A run that must write nothing, with the reason to print."""


def tsv_for(producer_tsv: bytes, lines: list[str]) -> tuple[bytes, list[str]]:
    """The producer's TSV without the data rows on a line outside `lines`: every
    other byte is unchanged. Returns the new bytes and the rows dropped."""
    kept: list[bytes] = []
    dropped: list[str] = []
    width = len(checker.TSV_HEADER)
    where = checker.TSV_HEADER.index("line")
    for raw_line in producer_tsv.splitlines(keepends=True):
        text = raw_line.decode("utf-8").rstrip("\n")
        if text.strip() and not text.startswith("#"):
            cols = text.split("\t")
            if len(cols) != width:
                raise Refused(f"{checker.TSV}: a row without {width} columns: {text!r}")
            if cols[where] not in lines:
                dropped.append(text)
                continue
        kept.append(raw_line)
    return b"".join(kept), dropped


def regen(src: Path, dst: Path, say, new_lines: bytes | None = None) -> int:
    """`new_lines`, a new lines.json, is written with everything else, so a run that
    is refused leaves the old one in place too."""
    path = dst / checker.LINES_FILE
    current = new_lines if new_lines is not None else (path.read_bytes() if path.exists() else None)
    lines, problems = checker.lines_of({} if current is None else {checker.LINES_FILE: current})
    if problems or not lines:
        raise Refused(f"{checker.LINES_FILE} is unusable: {problems}")
    lists: dict[str, bytes] = {}
    for line in lines:
        f = src / f"{line}.json"
        if not f.is_file():
            raise Refused(f"{src}: no list for {line}, which {checker.LINES_FILE} names")
        lists[line] = f.read_bytes()
    tsv_src = src / checker.TSV
    if not tsv_src.is_file():
        raise Refused(f"{src}: no {checker.TSV}")
    tsv, dropped = tsv_for(tsv_src.read_bytes(), lines)
    try:
        union = checker.derive_union([json.loads(lists[n]) for n in lines])
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise Refused(f"the union cannot be derived: {e!r}") from e

    # Everything is read and derived: only now does the tree change.
    if new_lines is not None:
        path.write_bytes(new_lines)
    for line in lines:
        (dst / f"{line}.json").write_bytes(lists[line])
    (dst / checker.UNION).write_bytes(union)
    (dst / checker.TSV).write_bytes(tsv)
    for p in sorted(dst.iterdir()):
        if LIST_NAME.fullmatch(p.name) and p.name[: -len(".json")] not in lines:
            p.unlink()
            say(f"regen: deleted {p.name}: {checker.LINES_FILE} does not name that line")
    for row in dropped:
        say(f"regen: dropped from {checker.TSV}: {row}")
    write_table(dst)

    rc, probs = checker.verdict(*checker.load(dst))
    for p in probs:
        say(f"regen: FAIL: {p}")
    if rc == 0:
        say(f"regen: {dst.name}: {len(lines)} lists copied, union derived, {len(dropped)} TSV rows dropped")
    return rc


def write_table(dst: Path) -> None:
    """Rewrite the README's run of sha256 rows from the files as they are."""
    readme_path = dst / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    raw, _ = checker.load(dst)
    block = re.compile(r"(?m)^(?:- `[^`]+\.(?:json|tsv)`: `[0-9a-f]{64}`\n)+")
    if len(block.findall(readme)) != 1:
        raise Refused("README.md: want exactly one run of sha256 rows")
    readme_path.write_text(block.sub(lambda _m: checker.table_of(raw) + "\n", readme), encoding="utf-8")


def control(src: Path, say) -> int:
    names = sorted(
        (p.name[: -len(".json")] for p in src.iterdir() if LIST_NAME.fullmatch(p.name)),
        key=checker.line_key,
    )
    union_path = src / checker.UNION
    if len(names) < 2 or not union_path.is_file():
        say(f"regen: union control FAILED: {src} has {len(names)} lists and union.json present={union_path.is_file()}")
        return 1
    want = union_path.read_bytes()
    try:
        got = checker.derive_union([json.loads((src / f"{n}.json").read_bytes()) for n in names])
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        say(f"regen: union control FAILED: the derivation refused: {e!r}")
        return 1
    if got != want:
        at = next((i for i, (a, b) in enumerate(zip(got, want, strict=False)) if a != b), min(len(got), len(want)))
        say(f"regen: union control FAILED: lines {names}: derived {len(got)} bytes, the producer's {len(want)}, first difference at byte {at}")
        return 1
    say(f"regen: union control OK: lines {names}: {len(want)} bytes, sha256 {hashlib.sha256(want).hexdigest()} byte for byte")
    return 0


def lines_doc(document: dict, source: str) -> bytes:
    entries = document.get("lines") if isinstance(document, dict) else None
    if not isinstance(entries, dict):
        raise Refused("the channel document has no `lines` object")
    supported = [n for n, e in entries.items() if isinstance(e, dict) and e.get("state") == "supported"]
    if not supported or not all(checker.LINE_SPELLING.fullmatch(n) for n in supported):
        raise Refused(f"the channel document's supported lines are unusable: {supported!r}")
    out = {"schema": checker.LINES_SCHEMA, "source": source, "lines": sorted(supported, key=checker.line_key)}
    return (json.dumps(out, indent=1) + "\n").encode()


def selftest() -> int:
    fails: list[str] = []
    said: list[str] = []

    def lists(names: list[str], extra_tier: dict | None = None) -> dict[str, bytes]:
        out = {}
        for n in names:
            d = {
                "schema": checker.LINE_SCHEMA,
                "line": n,
                "tag": f"v{n}.1.1-stable",
                "version": f"{n}.1.1",
                "generator": "selftest",
                "settings": [
                    {"name": "a_setting", "tier": (extra_tier or {}).get(n, "execution"), "kind": "where", "reason": "r", "scopes": ["s" + n], "sources": ["x"]},
                    {"name": "only_" + n.replace(".", "_"), "tier": "execution", "kind": "where", "reason": "r", "scopes": ["t"], "sources": ["x"]},
                ],
            }
            out[n] = (json.dumps(d, indent=1) + "\n").encode()
        return out

    header = "# comment\n# " + "\t".join(checker.TSV_HEADER) + "\n"

    def row(line: str) -> str:
        return f"x\t1\t{line}\tfilter\te:36\trefuses-every-query\tthe lead, 2026-01-01\n"

    with tempfile.TemporaryDirectory(prefix="regen-where-settings-") as tmp:
        src, dst = Path(tmp) / "src", Path(tmp) / "dst"
        src.mkdir()
        dst.mkdir()
        producer = lists(["1.2", "1.3", "1.10"])
        for n, b in producer.items():
            (src / f"{n}.json").write_bytes(b)
        (src / checker.TSV).write_text(header + row("1.2") + row("1.3") + row("1.10"), encoding="utf-8")
        (src / checker.UNION).write_bytes(checker.derive_union([json.loads(producer[n]) for n in ("1.2", "1.3", "1.10")]))
        (dst / checker.LINES_FILE).write_bytes(lines_doc({"lines": {"1.10": {"state": "supported"}, "1.2": {"state": "supported"}, "1.3": {"state": "retired"}}}, "selftest"))
        (dst / "README.md").write_text("- `x.json`: `" + "0" * 64 + "`\n", encoding="utf-8")
        (dst / "1.3.json").write_bytes(producer["1.3"])  # the stale list --from must delete
        if json.loads((dst / checker.LINES_FILE).read_text())["lines"] != ["1.2", "1.10"]:
            fails.append("lines.json: not the supported lines in numeric order")

        if control(src, said.append) != 0:
            fails.append("--control on a matching union: not 0")
        (src / checker.UNION).write_bytes((src / checker.UNION).read_bytes() + b"\n")
        if control(src, said.append) == 0:
            fails.append("--control on a union that differs: exit 0")
        (src / checker.UNION).unlink()
        if control(src, said.append) == 0:
            fails.append("--control with no union: exit 0")

        rc = regen(src, dst, said.append)
        if rc != 0:
            fails.append(f"--from: exit {rc}: {said[-3:]}")
        if not (dst / "1.2.json").read_bytes() == producer["1.2"] or not (dst / "1.10.json").read_bytes() == producer["1.10"]:
            fails.append("--from: a list is not byte for byte the producer's")
        if (dst / "1.3.json").exists():
            fails.append("--from: a list lines.json does not name survived")
        if (dst / checker.TSV).read_text(encoding="utf-8") != header + row("1.2") + row("1.10"):
            fails.append("--from: the TSV is not the producer's without the retired line's row")
        if json.loads((dst / checker.UNION).read_bytes())["tags"] != ["v1.2.1.1-stable", "v1.10.1.1-stable"]:
            fails.append("--from: the union's tags are not the kept lists' in numeric order")

        before = {p.name: p.read_bytes() for p in dst.iterdir()}
        (src / "1.10.json").unlink()
        try:
            regen(src, dst, said.append)
            fails.append("--from with a line's list missing: no refusal")
        except Refused:
            pass
        if {p.name: p.read_bytes() for p in dst.iterdir()} != before:
            fails.append("--from with a line's list missing: the tree changed")
        moved = lines_doc({"lines": {"1.2": {"state": "supported"}, "1.10": {"state": "supported"}, "1.3": {"state": "supported"}}}, "selftest")
        try:
            regen(src, dst, said.append, moved)
            fails.append("--from with a new lines.json and a list missing: no refusal")
        except Refused:
            pass
        if {p.name: p.read_bytes() for p in dst.iterdir()} != before:
            fails.append("--from with a new lines.json and a list missing: the tree changed")

        (src / "1.10.json").write_bytes(lists(["1.10"], {"1.10": "output"})["1.10"])
        try:
            regen(src, dst, said.append)
            fails.append("--from with lines disagreeing on a tier: no refusal")
        except Refused:
            pass
        (dst / checker.LINES_FILE).write_bytes(b"{}\n")
        try:
            regen(src, dst, said.append)
            fails.append("--from with an unusable lines.json: no refusal")
        except Refused:
            pass
    for name, doc in (("no lines", {}), ("nothing supported", {"lines": {"1.3": {"state": "retired"}}})):
        try:
            lines_doc(doc, "selftest")
            fails.append(f"--lines-document, {name}: no refusal")
        except Refused:
            pass
    if fails:
        for f in fails:
            print("SELFTEST FAIL:", f, file=sys.stderr)
        return 1
    print("regen-where-settings selftest: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", type=Path, help="the producer's files")
    ap.add_argument("--control", action="store_true", help="prove the union's derivation against --from")
    ap.add_argument("--lines-document", type=Path, help="a channel document to write lines.json from")
    ap.add_argument("--lines-source", help="lines.json's `source`: the document's tag, or a note")
    ap.add_argument("--selftest", action="store_true", help="prove each mode on a temporary tree")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    def say(m: str) -> None:
        print(m, file=sys.stderr)

    try:
        if args.control:
            if not args.src or args.lines_document:
                ap.error("--control takes --from, and nothing else")
            return control(args.src, say)
        if not args.lines_document:
            if not args.src:
                ap.error("--from is required")
            return regen(args.src, DIR, say)
        if not args.lines_source:
            ap.error("--lines-document needs --lines-source")
        out = lines_doc(json.loads(args.lines_document.read_bytes()), args.lines_source)
        if args.src:
            return regen(args.src, DIR, say, out)
        (DIR / checker.LINES_FILE).write_bytes(out)
        say(f"regen: wrote {checker.LINES_FILE}: {json.loads(out)['lines']}")
        return 0
    except Refused as e:
        say(f"regen: nothing written: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
