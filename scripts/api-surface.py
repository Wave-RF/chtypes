#!/usr/bin/env python3
"""api-surface.py — did a pull request change any binding's exported API?

    scripts/api-surface.py --selftest            the pure logic, offline, no tool needed
    scripts/api-surface.py --fixtures [BINDING…]  run each pinned tool on tests/fixtures/api-surface/
    scripts/api-surface.py run [--base SHA --head SHA] [--tools DIR]
                                                 CI: every fixture proof, then one verdict per touched binding

Run by ci.yml's non-blocking `api-surface` job (chtypes#285 §1). Its verdict
lines are read by scripts/policy-merge-check.py, which lets a pull request
that touches a binding's source enqueue itself only when that binding's
verdict on the judged head is `changed=false`. Read that file's header first:
it owns the decision, this file only measures.

WHAT IS COMPARED. The pull request's merge base with main (never main's tip:
main may have moved, and its changes are not this pull request's) against
the pull request's head, one binding at a time, and only for a binding whose
source the diff touches — the same path-to-binding mapping the checker uses,
imported from it (binding_of), so the two cannot disagree. An exported
ADDITION counts as a change, exactly like a removal or a signature change: a
new export is still an API change a human approves. Doc comments and
unexported code are not API.

THE TOOLS, ONE PER BINDING, EACH PINNED HERE AND NOWHERE ELSE:

  go      golang.org/x/exp/cmd/apidiff at APIDIFF_VERSION, module mode, over
          both builds the module has: the default (dlopen) one and
          `-tags chtypes_linked`, whose exported surface is larger. Any line
          apidiff prints, compatible or not, is a change.
  python  griffe at GRIFFE_VERSION, through `uv run --with` (uv's uvx).
          `griffe check` alone reports only BREAKING changes, so an addition
          — or a new optional parameter — would read as unchanged; this
          compares griffe's own loaded model of the public API instead
          (every public object, recursively, with its kind, signature, bases,
          annotation and module-level value; aliases resolved to what they
          name), and any difference is a change.
  ts      @microsoft/api-extractor at API_EXTRACTOR_VERSION, in report mode:
          one API report for the base and one for the head, compared as text
          once doc-comment-only markers are dropped. Forgotten exports are
          included, so a change to a non-exported type an export names is
          seen too.
  rust    cargo-public-api at CARGO_PUBLIC_API_VERSION, on the pinned
          RUST_NIGHTLY (rustdoc JSON format 57, which that version reads),
          with --all-features. cargo-semver-checks was the other candidate
          and was not used: it reports semver VIOLATIONS, and an addition is
          not one, so it cannot see the change this job exists to catch.

A FIXTURE PROOF RUNS FIRST, EVERY RUN, FOR ALL FOUR. Before any verdict,
each tool is run as invoked here on tests/fixtures/api-surface/<binding>/:
a base package and four variants of it — `unexported` (only unexported code
and doc comments differ) must read unchanged, and `added`, `removed` and
`signature` must each read changed. A tool that fails that proof prints no
`changed=false` for its binding, and the job goes red.

THE VERDICT. One line per binding, printed together at the very end of the
run, in the form scripts/policy-merge-check.py's api_surface_line() defines:

    chtypes-api-surface binding=<b> head=<40-hex head> changed=<true|false|error>

and, for a binding the diff does not touch, `... head=<sha> untouched`, which
is not a verdict at all. `error` — a tool that failed, a tree that could not
be built — is never `false`, and it makes this script exit 1, so the job is
red. Any genuine verdict, `true` included, leaves it green: a pull request
that changes the API on purpose is not a failure, only a manual merge.

NOTHING FROM THE JUDGED HEAD RUNS HERE. The verdict is trusted by a checker
that holds a write token, so the head's tree is only ever read: apidiff
type-checks Go without running it (GOWORK=off, so a head's go.work cannot
redirect the module), griffe parses Python statically (no inspection),
api-extractor reads declaration files, and TypeScript's compiler runs from
the BASE tree's install over the head's sources (`pnpm` never runs in the
head tree, so a head's .npmrc is never read); cargo is invoked from a
neutral directory, so no .cargo/config.toml of the head is read, and a crate
build script — the one thing cargo would run — is an unconditionally
protected path (rust/build.rs). Every tool also runs from a neutral working
directory. The head's own commit sha is in every verdict line, and a commit
cannot contain its own sha, so nothing in the judged tree can print a line
that passes for its verdict.

Exit status: 0 for a run whose every proof passed and every touched binding
got a genuine verdict; 1 for a failed proof, a tool error or a failed
selftest; 2 for a usage error.
"""

