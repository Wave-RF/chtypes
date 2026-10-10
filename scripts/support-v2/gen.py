#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["backports.zstd>=1.7; python_version < '3.14'"]
# ///
r"""gen.py: generate docs/support-v2.md, the verified v2 support matrix (public issue #438).

    uv run scripts/support-v2/gen.py              print the page to stdout
    uv run scripts/support-v2/gen.py --write      write docs/support-v2.md
    uv run scripts/support-v2/gen.py --check      the committed page must equal the output
    uv run scripts/support-v2/gen.py --selftest   prove every rule below on a fixture registry

WHERE THE FACTS COME FROM. The Python binding's ACTIVE generation-2 channel
decides the registry, the key and the generation (`chtypes._ocifetch._channel.
active()`): the staging dev channel today, production `chtypes/v2` with the
release key after the lock change flips `active()`, with no change here. This
script reads those three values and nothing else about the channel; an
environment variable cannot redirect it (the base and the keys are passed as
explicit options, which beat the environment on a channel that honors it).

VERIFY BEFORE USE. Every line the registry's tag listing names is resolved
with the fetch layer's own `resolve_each` (the code behind `chtypes resolve`):
the index, each platform's manifest and its signed statement are checked
exactly as a fetch checks them, and no layer is downloaded. A fact is read only
from a statement that verified. A platform that does not verify is OMITTED, and
is named on stderr by line, platform and error code (never the registry's own
words, which can echo a field's value). A platform the index does not offer is
shown as `not published`, which is not `unsupported`.

NOTHING PRIVATE. Every field the page shows must match its format (version,
build id, glibc floor), and no string anywhere in a verified predicate may match
a private-name pattern; a row that fails either is omitted like an unverified
one. Messages never carry a predicate's value.

DETERMINISM. No timestamp, no generated-at line, no host name beyond the
channel's own base: the same registry state gives the same bytes.

EXIT STATUSES
    0  the page is complete (and, with --check, equals the committed page)
    1  --check: the committed page differs from the output
    2  usage error
    3  the page was produced but some rows were omitted (named on stderr);
       --check does not compare, and fails
    4  nothing was written: the tag listing was unreadable, named no line, or
       no platform of any line verified
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from chtypes import __main__ as cli  # noqa: E402
from chtypes._ocifetch import (  # noqa: E402
    Options,
    Request,
    _channel,  # noqa: E402
    resolve_each,
)
from chtypes._ocifetch import _constants as C  # noqa: E402
from chtypes._ocifetch._dsse import trusted_keys_from_hex  # noqa: E402
from chtypes._ocifetch._errors import FetchError  # noqa: E402

PAGE = ROOT / "docs" / "support-v2.md"

EXIT_OK = 0
EXIT_DIFF = 1
EXIT_PARTIAL = 3
EXIT_NOTHING = 4

LINE_RE = re.compile(r"^[0-9]+\.[0-9]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
BUILD_RE = re.compile(r"^[0-9]{8}\.[0-9]{6}$")
GLIBC_RE = re.compile(r"^[0-9]+\.[0-9]+(\.[0-9]+)?$")

# The selftest's planted strings, one per pattern below, built at run time.
PLANTED_NAME = "chtypes-" + "zz"
PLANTED_PATH = "/" + "Users" + "/someone/lib.so"

# Shapes that point into the private half or at a developer's machine. The page
# is also scanned by scripts/lint-public.sh, whose rules these mirror in kind;
# this is the generator's own refusal, so a planted string never reaches a file.
PRIVATE_PATTERNS = (
    re.compile(r"chtypes-[A-Za-z]", re.IGNORECASE),
    re.compile(r"wave-rf", re.IGNORECASE),
    re.compile(r"/(?:Users|home)/"),
    re.compile(r"(?<![A-Za-z0-9_])core#[0-9]"),
    re.compile(r"(?<![A-Za-z0-9_])(?:ci|infra|notes)/"),
    re.compile(r"\b(?:AUDIT|HANDOFF|NOTE|PROMPT|REVIEW)-[A-Za-z0-9-]+\.md\b"),
)


# --------------------------------------------------------------------- channel


@dataclass(frozen=True)
class Context:
    """What the page is generated from, all of it taken from a channel."""

    name: str
    abi: int
    base: str
    key_ids: tuple[str, ...]
    production: bool


def channel_context(channel: _channel.Channel | None = None) -> Context:
    """The base, trust and generation of `channel`, default the ACTIVE one."""
    channel = channel or _channel.active()
    if len(channel.bases) != 1:
        raise ValueError(f"the {channel.name} channel names {len(channel.bases)} bases, want 1")
    return Context(
        name=channel.name,
        abi=channel.abi,
        base=channel.bases[0],
        key_ids=tuple(k["keyid"] for k in channel.keys),
        production=channel.name == C.PROD_V2_NAME,
    )


def channel_options(cache: str, channel: _channel.Channel | None = None) -> Options:
    """Options for the channel's own base and keys. A channel that honors the
    base and trust overrides (production) gets them pinned explicitly, so no
    environment variable can redirect the run; one that ignores them (dev) is
    left to use its own, and is not asked to warn. An empty cache and no system
    directory keep anything local from changing an answer."""
    channel = channel or _channel.active()
    pinned = {}
    if channel.overridable:
        pinned = {"bases": channel.bases, "trusted_keys": _channel.trusted_keys(channel)}
    return Options(cache_dir=cache, system_dirs=(), platform=C.PLATFORMS[0]["key"], **pinned)


# ---------------------------------------------------------------------- facts


@dataclass(frozen=True)
class Row:
    platform: str
    version: str = ""
    build: str = ""
    glibc: str = ""
    published: bool = True


@dataclass
class Collected:
    lines: dict[str, list[Row]] = field(default_factory=dict)
    omitted: list[str] = field(default_factory=list)
    fatal: str = ""

    @property
    def verified_rows(self) -> int:
        return sum(1 for rows in self.lines.values() for r in rows if r.published)


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _refusal(line: str, platform: str, predicate: dict) -> str:
    """Why a verified predicate may not be shown, or "" when it may. Names the
    field, never its value."""
    for name, value in predicate.items():
        if any(p.search(s) for s in _strings(value) for p in PRIVATE_PATTERNS) or any(
            p.search(str(name)) for p in PRIVATE_PATTERNS
        ):
            return f"a field ({name!r}) matched the private-name filter"
    version = predicate.get("clickhouse_version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        return "its version is not a four-part ClickHouse version"
    if not version.startswith(line + "."):
        return "its version does not belong to the line"
    build = predicate.get("build")
    if not isinstance(build, str) or not BUILD_RE.fullmatch(build):
        return "its build id is not a build id"
    floor = predicate.get("glibc_floor")
    if floor is not None and not (isinstance(floor, str) and GLIBC_RE.fullmatch(floor)):
        return "its glibc floor is not a glibc version"
    return ""


def collect(options: Options, say) -> Collected:
    out = Collected()
    try:
        tags = cli._published_tags(options)
    except FetchError as exc:
        out.fatal = f"the registry's tag listing could not be read ({getattr(exc, 'code', '')})"
        return out
    lines = sorted({t for t in tags if LINE_RE.fullmatch(t)}, key=lambda t: _num(t))
    if not lines:
        out.fatal = "the registry's tag listing names no line"
        return out
    for line in lines:
        try:
            outcomes = resolve_each(Request(line), options)
        except FetchError as exc:
            out.omitted.append(f"{line}: omitted, the line could not be resolved ({exc.code})")
            continue
        rows: list[Row] = []
        for o in outcomes:
            if o.error is not None and o.error.code == "CHTYPES_ARTIFACT_UNPUBLISHED":
                # The statement verified and says no build answers this SDK's
                # ABI here: absent, which is not unsupported.
                rows.append(Row(o.platform, published=False))
            elif o.error is not None:
                out.omitted.append(
                    f"{line} {o.platform}: omitted, its statement did not verify ({o.error.code})"
                )
            elif o.resolution is None:
                rows.append(Row(o.platform, published=False))
            else:
                predicate = dict(o.resolution.predicate)
                why = _refusal(line, o.platform, predicate)
                if why:
                    out.omitted.append(f"{line} {o.platform}: omitted, {why}")
                    continue
                rows.append(
                    Row(
                        o.platform,
                        version=predicate["clickhouse_version"],
                        build=predicate["build"],
                        glibc=predicate.get("glibc_floor") or "",
                    )
                )
        if any(r.published for r in rows):
            out.lines[line] = rows
        else:
            out.omitted.append(f"{line}: omitted, no platform of the line verified")
    return out


def _num(spelling: str) -> tuple[int, ...]:
    return tuple(int(p) for p in spelling.split("."))


# ----------------------------------------------------------------------- page


def _languages() -> list[tuple[str, str, str, str]]:
    """Binding, package, requirement and loader, read from each binding's own
    manifest so the page cannot drift from them."""
    go_mod = (ROOT / "go" / "go.mod").read_text(encoding="utf-8")
    go_module = re.search(r"^module\s+(\S+)", go_mod, re.M).group(1)
    go_min = re.search(r"^go\s+(\S+)", go_mod, re.M).group(1)
    py = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    py_min = py["requires-python"].removeprefix(">=")
    ts = json.loads((ROOT / "ts" / "package.json").read_text(encoding="utf-8"))
    node_min = ts["engines"]["node"].removeprefix(">=")
    esm = ", ESM only" if ts.get("type") == "module" else ""
    rs = tomllib.loads((ROOT / "rust" / "Cargo.toml").read_text(encoding="utf-8"))["package"]
    rs_min = rs["rust-version"]
    edition = rs["edition"]
    return [
        ("Go", f"`{go_module}`", f"Go {go_min}+", "cgo + `dlopen`"),
        ("Python", f"`{py['name']}`", f"Python {py_min}+", "stdlib `ctypes` (no build step)"),
        ("TypeScript", f"`{ts['name']}`", f"Node {node_min}+{esm}", "`ffi-rs` (prebuilt)"),
        (
            "Rust",
            f"`{rs['name']}`",
            f"Rust {rs_min}+ (edition {edition})",
            "`libloading`",
        ),
    ]


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]

    def fmt(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)) + " |"

    return [fmt(header), "| " + " | ".join("-" * w for w in widths) + " |", *map(fmt, rows)]


def render(ctx: Context, data: Collected) -> str:
    where = urlsplit(ctx.base)
    repo = where.path.strip("/")
    out: list[str] = ["# What chtypes supports in v2", ""]
    out += [
        "<!-- Generated by scripts/support-v2/gen.py. Do not edit: regenerate it. -->",
        "",
    ]
    if ctx.production:
        out += [
            f"This page is generated from the production registry (`{repo}` on "
            f"`{where.netloc}`), every statement verified against the release key "
            f"(`{', '.join(ctx.key_ids)}`) before a fact was read from it.",
            "",
        ]
    else:
        out += [
            f"**This page is generated from the development channel** (`{repo}` on "
            f"`{where.netloc}`), every statement verified against that channel's own key "
            f"(`{', '.join(ctx.key_ids)}`) before a fact was read from it. A development "
            "listing is replaceable and is not a support promise; the page switches to the "
            "production registry when v2 is locked.",
            "",
        ]
    out += [
        "Three independent axes, and a combination works only if all three do: the "
        "**language** you call from, the **platform** you run on, and the **ClickHouse line** "
        "you want answers for. Where this page does not know something, it says **support "
        "unknown**, which is not the same as unsupported.",
        "",
        "## Languages",
        "",
    ]
    out += _table(
        ["Binding", "Package", "Requires", "Loads the library with"],
        [list(r) for r in _languages()],
    )
    out += [
        "",
        "All four loaders are `dlopen`, so all four bindings are Unix-only. There is no Windows "
        "library and no 32-bit build.",
        "",
        "## SDK and ABI",
        "",
        f"Every v2 SDK speaks ABI generation {ctx.abi}: the `chs_*` function table described "
        "under [`spec/abi-v2/`](../spec/abi-v2/). A library is matched to an SDK by the ABI "
        "fingerprint its signed statement carries, and the loader refuses a mismatch naming "
        "both sides. The fingerprint belongs to a build of the SDK, so this page states none: "
        "a copy here could only drift from it.",
        "",
        "## Platforms",
        "",
        "The library is native code, so a platform works only if the registry serves a build "
        "for it. The platforms a fetch recognizes are fixed by "
        "[`spec/fetch-v1/constants.json`](../spec/fetch-v1/constants.json):",
        "",
    ]
    out += [f"- `{p['key']}`" for p in C.PLATFORMS]
    out += [
        "",
        "A host on any other platform gets `CHTYPES_ARTIFACT_UNPUBLISHED` from a fetch, naming "
        "what the registry does offer. On Linux the library needs a glibc at or above the "
        "floor its own build records, carried in its signed statement and checked by the "
        "loader; the floor is shown per build below, and a host under it is refused with "
        "`CHTYPES_ARTIFACT_INCOMPATIBLE`. musl-based images are in the same position as any "
        "platform not listed: support unknown until a build says otherwise.",
        "",
        "## ClickHouse lines",
        "",
        "One library per ClickHouse line, each carrying that release's own C++. The tables "
        "below list every line the registry publishes, with the exact four-part ClickHouse "
        "version and the build id the line resolves to on each platform, each read from a "
        "verified signed statement. A platform shown as `not published` has no build on this "
        "channel; that says nothing about whether the line works there. A statement that "
        "failed verification is left out, never shown.",
        "",
    ]
    if not data.lines:
        out += ["No line has a verified build on this channel.", ""]
    for line in sorted(data.lines, key=_num, reverse=True):
        out += [f"### {line}", ""]
        rows = []
        for r in data.lines[line]:
            if r.published:
                rows.append(
                    [f"`{r.platform}`", f"`{r.version}`", f"`{r.build}`", r.glibc or "not recorded"]
                )
            else:
                rows.append([f"`{r.platform}`", "not published", "", ""])
        out += _table(["Platform", "ClickHouse version", "Build", "glibc floor"], rows)
        out.append("")
    out += [
        'Ask for a line, never a nearest match: `for("26.8")` resolves the newest build of '
        "that line and fails if it is absent, rather than quietly handing back a neighbor "
        "whose answers differ. List what the registry publishes with the CLI of any binding:",
        "",
        "```sh",
        "chtypes list                    # what the registry publishes",
        "chtypes resolve 26.8            # each platform's 26.8 build, verified",
        "```",
        "",
        "See [`guides/fetch-v1.md`](guides/fetch-v1.md) for the contract those commands share.",
    ]
    return "\n".join(out).rstrip("\n") + "\n"


# ------------------------------------------------------------------------ run


def generate(options: Options, ctx: Context, say) -> tuple[int, str]:
    """(exit status, page). The page is empty when the status is 4."""
    data = collect(options, say)
    for message in data.omitted:
        say(f"support-v2: {message}")
    if data.fatal:
        say(f"support-v2: nothing written: {data.fatal}")
        return EXIT_NOTHING, ""
    if data.verified_rows == 0:
        say("support-v2: nothing written: no platform of any line verified")
        return EXIT_NOTHING, ""
    return (EXIT_PARTIAL if data.omitted else EXIT_OK), render(ctx, data)


def run(mode: str, out: Path, say=lambda m: print(m, file=sys.stderr)) -> int:
    ctx = channel_context()
    with tempfile.TemporaryDirectory(prefix="support-v2-cache-") as cache:
        status, page = generate(channel_options(cache), ctx, say)
    return _finish(mode, out, status, page, say)


def _finish(mode: str, out: Path, status: int, page: str, say) -> int:
    if status == EXIT_NOTHING:
        return status
    if mode == "check":
        if status != EXIT_OK:
            say("support-v2: not comparing: the page is incomplete")
            return status
        have = out.read_text(encoding="utf-8") if out.exists() else ""
        if have != page:
            say(
                f"support-v2: {out.name} differs from the generator's output; run "
                "`uv run scripts/support-v2/gen.py --write`"
            )
            return EXIT_DIFF
        return EXIT_OK
    if mode == "write":
        out.write_text(page, encoding="utf-8")
    else:
        sys.stdout.write(page)
        sys.stdout.flush()
    return status


# ------------------------------------------------------------------- selftest

FIXTURES = ROOT / "tests" / "fixtures" / "fetch-v1"
TREE = "resolve-build"
USER_AGENT = "chtypes-python/selftest"


def _test_seed() -> bytes:
    """The fixture test key's seed, from its PKCS#8 PEM (the last 32 bytes of
    the DER), proved against the public half the fixtures ship."""
    import base64

    pem = (FIXTURES / "test-key" / "private.pem").read_text(encoding="ascii")
    der = base64.b64decode("".join(ln for ln in pem.splitlines() if not ln.startswith("-----")))
    return der[-32:]


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Tree:
    """A copy of the fixture registry tree that a case may change."""

    def __init__(self, src: Path, dst: Path) -> None:
        import shutil

        shutil.copytree(src, dst)
        self.root = dst / "v2" / "chtypes" / "v1"

    def set_tags(self, tags: list[str]) -> None:
        (self.root / "tags" / "list").write_text(
            json.dumps({"name": "chtypes/v1", "tags": tags}), encoding="utf-8"
        )

    def platform_manifest(self, line: str, platform: str) -> str:
        os_name, arch = platform.split("-")
        index = json.loads((self.root / "manifests" / line).read_text(encoding="utf-8"))
        for m in index["manifests"]:
            if m["platform"] == {"os": os_name, "architecture": arch}:
                return m["digest"]
        raise KeyError(platform)

    def bundle(self, manifest: str) -> dict:
        index = json.loads((self.root / "referrers" / manifest).read_text(encoding="utf-8"))
        rm = json.loads(
            (self.root / "manifests" / index["manifests"][0]["digest"]).read_text(encoding="utf-8")
        )
        return json.loads(
            (self.root / "blobs" / rm["layers"][0]["digest"]).read_text(encoding="utf-8")
        )

    def statement(self, manifest: str) -> dict:
        import base64

        b = self.bundle(manifest)
        return json.loads(base64.b64decode(b["dsseEnvelope"]["payload"]))

    def replace_bundle(self, manifest: str, bundle: dict) -> None:
        """Serve `bundle` as the platform manifest's signature, rewriting each
        digest that names it (blob, referrer manifest, both referrers views)."""
        data = json.dumps(bundle).encode("utf-8")
        d_bundle = _sha(data)
        (self.root / "blobs" / d_bundle).write_bytes(data)
        rindex_path = self.root / "referrers" / manifest
        rindex = json.loads(rindex_path.read_text(encoding="utf-8"))
        old_rm = rindex["manifests"][0]["digest"]
        rm = json.loads((self.root / "manifests" / old_rm).read_text(encoding="utf-8"))
        rm["layers"][0].update(digest=d_bundle, size=len(data))
        rm_bytes = json.dumps(rm).encode("utf-8")
        d_rm = _sha(rm_bytes)
        (self.root / "manifests" / d_rm).write_bytes(rm_bytes)
        rindex["manifests"][0].update(digest=d_rm, size=len(rm_bytes))
        rindex_bytes = json.dumps(rindex).encode("utf-8")
        rindex_path.write_bytes(rindex_bytes)
        (self.root / "manifests" / ("sha256-" + manifest.split(":", 1)[1])).write_bytes(
            rindex_bytes
        )

    def tamper(self, manifest: str, build: str) -> None:
        """Change the signed build id without re-signing: the signature no
        longer matches the payload."""
        import base64

        b = self.bundle(manifest)
        st = json.loads(base64.b64decode(b["dsseEnvelope"]["payload"]))
        st["predicate"]["build"] = build
        b["dsseEnvelope"]["payload"] = base64.b64encode(json.dumps(st).encode()).decode()
        self.replace_bundle(manifest, b)

    def plant(self, manifest: str, **fields: str) -> None:
        """Re-sign the statement with the test key after setting predicate
        fields: a valid signature over a predicate that must still be refused."""
        sys.path.insert(0, str(ROOT / "python"))
        from tests.ocifetch._bundle_support import build_bundle  # noqa: PLC0415

        st = self.statement(manifest)
        st["predicate"].update(fields)
        keyid = self.bundle(manifest)["verificationMaterial"]["publicKey"]["hint"]
        self.replace_bundle(
            manifest,
            build_bundle(_test_seed(), keyid, subject_sha256="", statement=st),
        )


def _selftest() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    # The derivation: a channel gives its own base, keys and generation, and
    # the active one is the dev channel outside a test run.
    dev, prod = channel_context(_channel.DEV_CHANNEL), channel_context(_channel.PROD_V2_CHANNEL)
    check(_channel.active() is _channel.DEV_CHANNEL, "the active channel is the dev channel")
    check(
        channel_context() == dev
        and not dev.production
        and dev.base == _channel.DEV_CHANNEL_BASE
        and dev.key_ids == (_channel.DEV_KEY_ID,),
        "by default the context is the dev base and the dev key",
    )
    check(
        prod.production
        and prod.base == C.PROD_V2_BASES[0]
        and prod.key_ids == tuple(k["keyid"] for k in C.RELEASE_KEYS),
        "the production channel gives the production base and the release key",
    )
    check(dev.base != prod.base and not set(dev.key_ids) & set(prod.key_ids), "and they differ")

    tmp_ctx = tempfile.TemporaryDirectory(prefix="support-v2-selftest-")
    tmp = Path(tmp_ctx.name)
    seed = _test_seed()
    pub = (FIXTURES / "test-key" / "public.hex").read_text().strip()
    sys.path.insert(0, str(ROOT / "python"))
    from tests.ocifetch._sign_support import _public_key_for  # noqa: PLC0415

    check(_public_key_for(seed).hex() == pub, "the test key's seed matches its public half")

    src = FIXTURES / "trees" / TREE
    other = (FIXTURES / "other-key" / "public.hex").read_text().strip()
    cases: dict[str, Tree] = {}
    for name in ("valid", "tampered", "allbad", "planted-rendered", "planted-hidden"):
        t = Tree(src, tmp / "trees" / name / TREE)
        t.set_tags(["26.8", "26.6"])
        cases[name] = t
    amd = cases["tampered"].platform_manifest("26.8", "linux-amd64")
    cases["tampered"].tamper(amd, "99999999.999999")
    arm = cases["planted-rendered"].platform_manifest("26.8", "linux-arm64")
    # The planted strings are composed at run time so this file carries no
    # private-looking token itself (scripts/lint-public.sh would flag it).
    cases["planted-rendered"].plant(arm, build=PLANTED_NAME)
    arm_h = cases["planted-hidden"].platform_manifest("26.8", "linux-arm64")
    cases["planted-hidden"].plant(arm_h, library=PLANTED_PATH)
    (tmp / "fixtures" / "trees").mkdir(parents=True)
    for name in cases:
        os.symlink(tmp / "trees" / name / TREE, tmp / "fixtures" / "trees" / name)
    (tmp / "fixtures" / "cases.json").write_text(
        json.dumps({"schema": 1, "cases": [{"id": n, "tree": n} for n in cases]}),
        encoding="utf-8",
    )

    server = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "fetch-v1" / "server.py"),
         "--fixtures", str(tmp / "fixtures"), "--port", "0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        port = int(server.stdout.readline().split()[1])
        restore = _channel._use(replace(_channel.FETCH_V1_CHANNEL, name="selftest"))
        try:
            results = {}
            for name in cases:
                base = f"http://127.0.0.1:{port}/s-{name}/chtypes/v1"
                keys = (pub,) if name != "allbad" else (other,)
                options = Options(
                    bases=(base,),
                    trusted_keys=trusted_keys_from_hex(keys, "selftest"),
                    cache_dir=str(tmp / "cache" / name),
                    system_dirs=(),
                )
                said: list[str] = []
                status, page = generate(options, dev, said.append)
                results[name] = (status, page, "\n".join(said), base)
                if os.environ.get("SUPPORT_V2_SELFTEST_SHOW") == name:
                    print(page, "\n".join(said), sep="\n--stderr--\n")
        finally:
            restore()
        log = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/_log/s-valid").read())  # noqa: S310
    finally:
        server.terminate()
        server.wait(timeout=10)

    status, page, said, _ = results["valid"]
    check(status == EXIT_OK, "all statements valid: exit 0")
    check(
        "26.8.15.10" in page and "20261001.183455" in page and "### 26.6" in page,
        "all statements valid: a full page, from the statements' own fields",
    )
    check("not published" in page, "a platform the index lacks reads `not published`")
    check("unsupported" not in page.lower().replace("not the same as unsupported", ""),
          "the page never says `unsupported`")  # fmt: skip
    layers = set()
    for line in ("26.8", "26.6"):
        for m in json.loads((cases["valid"].root / "manifests" / line).read_text())["manifests"]:
            doc = json.loads((cases["valid"].root / "manifests" / m["digest"]).read_text())
            layers.update(layer["digest"] for layer in doc["layers"])
    fetched = {e["path"].rsplit("/", 1)[-1] for e in log if e["method"] == "GET"}
    check(bool(layers) and not layers & fetched, "the registry's own log: no layer was requested")
    check(
        any(e["path"].rsplit("/", 1)[-1] in {m for m in fetched} and "/referrers/" in e["path"]
            for e in log) or any("sha256-" in e["path"] for e in log),
        "the registry's own log: the signatures were requested",
    )  # fmt: skip
    status, page, said, _ = results["tampered"]
    check(status == EXIT_PARTIAL, "one platform tampered: the distinct partial status (3)")
    check("26.8 linux-amd64: omitted" in said, "one platform tampered: named on stderr")
    check("99999999.999999" not in page and "99999999" not in said,
          "one platform tampered: its forged fact is nowhere")  # fmt: skip
    check("`linux-arm64`" in page and "`darwin-arm64`" in page and page.count("26.8.15.10") == 2,
          "one platform tampered: the other rows are kept")  # fmt: skip
    status, page, said, _ = results["allbad"]
    check(status == EXIT_NOTHING and page == "", "every statement bad: nothing written (4)")
    check(said.count("omitted") >= 5, "every statement bad: each omission is named")
    for name in ("planted-rendered", "planted-hidden"):
        status, page, said, _ = results[name]
        check(status == EXIT_PARTIAL, f"{name}: a private-name string refuses that row (3)")
        check("26.8 linux-arm64: omitted" in said, f"{name}: the row is named")
        leak = (PLANTED_NAME, PLANTED_PATH)
        check(not any(s in page or s in said for s in leak), f"{name}: the string is nowhere")
    # The exit-status contract at the file level: --check on a partial page,
    # a missing page and an equal page.
    out = tmp / "page.md"
    ok_page = results["valid"][1]
    check(
        _finish("check", out, EXIT_OK, ok_page, lambda m: None) == EXIT_DIFF,
        "--check: no page differs (1)",
    )
    out.write_text(ok_page, encoding="utf-8")
    check(_finish("check", out, EXIT_OK, ok_page, lambda m: None) == EXIT_OK, "--check: equal (0)")
    check(_finish("check", out, EXIT_PARTIAL, ok_page, lambda m: None) == EXIT_PARTIAL,
          "--check: an incomplete page fails")  # fmt: skip
    check(
        render(dev, Collected(lines={})) == render(dev, Collected(lines={})),
        "render is deterministic",
    )
    tmp_ctx.cleanup()
    if failures:
        print(f"support-v2 gen.py --selftest: {len(failures)} FAILED", file=sys.stderr)
        return 1
    print("support-v2 gen.py --selftest: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="write docs/support-v2.md")
    g.add_argument("--check", action="store_true", help="fail unless the committed page is current")
    g.add_argument("--selftest", action="store_true", help="prove the rules on a fixture registry")
    ap.add_argument("--out", type=Path, default=PAGE, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    return run("write" if args.write else "check" if args.check else "print", args.out)


if __name__ == "__main__":
    sys.exit(main())
