#!/usr/bin/env python3
r"""regen-where-settings.py: write spec/abi-v2/where-settings/ from the artifact producer's files.

    regen-where-settings.py --from <dir>             copy the lists and the TSV, derive the union
    regen-where-settings.py --from <dir> --control   prove the union's derivation; writes nothing
    regen-where-settings.py --lines-document <file> --lines-source <source> [--from <dir>]
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
  - the README's sha256 list is rewritten.

Nothing is written until the whole tree as it would be has passed
check-where-settings.py's rules: a tree that would fail stops the run with exit
status 1 and every byte where it was. Afterward the tree on disk is judged again,
and a difference there is exit status 1 too.

--control derives the union over EVERY list in <dir> and compares it with <dir>'s
own union.json, byte for byte: that is the proof the derivation is the producer's
algorithm. Exit status 1 on any difference; fewer than two lists is a failure too,
since a union of one proves nothing.

--lines-document takes a channel document, the predicate of the channel's signed
statement (scripts/support-v2/gen.py's header has its contract, and its
`--print-document` prints the verified one; the first copy is the dry-run fixture
under tests/fixtures/support-v2/). It is held to rule 5's shape here, and a document
with a malformed entry is refused, never read around; this proves the shape only,
and only --print-document proves a signature. lines.json's `lines` are the supported
lines in numeric order, and `source` is the given one, closed form: the document's
tag, equal to its produced_at, or the fixture marker. With --from the lists move
with it. Without --from only lines.json and its README row are written, which is
right only while the supported lines do not change (a first publication that names
the same lines), so anything else is refused, naming --from.

`source` never moves backward here: once it names a publication, a run that would
write the fixture marker, or an older tag, is refused (scripts/support-v2/gen.py's
THE LATCH). A hand edit of lines.json is not seen by this script; that file is under
`spec/**`, a protected path.

--lines-source needs --lines-document, and --control takes --from and nothing else:
each is a usage error (exit status 2, no verdict line).

A read or write that fails (an unreadable file, a producer file that is not UTF-8, a
target that cannot be written) ends the run with exit status 1 and its verdict line,
naming the error. A write that fails part-way leaves the tree part-written; the run
says to read it with check-where-settings.py --check.

Every mode but --selftest ends with one `regen: verdict:` line on stderr, naming its
exit status, and --selftest drives each through this file's real entry point in a
subprocess, on a planted tree.

Python standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
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
# Rule 5 of scripts/support-v2/gen.py's header, which needs no binding to read. That
# script's selftest holds this copy of the shape to its own, plant by plant.
STATES = ("supported", "retired")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
COMMIT = re.compile(r"[0-9a-f]{40}")
PRODUCED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
REPOSITORY = re.compile(r"[a-z0-9._-]+(?:/[a-z0-9._-]+)*")


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


def with_table(readme: str, raw: dict[str, bytes]) -> str:
    """The README with its run of sha256 rows rewritten for `raw`."""
    block = re.compile(r"(?m)^(?:- `[^`]+\.(?:json|tsv)`: `[0-9a-f]{64}`\n)+")
    if len(block.findall(readme)) != 1:
        raise Refused("README.md: want exactly one run of sha256 rows")
    return block.sub(lambda _m: checker.table_of(raw) + "\n", readme)


def refused_candidate(raw: dict[str, bytes], readme: str, say) -> bool:
    """Judge the tree as it would be. True means it must not be written."""
    problems = checker.check(raw, readme)
    for p in problems:
        say(f"regen: FAIL: {p}")
    if problems:
        say("regen: nothing written: the tree it would write fails the rules above")
    return bool(problems)


def on_disk_verdict(dst: Path, say) -> int:
    """The tree that was written, as check-where-settings.py --check will read it."""
    rc, problems = checker.verdict(*checker.load(dst))
    for p in problems:
        say(f"regen: FAIL: {p}")
    return rc


def source_of(data: bytes | None) -> str:
    """lines.json's `source` in `data`, or "" when there is none to read."""
    try:
        doc = checker.strict_json(data) if data is not None else None
    except ValueError:
        return ""
    source = doc.get("source") if isinstance(doc, dict) else None
    return source if isinstance(source, str) else ""