from __future__ import annotations

import difflib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "api-surface"

APIDIFF_VERSION = "v0.0.0-20260908205506-85c1c2202aba"   # golang.org/x/exp, cmd/apidiff
GRIFFE_VERSION = "2.3.0"
API_EXTRACTOR_VERSION = "7.59.3"
CARGO_PUBLIC_API_VERSION = "0.52.0"
# cargo-public-api 0.52 reads rustdoc JSON format 57, which nightlies emit
# from 2025-11-22 until format 58 landed on 2026-05-31 (measured from the
# rust-lang/rust history of src/rustdoc-json-types/lib.rs).
RUST_NIGHTLY = "nightly-2026-05-20"

# Each variant, and whether comparing it against `base` must read changed.
VARIANTS: tuple[tuple[str, bool], ...] = (
    ("unexported", False),
    ("added", True),
    ("removed", True),
    ("signature", True),
)


def _load_checker():
    """scripts/policy-merge-check.py, imported by path (its name has a
    hyphen). Registered in sys.modules before it runs: its dataclasses
    resolve their own annotations through it."""
    spec = importlib.util.spec_from_file_location("policy_merge_check", ROOT / "scripts" / "policy-merge-check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PMC = _load_checker()
BINDINGS: tuple[str, ...] = PMC.BINDINGS


class ToolError(Exception):
    """A tool failed, or a tree could not be prepared: never a verdict of
    `false`."""


@dataclass
class Ctx:
    tools: Path      # where installed tools are kept (cached across CI runs)
    work: Path       # this run's scratch directory
    neutral: Path    # an empty working directory every tool runs from

    def scratch(self, *parts: str) -> Path:
        p = self.work.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run one tool; a missing tool or a non-zero exit is a ToolError, with
    the tail of what it said."""
    try:
        proc = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ToolError(f"{cmd[0]} is not installed: {e}") from e
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-25:])
        raise ToolError(f"`{' '.join(str(c) for c in cmd[:4])} …` exited {proc.returncode}:\n{tail}")
    return proc


def text_diff(old: list[str], new: list[str], limit: int = 60) -> str:
    lines = list(difflib.unified_diff(old, new, "base", "head", lineterm="", n=0))
    if len(lines) > limit:
        lines = lines[:limit] + [f"… and {len(lines) - limit} more line(s)"]
    return "\n".join(lines)


# --------------------------------------------------------------------- go


def go_env(tags: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(GOWORK="off", GOTOOLCHAIN="local", GOFLAGS=f"-tags={tags}" if tags else "", GO111MODULE="on")
    return env


def apidiff_bin(ctx: Ctx) -> Path:
    path = ctx.tools / "apidiff" / APIDIFF_VERSION / "apidiff"
    if not path.exists():
        env = go_env("")
        env["GOBIN"] = str(path.parent)
        run(["go", "install", f"golang.org/x/exp/cmd/apidiff@{APIDIFF_VERSION}"], cwd=ctx.neutral, env=env)
    return path


def go_module_path(module_dir: Path) -> str:
    for line in (module_dir / "go.mod").read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\s*module\s+(\S+)\s*", line)
        if m:
            return m.group(1)
    raise ToolError(f"{module_dir}/go.mod declares no module")


def apidiff_changed(output: str) -> bool:
    """apidiff prints nothing at all when the two APIs are identical, and a
    `- <change>` line per change otherwise, compatible or not."""
    return any(line.startswith("- ") for line in output.splitlines())


def go_compare(ctx: Ctx, base: Path, head: Path, *, fixture: bool) -> tuple[bool, str]:
    base_mod, head_mod = (base, head) if fixture else (base / "go", head / "go")
    module = go_module_path(base_mod)
    if go_module_path(head_mod) != module:
        return True, f"the module path changed: {module} -> {go_module_path(head_mod)}"
    tool = apidiff_bin(ctx)
    reports = []
    for tags in ("",) if fixture else ("", "chtypes_linked"):
        export = ctx.scratch("go") / f"{base.name}-{head.name}-{tags or 'default'}.export"
        run([tool, "-m", "-w", export, module], cwd=base_mod, env=go_env(tags))
        out = run([tool, "-m", export, module], cwd=head_mod, env=go_env(tags)).stdout
        if apidiff_changed(out):
            reports.append(f"apidiff, {'-tags ' + tags if tags else 'the default build'}:\n{out.rstrip()}")
    return bool(reports), "\n".join(reports)


# ----------------------------------------------------------------- python


def griffe_snapshot(search_path: str, package: str) -> list[str]:
    """Runs INSIDE `uv run --with griffe==GRIFFE_VERSION` (see python_snapshot):
    one line per public object of `package`, found only under `search_path`
    and parsed statically. Public is griffe's own rule (`__all__` where a
    module has one, else no leading underscore); an alias — a re-export — is
    resolved and recorded under its PUBLIC path with what it names, so a
    signature change behind a re-export from a private module is seen."""
    import griffe  # noqa: PLC0415 — only importable inside uv's environment

    root = griffe.load(package, search_paths=[search_path], allow_inspection=False, resolve_aliases=True,
                       resolve_external=False, try_relative_path=False)
    lines: list[str] = []
    seen: set[str] = set()

    def describe(obj) -> str:
        parts = [obj.kind.value]
        labels = sorted(str(label) for label in (getattr(obj, "labels", None) or ()))
        if labels:
            parts.append("labels=" + ",".join(labels))
        if obj.is_function:
            parts.append(obj.signature())
        elif obj.is_class:
            parts.append("bases=(" + ", ".join(str(b) for b in obj.bases) + ")")
        elif obj.is_attribute:
            parts.append(f"annotation={obj.annotation}")
            # A module- or class-level value is part of what a caller sees; an
            # instance attribute's value is the right-hand side of an
            # assignment inside a method, which is implementation.
            if "instance-attribute" not in labels or "class-attribute" in labels:
                parts.append(f"value={obj.value}")
        elif obj.is_type_alias:
            parts.append(f"value={obj.value}")
        return " ".join(parts)

    def walk(obj, path: str) -> None:
        if path in seen:
            return
        seen.add(path)
        target = obj
        if obj.is_alias:
            try:
                target = obj.final_target
            except Exception:  # noqa: BLE001 — an external or unresolvable target is recorded by name
                lines.append(f"{path} unresolved-reexport {obj.target_path}")
                return
            if target.is_module:
                lines.append(f"{path} module-reexport {target.path}")
                return
        lines.append(f"{path} {describe(target)}")
        if target.is_module or target.is_class:
            for name, member in sorted(target.members.items()):
                if member.is_public:
                    walk(member, f"{path}.{name}")

    walk(root, root.path)
    return sorted(lines)


def python_snapshot(ctx: Ctx, search_path: Path, package: str) -> list[str]:
    cmd = ["uv", "run", "--no-project", "--no-config", "--quiet", "--with", f"griffe=={GRIFFE_VERSION}",
           "python3", Path(__file__).resolve(), "griffe-snapshot", search_path, package]
    out = run(cmd, cwd=ctx.neutral).stdout
    try:
        snapshot = json.loads(out)
    except json.JSONDecodeError as e:
        raise ToolError(f"griffe-snapshot printed something that is not its JSON: {e}") from e
    if not isinstance(snapshot, list) or not snapshot:
        raise ToolError(f"griffe found no public API at all in {package} under {search_path}")
    return snapshot


def python_compare(ctx: Ctx, base: Path, head: Path, *, fixture: bool) -> tuple[bool, str]:
    if fixture:
        old, new = python_snapshot(ctx, base, "apifix"), python_snapshot(ctx, head, "apifix")
    else:
        old = python_snapshot(ctx, base / "python" / "src", "chtypes")
        new = python_snapshot(ctx, head / "python" / "src", "chtypes")
    return old != new, text_diff(old, new) if old != new else ""


# --------------------------------------------------------------------- ts

TS_BUILD = "tsc -p tsconfig.json"


def normalize_api_report(text: str) -> list[str]:
    """An api-extractor report, minus what is not API: the `(undocumented)`
    marker it adds to a declaration with no doc comment (adding one is a
    docs change), and any warning line that carries a source position."""
    out = []
    for line in text.replace("\r\n", "\n").splitlines():
        if re.match(r"^// .*:\d+:\d+ - \(ae-", line):
            continue
        out.append(line.replace(" (undocumented)", ""))
    return out


def api_extractor_report(ctx: Ctx, project: Path, entry: Path, types: list[str], label: str) -> list[str]:
    out_dir, tmp_dir = ctx.scratch("ts", label, "report"), ctx.scratch("ts", label, "temp")
    config = {
        "projectFolder": str(project),
        "mainEntryPointFilePath": str(entry),
        "compiler": {"overrideTsconfig": {
            "compilerOptions": {"target": "ES2023", "lib": ["ES2023", "ESNext.Disposable"], "module": "NodeNext",
                                "moduleResolution": "NodeNext", "strict": True, "skipLibCheck": True,
                                "types": types},
            "files": [str(entry)],
        }},
        "apiReport": {"enabled": True, "reportFileName": "api", "reportFolder": str(out_dir),
                      "reportTempFolder": str(tmp_dir), "includeForgottenExports": True},
        "docModel": {"enabled": False},
        "dtsRollup": {"enabled": False},
        "tsdocMetadata": {"enabled": False},
        "messages": {
            "compilerMessageReporting": {"default": {"logLevel": "warning"}},
            "extractorMessageReporting": {"default": {"logLevel": "none", "addToApiReportFile": False}},
            "tsdocMessageReporting": {"default": {"logLevel": "none"}},
        },
    }
    config_path = ctx.scratch("ts", label) / "api-extractor.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    run(["pnpm", "dlx", f"@microsoft/api-extractor@{API_EXTRACTOR_VERSION}", "run", "--local", "--config",
         config_path], cwd=ctx.neutral)
    for folder in (tmp_dir, out_dir):
        report = folder / "api.api.md"
        if report.exists():
            return normalize_api_report(report.read_text(encoding="utf-8"))
    raise ToolError(f"api-extractor exited 0 but wrote no api.api.md for {label}")


def ts_build(ctx: Ctx, base_ts: Path, tree_ts: Path) -> None:
    """Emit tree_ts/dist/*.d.ts with the BASE tree's TypeScript, never by
    running a package manager in tree_ts (see the module docstring). The
    build command is replicated, so it must still be the one replicated."""
    build = json.loads((tree_ts / "package.json").read_text(encoding="utf-8")).get("scripts", {}).get("build")
    if build != TS_BUILD:
        raise ToolError(f"ts/package.json's build script is {build!r}, not {TS_BUILD!r}, which this tool "
                        "replicates; update scripts/api-surface.py with it")
    if tree_ts != base_ts:
        (tree_ts / "node_modules").symlink_to(base_ts / "node_modules", target_is_directory=True)
    run([base_ts / "node_modules" / ".bin" / "tsc", "-p", tree_ts / "tsconfig.json"], cwd=ctx.neutral)


def ts_compare(ctx: Ctx, base: Path, head: Path, *, fixture: bool) -> tuple[bool, str]:
    if fixture:
        old = api_extractor_report(ctx, base, base / "index.d.ts", [], "fixture-base")
        new = api_extractor_report(ctx, head, head / "index.d.ts", [], f"fixture-{head.name}")
    else:
        base_ts, head_ts = base / "ts", head / "ts"
        # The base is main's own history, so installing it is no different
        # from what every ts job on main does; scripts are not needed for
        # type declarations and are not run.
        run(["pnpm", "install", "--frozen-lockfile", "--ignore-scripts"], cwd=base_ts)
        ts_build(ctx, base_ts, base_ts)
        ts_build(ctx, base_ts, head_ts)
        old = api_extractor_report(ctx, base_ts, base_ts / "dist" / "index.d.ts", ["node"], "base")
        new = api_extractor_report(ctx, head_ts, head_ts / "dist" / "index.d.ts", ["node"], "head")
    return old != new, text_diff(old, new) if old != new else ""


# ------------------------------------------------------------------- rust


def cargo_public_api_root(ctx: Ctx) -> Path:
    root = ctx.tools / "cargo-public-api" / CARGO_PUBLIC_API_VERSION
    run(["rustup", "toolchain", "install", RUST_NIGHTLY, "--profile", "minimal", "--no-self-update"], cwd=ctx.neutral)
    if not (root / "bin" / "cargo-public-api").exists():
        run(["cargo", "install", "cargo-public-api", "--locked", "--version", CARGO_PUBLIC_API_VERSION,
             "--root", root], cwd=ctx.neutral)
    return root


def rust_public_api(ctx: Ctx, manifest: Path, root: Path) -> list[str]:
    env = dict(os.environ)
    env["PATH"] = f"{root / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["CARGO_TARGET_DIR"] = str(ctx.scratch("rust", "target"))
    out = run(["cargo", f"+{RUST_NIGHTLY}", "public-api", "--manifest-path", manifest, "--all-features", "-s",
               "--color=never"], cwd=ctx.neutral, env=env).stdout
    items = [line for line in out.splitlines() if line.strip()]
    if not items:
        raise ToolError(f"cargo-public-api listed nothing at all for {manifest}")
    return items


def rust_compare(ctx: Ctx, base: Path, head: Path, *, fixture: bool) -> tuple[bool, str]:
    root = cargo_public_api_root(ctx)
    rel = Path("Cargo.toml") if fixture else Path("rust") / "Cargo.toml"
    old, new = rust_public_api(ctx, base / rel, root), rust_public_api(ctx, head / rel, root)
    return old != new, text_diff(old, new) if old != new else ""


COMPARE = {"go": go_compare, "python": python_compare, "ts": ts_compare, "rust": rust_compare}
# What of a revision each binding's comparison needs extracted.
TREE_PATHS = {"go": ("go", "include"), "python": ("python/src",), "ts": ("ts",), "rust": ("rust",)}


# ------------------------------------------------------- fixtures, verdicts


def verdict_of(compare) -> tuple[str, str]:
    """`true`/`false` from a comparison that ran, `error` from one that did
    not — a missing tool and a failed one alike."""
    try:
        changed, detail = compare()
    except (ToolError, OSError) as e:
        return "error", str(e)
    return ("true" if changed else "false"), detail


def prove(ctx: Ctx, binding: str) -> list[str]:
    """Every way `binding`'s tool, as invoked here, gets the fixtures wrong —
    [] when `unexported` reads unchanged and the other three read changed."""
    copy = ctx.work / "fixtures" / binding
    shutil.copytree(FIXTURES / binding, copy, dirs_exist_ok=True)
    problems = []
    for variant, want in VARIANTS:
        verdict, detail = verdict_of(lambda v=variant: COMPARE[binding](ctx, copy / "base", copy / v, fixture=True))
        print(f"  {binding} base -> {variant}: {'changed' if verdict == 'true' else 'unchanged' if verdict == 'false' else 'ERROR'}")
        for line in detail.splitlines()[:20]:
            print(f"      {line}")
        if verdict == "error":
            problems.append(f"{binding} fixture `{variant}`: the tool failed: {detail}")
        elif (verdict == "true") != want:
            problems.append(f"{binding} fixture `{variant}` read {'changed' if verdict == 'true' else 'unchanged'}, "
                            f"must read {'changed' if want else 'unchanged'}" + (f":\n{detail}" if detail else ""))
    return problems


def verdict_block(head: str, touched: set[str], verdicts: dict[str, str]) -> list[str]:
    """The lines printed last: a verdict per touched binding (api_surface_line,
    the checker's own definition) and a non-verdict line per untouched one."""
    return [PMC.api_surface_line(b, head, verdicts.get(b, "error")) if b in touched
            else f"{PMC.API_SURFACE_PREFIX} binding={b} head={head} untouched" for b in BINDINGS]


def git(*args: str) -> str:
    return run(["git", "-C", ROOT, *args], cwd=ROOT).stdout


def touched_in(paths: list[str]) -> set[str]:
    return {g.binding for g in (PMC.binding_of(p) for p in paths) if g is not None}


def extract(rev: str, binding: str, dest: Path) -> Path:
    """The files of `rev` a comparison of `binding` needs, written under
    `dest` by `git archive` — no worktree, no checkout of the head."""
    dest.mkdir(parents=True, exist_ok=True)
    tar_path = dest.parent / f"{dest.name}.tar"
    with open(tar_path, "wb") as f:
        proc = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", rev, "--", *TREE_PATHS[binding]],
                              stdout=f, stderr=subprocess.PIPE, text=False)
    if proc.returncode != 0:
        raise ToolError(f"git archive {rev[:12]} failed: {proc.stderr.decode(errors='replace').strip()}")
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter="data")
    return dest


SHA40 = re.compile(r"[0-9a-f]{40}")


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def cmd_run(base: str, head: str, tools: Path, only: tuple[str, ...] | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="api-surface-") as tmp:
        ctx = Ctx(tools=tools, work=Path(tmp), neutral=Path(tmp) / "neutral")
        ctx.neutral.mkdir()
        proofs: dict[str, list[str]] = {}
        for b in only or BINDINGS:
            print(f"::group::fixture proof — {b}", flush=True)
            proofs[b] = prove(ctx, b)
            for p in proofs[b]:
                print(f"  {p}")
            print("::endgroup::")
            print(f"api-surface: fixture proof for {b}: {'FAILED' if proofs[b] else 'ok'} (unexported unchanged; "
                  "added, removed and signature changed)", flush=True)
        failed = sorted(b for b, p in proofs.items() if p)
        if only is not None or not (base or head):
            if only is None:
                print("api-surface: no pull request or merge group to judge here (no base and head); the fixture "
                      "proofs are this run's whole result")
            summary(["### api-surface — fixture proofs", ""] + [f"- {b}: {'FAILED' if proofs[b] else 'ok'}"
                                                                 for b in proofs])
            return 1 if failed else 0
        if not (SHA40.fullmatch(base or "") and SHA40.fullmatch(head or "")):
            print(f"api-surface: --base and --head must both be 40-hex commit shas, got {base!r} and {head!r}",
                  file=sys.stderr)
            return 2
        merge_base = git("merge-base", base, head).strip()
        changed_paths = [p for p in git("diff", "--name-only", "--no-renames", "-z", merge_base, head).split("\0") if p]
        touched = touched_in(changed_paths)
        print(f"api-surface: merge base {merge_base}, head {head}; bindings touched: {', '.join(sorted(touched)) or 'none'}")
        verdicts: dict[str, str] = {}
        details: dict[str, str] = {}
        for b in BINDINGS:
            if b not in touched:
                continue
            if proofs.get(b):
                verdicts[b], details[b] = "error", "its fixture proof failed above, so no verdict from it is trusted"
                continue

            def compare(b=b):
                base_tree = extract(merge_base, b, ctx.work / "trees" / b / "base")
                head_tree = extract(head, b, ctx.work / "trees" / b / "head")
                return COMPARE[b](ctx, base_tree, head_tree, fixture=False)

            print(f"::group::{b} — merge base against head", flush=True)
            try:
                verdicts[b], details[b] = verdict_of(compare)
            except Exception as e:  # noqa: BLE001 — anything unexpected is an error verdict, never `false`
                verdicts[b], details[b] = "error", f"{type(e).__name__}: {e}"
            print(details[b] or "(no difference)")
            print("::endgroup::")
        block = verdict_block(head, touched, verdicts)
        summary(["### api-surface — merge base against head", "", "| binding | verdict |", "| --- | --- |"]
                + [f"| {b} | {'changed=' + verdicts[b] if b in touched else 'untouched'} |" for b in BINDINGS]
                + ["", "Fixture proofs: " + ", ".join(f"{b} {'FAILED' if proofs[b] else 'ok'}" for b in BINDINGS)])
        print("api-surface: the verdicts (scripts/policy-merge-check.py reads these lines):", flush=True)
        for line in block:
            print(line)
        sys.stdout.flush()
        errored = sorted(b for b, v in verdicts.items() if v == "error")
        if failed or errored:
            print(f"api-surface: red — fixture proof failed for {failed or 'none'}, tool error for "
                  f"{errored or 'none'}; neither is ever read as unchanged", file=sys.stderr)
            return 1
        return 0


# ----------------------------------------------------------------- selftest


def selftest() -> int:
    failures: list[str] = []
    head = "c" * 40

    # The verdict block round-trips through the checker's own reader, behind
    # GitHub's timestamp prefix; an untouched binding is not a verdict.
    block = verdict_block(head, {"go", "rust"}, {"go": "false", "rust": "true"})
    log = "\ufeff" + "\n".join(f"2026-10-01T00:49:13.2543384Z {line}" for line in ["noise"] + block) + "\n"
    if PMC.parse_api_surface(log, head) != {"go": ["false"], "rust": ["true"]}:
        failures.append(f"verdict_block did not round-trip through parse_api_surface: {PMC.parse_api_surface(log, head)}")
    if PMC.parse_api_surface(log, "d" * 40):
        failures.append("a verdict for one head was read as a verdict for another")
    if verdict_block(head, {"ts"}, {})[BINDINGS.index("ts")] != PMC.api_surface_line("ts", head, "error"):
        failures.append("a touched binding with no comparison result was not printed as changed=error")

    # Which bindings a diff touches: the checker's own mapping.
    if touched_in(["go/chtypes/transform.go", "go/chtypes/transform_test.go", "README.md"]) != {"go"}:
        failures.append("touched_in: a Go source file was not read as go, or a test file was")
    if touched_in(["python/tests/test_x.py", "ts/test/x.test.ts", "rust/tests/x.rs", "docs/x.md"]):
        failures.append("touched_in: a test or doc path was read as binding source")
    if touched_in(["python/src/chtypes/fetch.py", "rust/src/fetch/trust.rs"]) != {"python", "rust"}:
        failures.append("touched_in: a carve-out file must still name its binding (the verdict is still printed)")

    # A comparison that ran gives true/false; one that could not — the tool
    # missing, the tool failing — gives error, never false.
    def missing():
        run(["chtypes-api-surface-no-such-tool"], cwd=ROOT)
        return False, ""

    def failing():
        raise ToolError("exited 1")

    if verdict_of(lambda: (True, "x"))[0] != "true" or verdict_of(lambda: (False, ""))[0] != "false":
        failures.append("verdict_of: a comparison that ran was misread")
    for label, fn in (("a missing tool", missing), ("a failing tool", failing)):
        if verdict_of(fn)[0] != "error":
            failures.append(f"verdict_of: {label} was not an error verdict")

    # Each tool's output, read the way the comparisons read it.
    if apidiff_changed(""):
        failures.append("apidiff_changed: empty output (identical APIs) read as changed")
    for out in ("Compatible changes:\n- Farewell: added\n", "Incompatible changes:\n- Greet: removed\n"):
        if not apidiff_changed(out):
            failures.append(f"apidiff_changed: {out!r} read as unchanged")
    report = "## API Report\n\n```ts\n// @public (undocumented)\nexport function greet(name: string): string;\n```\n"
    documented = report.replace(" (undocumented)", "")
    if normalize_api_report(report) != normalize_api_report(documented):
        failures.append("normalize_api_report: adding a doc comment read as an API change")
    if normalize_api_report(report) == normalize_api_report(report.replace("name: string", "name: number")):
        failures.append("normalize_api_report: a signature change was normalized away")
    positioned = report + "// dist/index.d.ts:12:5 - (ae-forgotten-export) The symbol \"X\" needs to be exported\n"
    if normalize_api_report(positioned) != normalize_api_report(report):
        failures.append("normalize_api_report: a warning carrying a source position survived")

    # Every binding has every fixture variant on disk.
    for b in BINDINGS:
        for variant in ("base",) + tuple(v for v, _ in VARIANTS):
            d = FIXTURES / b / variant
            if not d.is_dir() or not any(p.is_file() for p in d.rglob("*")):
                failures.append(f"fixture {b}/{variant} is missing")
    if set(COMPARE) != set(BINDINGS) or set(TREE_PATHS) != set(BINDINGS):
        failures.append(f"COMPARE/TREE_PATHS name {sorted(COMPARE)}, the checker names {sorted(BINDINGS)}")
    # CONTRIBUTING.md names every pin; a pin bumped here without it fails here.
    guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for pin in (APIDIFF_VERSION, GRIFFE_VERSION, API_EXTRACTOR_VERSION, CARGO_PUBLIC_API_VERSION, RUST_NIGHTLY):
        if f"`{pin}`" not in guide:
            failures.append(f"CONTRIBUTING.md does not name the pinned `{pin}`; update its policy-merge section")

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print("api-surface: selftest ok — the verdict block round-trips through the checker's reader behind GitHub's "
          "timestamp and names only its own head, an untouched binding prints no verdict and a touched one with no "
          "result prints changed=error; a missing or failing tool is an error verdict, never false; apidiff's "
          "output, an api-extractor report's doc-only marker and a positioned warning are read as intended; every "
          "binding has its five fixture trees; CONTRIBUTING.md names every pin")
    return 0


# --------------------------------------------------------------------- main


def default_tools() -> Path:
    return Path(os.environ.get("CHTYPES_API_SURFACE_TOOLS") or Path.home() / ".cache" / "chtypes" / "api-surface")


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    if argv[:1] == ["griffe-snapshot"] and len(argv) == 3:
        print(json.dumps(griffe_snapshot(argv[1], argv[2])))
        return 0
    if argv[:1] == ["--fixtures"]:
        only = tuple(argv[1:]) or BINDINGS
        unknown = [b for b in only if b not in BINDINGS]
        if unknown:
            print(f"api-surface: unknown binding(s) {unknown}; the bindings are {list(BINDINGS)}", file=sys.stderr)
            return 2
        return cmd_run("", "", default_tools(), only=only)
    if argv[:1] == ["run"]:
        args = dict(base="", head="", tools=str(default_tools()))
        rest = argv[1:]
        while rest:
            flag = rest.pop(0)
            if flag not in ("--base", "--head", "--tools") or not rest:
                print(f"api-surface: unexpected argument {flag!r}", file=sys.stderr)
                return 2
            args[flag[2:]] = rest.pop(0)
        return cmd_run(args["base"], args["head"], Path(args["tools"]))
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
