#!/usr/bin/env python3
"""PROBE (do not merge): a stress run of v1-cache-interop's concurrent-install
case (public issue #482), to measure a failure RATE per binding.

    probe-install-race.py --cli <binding>=<command> ... --rounds <binding>=<n>,...
                          --label <text> --json <out> [--control]

Each round is the interop job's own round: 8 processes of ONE binding,
released together by a barrier, each `fetch 26.8` into one fresh cache, with
the same read-only watcher. Unlike the job it keeps every failing process's
full stderr, groups failures by error code and by message (paths and
time/thread suffixes normalised), and lists stray installer names
(`.tmp-*`, `.stale-*`, `unpack-*`, `.staging-*`) left ANYWHERE in the cache,
inside the final entry included.

--control: the positive control. The command given must make every process
choose the same temporary names (the caller runs it under a frozen clock).
First ONE process alone must succeed (the frozen clock breaks nothing by
itself), then the rounds must show at least one failure, or this exits 1.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("cache_interop", HERE / "cache-interop.py")
assert _spec is not None and _spec.loader is not None
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)

STRAY = re.compile(r"^(\.tmp-|\.stale-|unpack-|\.staging-|tmp[a-z0-9_]{8}$)")
CODE = re.compile(r"CHTYPES_[A-Z_]+")


def normalise(msg: str, cache: Path) -> str:
    out = msg.replace(str(cache), "<cache>")
    out = re.sub(r"[0-9a-f]{64}", "<hex64>", out)
    out = re.sub(r"[0-9a-f]{8,}-ThreadId\(\d+\)", "<nanos>-<thread>", out)
    out = re.sub(r"\.(tmp|stale|staging)-[0-9]+-[0-9a-f]{12,}", r".\1-<pid>-<rand>", out)
    return out


def strays(cache: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(cache):
        for name in dirnames + filenames:
            if STRAY.match(name):
                rel = os.path.relpath(os.path.join(dirpath, name), cache)
                found.append(rel)
    return sorted(found)


def stress_round(h: Any, binding: str, n: int, cache: Path) -> dict[str, Any]:
    """ci.concurrent_round, keeping each failing process's whole stderr."""
    ready = cache.parent / f"{cache.name}.ready"
    barrier = cache.parent / f"{cache.name}.go"
    ready.mkdir()
    procs = []
    for i in range(n):
        argv = [*h.clis[binding], "fetch", ci.SPELLING, "--platform", ci.PLATFORM, "--cache", str(cache)]
        procs.append(subprocess.Popen(
            [sys.executable, "-c", ci.BARRIER, str(ready / str(i)), str(barrier), *argv],
            env=h.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    deadline = time.monotonic() + 120
    while len(os.listdir(ready)) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    watcher = ci.Watcher(cache)
    watcher.start()
    barrier.touch()
    results = []
    for p in procs:
        try:
            out, err = p.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        results.append(subprocess.CompletedProcess(p.args, p.returncode, out, err))
    watcher.stop.set()
    watcher.join()
    entries = [p for p in sorted((cache / "unpacked" / "sha256").glob("*")) if ci.HEX64.match(p.name)]
    failures: list[dict[str, Any]] = []
    installed = 0
    library_bytes = None
    for i, proc in enumerate(results):
        if proc.returncode != 0:
            text = " | ".join(ln.strip() for ln in proc.stderr.splitlines() if ln.strip())
            m = CODE.search(text)
            failures.append({
                "proc": i + 1,
                "exit": proc.returncode,
                "code": m.group(0) if m else "(no code)",
                "message": text[:2000],
                "normalised": normalise(text[:2000], cache),
            })
            continue
        entry = Path(ci.named_dir(proc))
        rec = ci.read_record(entry)
        if rec is None or len(entries) != 1 or os.path.realpath(entry) != os.path.realpath(entries[0]):
            failures.append({"proc": i + 1, "exit": 0, "code": "(wrong entry)", "message": f"named {str(entry)!r}",
                             "normalised": "named something other than the one final entry"})
            continue
        lib = entry / str(rec.get("library", ""))
        if not lib.is_file() or ci.sha256_file(lib) != rec.get("library_sha256"):
            failures.append({"proc": i + 1, "exit": 0, "code": "(bad library)", "message": f"{lib}",
                             "normalised": "the library is not its record's"})
            continue
        library_bytes = rec.get("library_bytes")
        installed += 1
    left = strays(cache)
    verified = h.run(binding, "verify", "--cache", str(cache))
    verify_fail = None if verified.returncode == 0 else f"exit {verified.returncode}: {verified.stderr.strip()[-300:]}"
    return {
        "installed": installed,
        "failures": failures,
        "events": dict(watcher.events),
        "strays": left,
        "verify": verify_fail,
        "library_bytes": library_bytes,
    }


def run_binding(h: Any, binding: str, rounds: int, n: int, work: Path) -> dict[str, Any]:
    total = Counter()
    by_code: dict[str, Counter[str]] = {}
    examples: list[dict[str, Any]] = []
    stray_rounds = 0
    stray_examples: list[list[str]] = []
    bad_rounds = 0
    verify_failures: list[str] = []
    library_bytes = None
    started = time.monotonic()
    for rnd in range(rounds):
        cache = work / f"{binding}-{rnd}"
        r = stress_round(h, binding, n, cache)
        total["attempts"] += n
        total["installed"] += r["installed"]
        total["failed"] += len(r["failures"])
        for f in r["failures"]:
            by_code.setdefault(f["code"], Counter())[f["normalised"]] += 1
            if len(examples) < 40:
                examples.append({"round": rnd + 1, **f})
        for k, v in r["events"].items():
            total[f"event:{k}"] += v
        if r["strays"]:
            stray_rounds += 1
            if len(stray_examples) < 10:
                stray_examples.append(r["strays"])
        if r["verify"]:
            verify_failures.append(f"round {rnd + 1}: {r['verify']}")
        if r["failures"] or r["events"] or r["strays"] or r["verify"]:
            bad_rounds += 1
        library_bytes = r["library_bytes"] or library_bytes
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(cache.parent / f"{cache.name}.ready", ignore_errors=True)
        (cache.parent / f"{cache.name}.go").unlink(missing_ok=True)
    return {
        "rounds": rounds,
        "processes_per_round": n,
        "attempts": total["attempts"],
        "installed": total["installed"],
        "failed": total["failed"],
        "by_code": {c: dict(m) for c, m in by_code.items()},
        "events": {k[6:]: v for k, v in total.items() if k.startswith("event:")},
        "stray_rounds": stray_rounds,
        "stray_examples": stray_examples,
        "verify_failures": verify_failures[:10],
        "bad_rounds": bad_rounds,
        "library_bytes": library_bytes,
        "seconds": round(time.monotonic() - started, 1),
        "examples": examples,
    }


def main() -> int:
    argv = sys.argv[1:]
    clis: dict[str, list[str]] = {}
    rounds: dict[str, int] = {}
    label = "probe"
    out_json: Path | None = None
    control = False
    n = ci.CONCURRENCY
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--cli":
            name, _, cmd = argv[i + 1].partition("=")
            clis[name] = shlex.split(cmd)
            i += 2
        elif a == "--rounds":
            for part in argv[i + 1].split(","):
                k, _, v = part.partition("=")
                rounds[k] = int(v)
            i += 2
        elif a == "--label":
            label = argv[i + 1]
            i += 2
        elif a == "--json":
            out_json = Path(argv[i + 1])
            i += 2
        elif a == "--processes":
            n = int(argv[i + 1])
            i += 2
        elif a == "--control":
            control = True
            i += 1
        else:
            print(f"probe: unknown argument {a!r}", file=sys.stderr)
            return 2
    constants = json.loads((ci.ROOT / "spec" / "fetch-v1" / "constants.json").read_text(encoding="utf-8"))
    trusted = constants["test_keys"][0]["ed25519_hex"]
    server = subprocess.Popen(
        [sys.executable, str(ci.ROOT / "scripts" / "fetch-v1" / "server.py"), "--fixtures", str(ci.FIXTURES), "--port", "0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    report: dict[str, Any] = {"label": label, "control": control, "bindings": {}}
    try:
        assert server.stdout is not None
        listening = server.stdout.readline().split()
        if len(listening) < 2 or listening[0] != "LISTENING":
            print(f"probe: the fixture server did not start: {listening}", file=sys.stderr)
            return 1
        base = f"http://127.0.0.1:{listening[1]}/s-{ci.CASE}/chtypes/v1"
        os.umask(ci.UMASK)
        work = Path(tempfile.mkdtemp(prefix="install-race-"))
        rc = 0
        for binding in [b for b in ci.BINDINGS if b in clis]:
            h = ci.Harness({binding: clis[binding]}, base, trusted, work)
            if control:
                solo = h.fetch(binding, ci.SPELLING, work / f"solo-{binding}", offline=False)
                report["solo"] = {"exit": solo.returncode, "stderr": solo.stderr.strip()[-500:], "stdout": solo.stdout.strip()[-300:]}
                print(f"probe: CONTROL solo {binding}: exit {solo.returncode} {solo.stderr.strip()[-300:]}", flush=True)
                if solo.returncode != 0:
                    print("probe: CONTROL INVALID: one process alone under the control command failed", file=sys.stderr)
                    rc = 1
            res = run_binding(h, binding, rounds.get(binding, 3), n, work)
            report["bindings"][binding] = res
            print(f"probe: {label} {binding}: {res['failed']}/{res['attempts']} failed, {res['bad_rounds']}/{res['rounds']} bad rounds, "
                  f"events {res['events'] or 'none'}, stray rounds {res['stray_rounds']}, library_bytes {res['library_bytes']}, {res['seconds']}s", flush=True)
            for code, msgs in res["by_code"].items():
                for msg, count in sorted(msgs.items(), key=lambda kv: -kv[1]):
                    print(f"probe:   {count} x {code}: {msg}", flush=True)
            for ex in res["stray_examples"][:3]:
                print(f"probe:   strays: {ex[:6]}", flush=True)
            for vf in res["verify_failures"][:3]:
                print(f"probe:   verify: {vf}", flush=True)
            if control and res["failed"] == 0 and res["bad_rounds"] == 0:
                print(f"probe: CONTROL FAILED: {binding} under a forced name collision showed nothing", file=sys.stderr)
                rc = 1
        if out_json is not None:
            out_json.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"### {label}\n\n| binding | failed / attempts | bad rounds | events | stray rounds | library bytes |\n|---|---|---|---|---|---|\n")
                for b, res in report["bindings"].items():
                    f.write(f"| {b} | {res['failed']} / {res['attempts']} | {res['bad_rounds']} / {res['rounds']} | {res['events'] or 'none'} | {res['stray_rounds']} | {res['library_bytes']} |\n")
                for b, res in report["bindings"].items():
                    for code, msgs in res["by_code"].items():
                        for msg, count in msgs.items():
                            f.write(f"\n- {b}: {count} x `{code}`: `{msg[:400]}`")
                f.write("\n\n")
        shutil.rmtree(work, ignore_errors=True)
        return rc
    finally:
        server.stdin and server.stdin.close()
        server.terminate()


if __name__ == "__main__":
    sys.exit(main())