def refuse_backward(current: bytes | None, new_lines: bytes) -> None:
    """THE LATCH only turns one way: a source that names a publication does not
    become the fixture marker or an older publication."""
    old, new = source_of(current), source_of(new_lines)
    if not (checker.PUBLICATION_TAG.fullmatch(old) and not checker.source_problem(old)):
        return
    if new == checker.FIXTURE_SOURCE or (checker.PUBLICATION_TAG.fullmatch(new) and new < old):
        raise Refused(f"--lines-source {new} moves lines.json's source backward from the publication {old}")


def regen(src: Path, dst: Path, say, new_lines: bytes | None = None, *, write=Path.write_bytes) -> int:
    """`new_lines`, a new lines.json, is written with everything else, so a run that
    is refused leaves the old one in place too. `write` is how a file reaches the
    disk: the selftest plants a write that goes wrong through it, which nothing
    short of a failing disk can otherwise do."""
    path = dst / checker.LINES_FILE
    if new_lines is not None:
        refuse_backward(path.read_bytes() if path.exists() else None, new_lines)
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

    # Everything is read and derived. Judge the tree as it would be, whatever else
    # is in the directory; only then does anything change.
    raw, readme = checker.load(dst)
    candidate = {n: b for n, b in raw.items() if not LIST_NAME.fullmatch(n)}
    candidate.update({f"{line}.json": lists[line] for line in lines})
    candidate.update({checker.UNION: union, checker.TSV: tsv, checker.LINES_FILE: current})
    new_readme = with_table(readme, candidate)
    if refused_candidate(candidate, new_readme, say):
        return 1

    if new_lines is not None:
        write(path, new_lines)
    for line in lines:
        write(dst / f"{line}.json", lists[line])
    write(dst / checker.UNION, union)
    write(dst / checker.TSV, tsv)
    for p in sorted(dst.iterdir()):
        if LIST_NAME.fullmatch(p.name) and p.name[: -len(".json")] not in lines:
            p.unlink()
            say(f"regen: deleted {p.name}: {checker.LINES_FILE} does not name that line")
    for row in dropped:
        say(f"regen: dropped from {checker.TSV}: {row}")
    write(dst / "README.md", new_readme.encode("utf-8"))

    rc = on_disk_verdict(dst, say)
    if rc == 0:
        say(f"regen: {dst.name}: {len(lines)} lists copied, union derived, {len(dropped)} TSV rows dropped")
    return rc


def relabel(dst: Path, say, new_lines: bytes, *, write=Path.write_bytes) -> int:
    """lines.json alone, with its README row: the supported lines stay where they
    are, so no list has to move. `write` is regen's."""
    raw, readme = checker.load(dst)
    refuse_backward(raw.get(checker.LINES_FILE), new_lines)
    old, _ = checker.lines_of(raw)
    new, problems = checker.lines_of({checker.LINES_FILE: new_lines})
    if problems or not new:
        raise Refused(f"the new {checker.LINES_FILE} is unusable: {problems}")
    if new != old:
        raise Refused(f"the supported lines move from {old} to {new}, so the lists must move with them: give --from <dir>")
    candidate = {**raw, checker.LINES_FILE: new_lines}
    new_readme = with_table(readme, candidate)
    if refused_candidate(candidate, new_readme, say):
        return 1
    write(dst / checker.LINES_FILE, new_lines)
    write(dst / "README.md", new_readme.encode("utf-8"))
    rc = on_disk_verdict(dst, say)
    if rc == 0:
        say(f"regen: wrote {checker.LINES_FILE}: {new}")
    return rc


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


def basic_form(produced_at: object) -> str:
    """produced_at as a publication tag, or "" when it is not a real UTC time."""
    if not isinstance(produced_at, str) or not PRODUCED_AT.fullmatch(produced_at):
        return ""
    try:
        datetime.strptime(produced_at, "%Y-%m-%dT%H:%M:%SZ")  # noqa: DTZ007
    except ValueError:
        return ""
    return produced_at.replace("-", "").replace(":", "")


def entry_problem(entry: object) -> str:
    """What is wrong with one `lines` entry, or ""."""
    if not isinstance(entry, dict):
        return "it is not an object"
    state = entry.get("state")
    if not isinstance(state, str) or state not in STATES:
        return "its state is neither supported nor retired"
    since = entry.get("since")
    if not isinstance(since, str) or not DATE.fullmatch(since):
        return "its since is not a date"
    try:
        datetime.strptime(since, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        return "its since is not a date"
    commit = entry.get("security_md_commit")
    if not isinstance(commit, str) or (commit != "" and not COMMIT.fullmatch(commit)):
        return "its security_md_commit is neither 40 lowercase hex nor empty"
    return ""


def document_problem(document: object) -> str:
    """What keeps `document` from rule 5's shape, or "". Which channel it belongs to
    is not known here, so `repository` is held to a repository's shape only."""
    if not isinstance(document, dict):
        return "it is not an object"
    schema = document.get("schema")
    if type(schema) is not int or schema != 1:
        return "its schema is not 1"
    repository = document.get("repository")
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        return "its repository is not a repository path"
    if not basic_form(document.get("produced_at")):
        return "its produced_at is not a UTC time"
    entries = document.get("lines")
    if not isinstance(entries, dict):
        return "its lines is not an object"
    for line, entry in entries.items():
        if not checker.LINE_SPELLING.fullmatch(line):
            return "a key of its lines is not a line"
        why = entry_problem(entry)
        if why:
            return f"the entry for {line}: {why}"
    return ""


def lines_doc(document: object, source: str) -> bytes:
    problem = document_problem(document)
    if problem:
        raise Refused(f"the channel document: {problem}")
    problem = checker.source_problem(source)
    if problem:
        raise Refused(f"--lines-source: {problem}")
    if source != checker.FIXTURE_SOURCE and basic_form(document["produced_at"]) != source:
        raise Refused(f"--lines-source {source} is not the document's produced_at")
    entries = document["lines"]
    supported = [n for n, e in entries.items() if e.get("state") == "supported"]
    if not supported:
        raise Refused("the channel document names no supported line")
    out = {"schema": checker.LINES_SCHEMA, "source": source, "lines": sorted(supported, key=checker.line_key)}
    return (json.dumps(out, indent=1) + "\n").encode()


def selftest() -> int:
    fails: list[str] = []
    said: list[str] = []
    tag, tag2 = "20260102T030405Z", "20260103T030405Z"

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

    def document(states: dict[str, str], produced_at: str = "2026-01-02T03:04:05Z") -> dict:
        return {
            "schema": 1,
            "repository": "chtypes/v2-dev",
            "produced_at": produced_at,
            "lines": {n: {"state": s, "since": "2026-01-01", "security_md_commit": "a" * 40} for n, s in states.items()},
        }

    header = "# comment\n# " + "\t".join(checker.TSV_HEADER) + "\n"

    def row(line: str) -> str:
        return f"a_setting\t1\t{line}\tfilter\te:36\trefuses-every-query\tthe lead, 2026-01-01\n"

    def refused_with(what: str, needle: str, call) -> None:
        """`call` must raise Refused, and for this reason: a refusal from further
        on would pass the test and hide that this rule had stopped firing."""
        try:
            call()
            fails.append(f"{what}: no refusal")
        except Refused as e:
            if needle not in str(e):
                fails.append(f"{what}: refused for another reason: {e}")

    with tempfile.TemporaryDirectory(prefix="regen-where-settings-") as tmp:
        src, dst = Path(tmp) / "src", Path(tmp) / "dst"
        src.mkdir()
        dst.mkdir()
        producer = lists(["1.2", "1.3", "1.10"])
        for n, b in producer.items():
            (src / f"{n}.json").write_bytes(b)
        (src / checker.TSV).write_text(header + row("1.2") + row("1.3") + row("1.10"), encoding="utf-8")
        (src / checker.UNION).write_bytes(checker.derive_union([json.loads(producer[n]) for n in ("1.2", "1.3", "1.10")]))
        (dst / checker.LINES_FILE).write_bytes(lines_doc(document({"1.10": "supported", "1.2": "supported", "1.3": "retired"}), tag))
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

        def tree_now() -> dict[str, bytes]:
            return {p.name: p.read_bytes() for p in dst.iterdir()}

        before = tree_now()

        # The new lines.json is written with the rest (and the rest moves with it).
        wider = lines_doc(document({"1.2": "supported", "1.3": "supported", "1.10": "supported"}, "2026-01-03T03:04:05Z"), tag2)
        if regen(src, dst, said.append, wider) != 0 or (dst / checker.LINES_FILE).read_bytes() != wider or not (dst / "1.3.json").exists():
            fails.append("--from with a new lines.json: it was not written, or the lists did not move with it")
        for p in dst.iterdir():
            if p.name not in before:
                p.unlink()
        for name, data in before.items():
            (dst / name).write_bytes(data)

        (src / "1.10.json").unlink()
        refused_with("--from with a line's list missing", "no list for 1.10", lambda: regen(src, dst, said.append))
        if tree_now() != before:
            fails.append("--from with a line's list missing: the tree changed")
        moved = lines_doc(document({"1.2": "supported", "1.10": "supported", "1.3": "supported"}), tag)
        refused_with("--from with a new lines.json and a list missing", "no list for 1.10", lambda: regen(src, dst, said.append, moved))
        if tree_now() != before:
            fails.append("--from with a new lines.json and a list missing: the tree changed")

        (src / "1.10.json").write_bytes(lists(["1.10"], {"1.10": "output"})["1.10"])
        refused_with("--from with lines disagreeing on a tier", "the union cannot be derived", lambda: regen(src, dst, said.append))
        if tree_now() != before:
            fails.append("--from with lines disagreeing on a tier: the tree changed")

        # The tree as it would be fails a rule: refused, and every byte stays.
        def with_list(line: str, change) -> None:
            d = json.loads(producer[line])
            change(d)
            (src / f"{line}.json").write_bytes((json.dumps(d, indent=1) + "\n").encode())

        with_list("1.10", lambda d: d.update(tag="v1.10.garbage"))
        said.clear()
        if regen(src, dst, said.append) == 0 or tree_now() != before or not any("nothing written" in m for m in said):
            fails.append("--from with a list failing a rule: not refused before writing")

        # A write that goes wrong after the tree was judged: what is on disk decides.
        (src / "1.10.json").write_bytes(producer["1.10"])
        said.clear()

        def bad_write(path: Path, data: bytes) -> None:
            path.write_bytes(data + b"x" if path.name == "1.2.json" else data)

        if regen(src, dst, said.append, write=bad_write) == 0:
            fails.append("--from with a write that went wrong: the tree on disk fails --check, and the status was 0")
        for name, data in before.items():
            (dst / name).write_bytes(data)
        said.clear()
        relabelled = lines_doc(document({"1.2": "supported", "1.10": "supported"}), tag)

        def bad_lines_write(path: Path, data: bytes) -> None:
            path.write_bytes(data + b"x" if path.name == checker.LINES_FILE else data)

        if relabel(dst, said.append, relabelled, write=bad_lines_write) == 0:
            fails.append("--lines-document with a write that went wrong: the tree on disk fails --check, and the status was 0")
        for name, data in before.items():
            (dst / name).write_bytes(data)

        # The latch turns one way: a tag does not go back to the marker or to an older tag.
        two = {"1.2": "supported", "1.10": "supported"}
        for what, new in (
            ("the fixture marker", lines_doc(document(two), checker.FIXTURE_SOURCE)),
            ("an older tag", lines_doc(document(two, "2026-01-01T03:04:05Z"), "20260101T030405Z")),
        ):
            refused_with(f"--lines-document, back to {what}, no --from", "backward", lambda n=new: relabel(dst, said.append, n))
            refused_with(f"--lines-document, back to {what}, with --from", "backward", lambda n=new: regen(src, dst, said.append, n))
            if tree_now() != before:
                fails.append(f"--lines-document, back to {what}: the tree changed")

        # A README with no sha256 rows: refused, and nothing was written first.
        (dst / "README.md").write_text("no rows\n", encoding="utf-8")
        held = tree_now()
        refused_with("--from with no sha256 rows in the README", "run of sha256 rows", lambda: regen(src, dst, said.append))
        if tree_now() != held:
            fails.append("--from with no sha256 rows in the README: the tree changed before the refusal")
        (dst / "README.md").write_bytes(before["README.md"])

        (dst / checker.LINES_FILE).write_bytes(b"{}\n")
        refused_with("--from with an unusable lines.json", "is unusable", lambda: regen(src, dst, said.append))

    good = document({"1.2": "supported", "1.3": "retired"})

    def changed(change) -> dict:
        d = json.loads(json.dumps(good))
        change(d)
        return d

    for name, doc, needle in (
        ("not an object", [], "not an object"),
        ("no schema", changed(lambda d: d.pop("schema")), "its schema"),
        ("schema 2", changed(lambda d: d.update(schema=2)), "its schema"),
        ("schema true", changed(lambda d: d.update(schema=True)), "its schema"),
        ("no repository", changed(lambda d: d.pop("repository")), "its repository"),
        ("a repository with a space", changed(lambda d: d.update(repository="a b")), "its repository"),
        ("no produced_at", changed(lambda d: d.pop("produced_at")), "its produced_at"),
        ("produced_at no time", changed(lambda d: d.update(produced_at="2026-02-30T00:00:00Z")), "its produced_at"),
        ("no lines", changed(lambda d: d.pop("lines")), "its lines"),
        ("a key that is no line", changed(lambda d: d["lines"].update(latest=d["lines"]["1.2"])), "not a line"),
        ("an entry that is a string", changed(lambda d: d["lines"].update({"1.2": "supported"})), "the entry for 1.2"),
        ("an entry with no state", changed(lambda d: d["lines"]["1.2"].pop("state")), "its state"),
        ("a state in other case", changed(lambda d: d["lines"]["1.2"].update(state="SUPPORTED")), "its state"),
        ("no since", changed(lambda d: d["lines"]["1.2"].pop("since")), "its since"),
        ("since not padded", changed(lambda d: d["lines"]["1.2"].update(since="2026-1-1")), "its since"),
        ("since no date", changed(lambda d: d["lines"]["1.2"].update(since="2026-02-30")), "its since"),
        ("no commit", changed(lambda d: d["lines"]["1.2"].pop("security_md_commit")), "its security_md_commit"),
        ("a commit of 41", changed(lambda d: d["lines"]["1.2"].update(security_md_commit="a" * 41)), "its security_md_commit"),
        ("nothing supported", document({"1.3": "retired"}), "no supported line"),
    ):
        refused_with(f"--lines-document, {name}", needle, lambda d=doc: lines_doc(d, tag))
    try:
        for doc in (good, changed(lambda d: d["lines"]["1.2"].update(security_md_commit="")), changed(lambda d: d.update(future=1))):
            lines_doc(doc, tag)
    except Refused as e:
        fails.append(f"--lines-document on a good document: {e}")
    refused_with("--lines-source outside the closed form", "is neither", lambda: lines_doc(good, "a note"))
    refused_with("--lines-source not the document's", "not the document's produced_at", lambda: lines_doc(good, tag2))

    runs = cli_selftest(fails, tag, tag2, lists, document, header, row)

    # A selftest that saw a failure ends the process itself: it is the one mode whose
    # status no selftest can read, and CI's whole trust in it is that status.
    if fails:
        for f in fails:
            print("SELFTEST FAIL:", f, file=sys.stderr)
        sys.exit(1)
    print(f"regen-where-settings selftest: ok ({runs} command-line runs, each process's status equal to its verdict line)")
    return 0


def cli_selftest(fails: list[str], tag: str, tag2: str, lists, document, header: str, row) -> int:
    """This file's real entry point in a subprocess, on a planted copy of the tree,
    run from that copy so ROOT and DIR are derived the way a real run derives them.
    What is asserted is the process's own status: nothing above reads it."""
    verdict_re = re.compile(r"^regen: verdict: .*\(exit ([0-9]+)\)$", re.M)
    runs = 0
    with tempfile.TemporaryDirectory(prefix="regen-where-settings-cli-") as tmp:
        base = Path(tmp)
        src = base / "producer"
        src.mkdir()
        producer = lists(["1.2", "1.3", "1.10"])
        for n, b in producer.items():
            (src / f"{n}.json").write_bytes(b)
        (src / checker.TSV).write_text(header + row("1.2") + row("1.3") + row("1.10"), encoding="utf-8")
        (src / checker.UNION).write_bytes(checker.derive_union([json.loads(producer[n]) for n in ("1.2", "1.3", "1.10")]))

        def fresh_tree(name: str) -> Path:
            """A tree regen has already written: lines 1.2 and 1.10."""
            root = base / name
            scripts = root / "scripts" / "abi-v2"
            scripts.mkdir(parents=True)
            shutil.copyfile(__file__, scripts / "regen-where-settings.py")
            shutil.copyfile(Path(__file__).resolve().parent / "check-where-settings.py", scripts / "check-where-settings.py")
            where = root / DIR.relative_to(ROOT)
            where.mkdir(parents=True)
            (where / checker.LINES_FILE).write_bytes(lines_doc(document({"1.2": "supported", "1.10": "supported"}), tag))
            (where / "README.md").write_text("- `x.json`: `" + "0" * 64 + "`\n", encoding="utf-8")
            if regen(src, where, lambda _m: None) != 0:
                fails.append("command line: the planted tree could not be written")
            return root

        def tree_of(root: Path) -> dict[str, bytes]:
            where = root / DIR.relative_to(ROOT)
            return {p.name: p.read_bytes() for p in where.iterdir()}

        def run(what: str, root: Path, args: list[str], want: int, expect: str = "") -> subprocess.CompletedProcess:
            nonlocal runs
            runs += 1
            r = subprocess.run(
                [sys.executable, "-I", str(root / "scripts" / "abi-v2" / "regen-where-settings.py"), *args],
                capture_output=True,
                text=True,
                cwd=tmp,
                timeout=120,
            )
            lines = verdict_re.findall(r.stderr)
            if r.returncode != want or (want != 2 and lines != [str(want)]):
                fails.append(f"command line {what!r}: status {r.returncode}, verdict lines {lines}, want {want}: {r.stderr[-300:]}")
            if expect not in r.stderr:
                fails.append(f"command line {what!r}: {expect!r} was not said")
            return r

        def unchanged(what: str, root: Path, args: list[str], expect: str) -> None:
            before = tree_of(root)
            run(what, root, args, 1, expect)
            if tree_of(root) != before:
                fails.append(f"command line {what!r}: exit 1, and the tree changed")

        def file(name: str, doc) -> str:
            path = base / name
            path.write_bytes(doc if isinstance(doc, bytes) else json.dumps(doc).encode())
            return str(path)

        # --from
        root = fresh_tree("from")
        before = tree_of(root)
        run("--from, nothing to change", root, ["--from", str(src)], 0, "lists copied")
        if tree_of(root) != before:
            fails.append("command line --from: a second run changed the tree")
        broken = base / "broken"
        shutil.copytree(src, broken)
        d = json.loads(producer["1.10"])
        d["settings"] = []
        (broken / "1.10.json").write_bytes((json.dumps(d, indent=1) + "\n").encode())
        unchanged("--from a list that fails a rule", root, ["--from", str(broken)], "(settings)")
        broken2 = base / "broken2"
        shutil.copytree(src, broken2)
        d = json.loads(producer["1.2"])
        d["settings"] = []
        (broken2 / "1.2.json").write_bytes((json.dumps(d, indent=1) + "\n").encode())
        gone = base / "gone"
        shutil.copytree(src, gone)
        (gone / "1.2.json").unlink()
        unchanged("--from a directory with a list missing", root, ["--from", str(gone)], "no list for 1.2")
        readme = root / DIR.relative_to(ROOT) / "README.md"
        kept = readme.read_bytes()
        readme.write_text("no rows\n", encoding="utf-8")
        unchanged("--from with no sha256 rows in the README", root, ["--from", str(src)], "nothing written")
        readme.write_bytes(kept)
        run("--from is required", root, [], 2)

        # --control
        run("--control on a matching union", root, ["--from", str(src), "--control"], 0, "union control OK")
        other = base / "other"
        shutil.copytree(src, other)
        (other / checker.UNION).write_bytes(b"{}\n")
        run("--control on a union that differs", root, ["--from", str(other), "--control"], 1, "union control FAILED")
        run("--control needs --from", root, ["--control"], 2)

        # --lines-document, without --from: lines.json and its row, the lines unmoved.
        same = file("same.json", document({"1.2": "supported", "1.3": "retired", "1.10": "supported"}, "2026-01-03T03:04:05Z"))
        root = fresh_tree("lines")
        run("--lines-document, the same lines", root, ["--lines-document", same, "--lines-source", tag2], 0, "wrote lines.json")
        if json.loads(tree_of(root)[checker.LINES_FILE])["source"] != tag2:
            fails.append("command line --lines-document: lines.json's source did not move")
        if checker.verdict(*checker.load(root / DIR.relative_to(ROOT)))[0] != 0:
            fails.append("command line --lines-document: reported success on a tree that fails --check")
        moved = file("moved.json", document({"1.2": "supported", "1.10": "retired"}, "2026-01-03T03:04:05Z"))
        unchanged("--lines-document, moved lines, no --from", root, ["--lines-document", moved, "--lines-source", tag2], "give --from")
        malformed = file("malformed.json", {"lines": {"1.2": {"state": "supported"}, "1.10": "supported"}})
        unchanged("--lines-document, a malformed document", root, ["--lines-document", malformed, "--lines-source", tag2], "the channel document")
        repeated = file("repeated.json", b'{"schema":1,"schema":1}')
        unchanged("--lines-document, a repeated member", root, ["--lines-document", repeated, "--lines-source", tag2], "could not be read")
        unchanged("--lines-document, no such file", root, ["--lines-document", str(base / "absent.json"), "--lines-source", tag2], "could not be read")
        unchanged("--lines-source outside the closed form", root, ["--lines-document", same, "--lines-source", "a note"], "is neither")
        # A source lines.json does not already name: writing it would change the tree.
        first = file("first.json", document({"1.2": "supported", "1.3": "retired", "1.10": "supported"}, "2026-01-04T03:04:05Z"))
        stray = root / DIR.relative_to(ROOT) / "extra.txt"
        stray.write_bytes(b"x\n")
        unchanged("--lines-document, a tree that would fail a rule", root, ["--lines-document", first, "--lines-source", "20260104T030405Z"], "(stray)")
        stray.unlink()
        run("--lines-document needs --lines-source", root, ["--lines-document", same], 2)
        run("--lines-source needs --lines-document", root, ["--from", str(src), "--lines-source", tag], 2, "--lines-source needs --lines-document")
        run("--control takes no --lines-source", root, ["--from", str(src), "--control", "--lines-source", tag], 2, "--control takes --from")

        # The latch turns one way, and the refusal leaves the tree as it was.
        root = fresh_tree("backward")
        three = {"1.2": "supported", "1.3": "retired", "1.10": "supported"}
        marker_doc = file("marker.json", document(three))
        old_doc = file("old.json", document(three, "2025-12-31T03:04:05Z"))
        for extra in ([], ["--from", str(src)]):
            unchanged("--lines-source the fixture marker over a tag", root, ["--lines-document", marker_doc, "--lines-source", checker.FIXTURE_SOURCE, *extra], "backward")
            unchanged("--lines-source an older tag", root, ["--lines-document", old_doc, "--lines-source", "20251231T030405Z", *extra], "backward")

        # A read or a write that fails ends in a verdict line, not a traceback.
        root = fresh_tree("io")
        nonutf = base / "nonutf"
        shutil.copytree(src, nonutf)
        (nonutf / checker.TSV).write_bytes(b"\xff\xfe\n")
        unchanged("--from, a producer TSV that is not UTF-8", root, ["--from", str(nonutf)], "UnicodeDecodeError")
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            changed = base / "changed"
            shutil.copytree(src, changed)
            d = json.loads(producer["1.2"])
            d["settings"][0]["reason"] = "moved"
            (changed / "1.2.json").write_bytes((json.dumps(d, indent=1) + "\n").encode())
            union = root / DIR.relative_to(ROOT) / checker.UNION
            union.chmod(0o444)
            try:
                run("--from, a target that cannot be written", root, ["--from", str(changed)], 1, "PermissionError")
            finally:
                union.chmod(0o644)

        # --lines-document, with --from: the lists move with it.
        root = fresh_tree("both")
        run("--lines-document and --from", root, ["--lines-document", moved, "--lines-source", tag2, "--from", str(src)], 0, "lists copied")
        tree = tree_of(root)
        if json.loads(tree[checker.LINES_FILE])["lines"] != ["1.2"] or "1.10.json" in tree:
            fails.append("command line --lines-document and --from: lines.json or the lists did not move")
        unchanged("--lines-document and --from, a list that fails a rule", root, ["--lines-document", moved, "--lines-source", tag2, "--from", str(broken2)], "(settings)")
    return runs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", type=Path, help="the producer's files")
    ap.add_argument("--control", action="store_true", help="prove the union's derivation against --from")
    ap.add_argument("--lines-document", type=Path, help="a channel document to write lines.json from")
    ap.add_argument("--lines-source", help="lines.json's `source`: the document's tag, or the fixture marker")
    ap.add_argument("--selftest", action="store_true", help="prove each mode on a temporary tree")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    def say(m: str) -> None:
        print(m, file=sys.stderr)

    status = run_mode(args, ap, say)
    say(f"regen: verdict: {'ok' if status == 0 else 'failed'} (exit {status})")
    return status


def run_mode(args: argparse.Namespace, ap: argparse.ArgumentParser, say) -> int:
    try:
        if args.control:
            if not args.src or args.lines_document or args.lines_source:
                ap.error("--control takes --from, and nothing else")
            return control(args.src, say)
        if not args.lines_document:
            if args.lines_source:
                ap.error("--lines-source needs --lines-document")
            if not args.src:
                ap.error("--from is required")
            return regen(args.src, DIR, say)
        if not args.lines_source:
            ap.error("--lines-document needs --lines-source")
        try:
            document = checker.strict_json(args.lines_document.read_bytes())
        except (OSError, ValueError) as e:
            raise Refused(f"{args.lines_document} could not be read: {e}") from e
        out = lines_doc(document, args.lines_source)
        if args.src:
            return regen(args.src, DIR, say, out)
        return relabel(DIR, say, out)
    except Refused as e:
        say(f"regen: nothing written: {e}")
        return 1
    except (OSError, UnicodeDecodeError) as e:
        say(f"regen: failed: {type(e).__name__}: {e}; read what is on disk with check-where-settings.py --check")
        return 1


if __name__ == "__main__":
    sys.exit(main())
