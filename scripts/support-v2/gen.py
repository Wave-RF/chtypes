#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["backports.zstd>=1.7; python_version < '3.14'"]
# ///
r"""gen.py: generate docs/support-v2.md, and judge the channel's published lines
against its signed per-line document (public issue #438).

    uv run scripts/support-v2/gen.py                  print the page to stdout
    uv run scripts/support-v2/gen.py --write          write docs/support-v2.md
    uv run scripts/support-v2/gen.py --check          the committed page and lines.json must agree
    uv run scripts/support-v2/gen.py --print-document print the verified channel document to stdout
    uv run scripts/support-v2/gen.py --selftest       prove every rule below on a fixture registry

THE PAGE holds only what the SDK owns: languages, SDK to ABI, platforms, the rule
that decides a line's support, and the link to the per-line page. That page is the
artifact producer's: its publish job renders it and serves it at
`<channel base>/support-matrix.md`, so a publish opens no pull request here. The
page here depends on nothing the registry says, and the same tree gives the same
bytes.

WHERE THE FACTS COME FROM. The Python binding's ACTIVE generation-2 channel
decides the registry, the key and the generation (`chtypes._ocifetch._channel.
active()`): the staging dev channel today, production `chtypes/v2` with the
release key after the lock change flips `active()`, with no change here. This
script reads those three values and nothing else about the channel; an
environment variable cannot redirect it (the base and the keys are passed as
explicit options, which beat the environment on a channel that honors it).

THE CHANNEL DOCUMENT is the only source of per-line state. It lives in the
repository `<channel>/channel` beside the channel, with no `current` tag; each
publication is a create-only tag `YYYYMMDDTHHMMSSZ`. A publication is a manifest of
artifactType `application/vnd.wavehouse.chtypes.channel.v1`, an empty config and
one layer of media type `application/vnd.wavehouse.chtypes.channel.v1+json` holding
the document's canonical bytes, and a bundle referrer whose statement has
predicateType `https://artifacts.wavehouse.dev/spec/channel/v1`, one subject (the
layer, named `channel.json`) and the document as its predicate. The document:

    {"schema": 1, "repository": "chtypes/v2-dev", "produced_at": "2026-11-06T12:00:00Z",
     "lines": {"26.3": {"state": "supported", "since": "2026-03-27",
                        "security_md_commit": "<40 lowercase hex, or the empty string>"}}}

`state` is `supported` or `retired`. `since` is a UTC date. Unknown keys are ignored.

Each step below refuses with its own name in brackets on stderr:

 1. Read `tags/list?n=1000` and take the largest tag spelled like a publication.
    NO SUCH TAG is the one answer that means "nothing is published", and the only
    form it takes is the one measured on both registries (2026-11-06): a 200 whose
    JSON has this repository's `name` and a `tags` list. Those registries answer
    that for ANY repository name, so a 404 is not this answer: it, a transport
    failure, a 5xx, a 401, a 403 or an unreadable body is [listing] or
    [listing-shape]. A listing that may be cut off ([listing-truncated]: a `Link`
    next page, or a full page) cannot name the newest tag.
 2. The tag's manifest must have the channel artifactType ([artifact-type]) and
    exactly one layer, of the channel media type ([layers]).
 3. A bundle referrer counts only if it verifies under the channel's own keys, the
    ones that verify an artifact there ([bundle-untrusted]), with the channel
    predicateType ([predicate-type]) and exactly one subject ([subject-count]),
    equal to the layer's sha256 ([subject-digest]). None listed is [no-bundle].
 4. The layer's sha256 must match ([layer-sha]), and its JSON must be value-equal
    to the predicate ([layer-value]): JSON values, so `true` is not `1`.
 5. In the predicate: schema is 1 ([schema]), repository is the channel being read
    ([repository]), produced_at in basic form equals the tag ([produced-at]), and
    every entry has the shape above ([entry]).
 6. ANY failure on the newest is a hard failure, status 5. An older document is
    never read, because an older one can call a retired line supported.
 7. Facts come only from the predicate. A line on the channel that `lines` does not
    name is "support unknown", never unsupported.

EVERY LINE IS CLASSIFIED, each line the channel's tag listing names and each line
the document calls supported. "Verified" is a platform's build verified exactly as
a fetch checks it, with the fetch layer's own `resolve_each`, downloading no layer;
"no build" is no platform at this SDK's fingerprint, or the tag answering 404;
"failed" is a statement that did not verify, a refused predicate or a read that
failed.

    state                   verified   verified+failed   no build     failed only
    supported               fine       3                 3            3
    retired                 named      named             named        named
    unknown, once published named      3                 3            3
    unknown, none published named      3                 NOT JUDGED   3

While no document is published the per-line comparison is NOT JUDGED, printed as
`live check off`: only a line with no build gets that leniency, and it ends with the
first publication. A statement that does not verify is never excused, published or
not, except on a retired line. Messages never carry a predicate's or a registry's
value, and every run ends with one `verdict` line that agrees with its status.

NOTHING PRIVATE. In a build's statement every field checked must match its format
(version, build id, glibc floor), and no string anywhere in it may match a
private-name pattern; a build that fails either counts as failed.

--check, once a document exists, also compares spec/abi-v2/where-settings/lines.json
with the document's supported lines. A publish cannot redden an unrelated pull
request: .github/workflows/ci.yml runs --check only on one that touches these paths.

EXIT STATUSES
    0  the page is current or written: every line fine, or NOT JUDGED (named)
    1  --check: the committed page or lines.json differs from the output (named)
    2  usage error, or --print-document with nothing published
    3  the page was produced, but a line failed by the table above (named);
       --check does not compare, and fails
    4  nothing was written: the channel's tag listing was unreadable, named no line,
       or no platform of any line verified
    5  nothing was written: the channel document was refused, rules 1 to 6 (named)
   70  a crash in this script, which is never a verdict
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import traceback
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
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
from chtypes._ocifetch._dsse import (  # noqa: E402
    loads_no_duplicate_keys,
    trusted_keys_from_hex,
    verify_bundle,
)
from chtypes._ocifetch._errors import ArtifactCorruptError, FetchError  # noqa: E402
from chtypes._ocifetch._http import (  # noqa: E402
    FetchPolicy,
    RetryPolicy,
    TransportError,
    fetch_from_bases,
)
from chtypes._ocifetch._oci import (  # noqa: E402
    fetch_blob_bytes,
    fetch_manifest_by_digest,
    fetch_manifest_by_tag,
    manifest_single_layer,
)
from chtypes._ocifetch._referrers import discover_referrers  # noqa: E402

PAGE = ROOT / "docs" / "support-v2.md"
LINES_JSON = ROOT / "spec" / "abi-v2" / "where-settings" / "lines.json"

EXIT_OK = 0
EXIT_DIFF = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_NOTHING = 4
EXIT_CHANNEL = 5
EXIT_CRASH = 70

# What the one verdict line of a run says for each status. It is written from the
# status, so the two cannot disagree.
VERDICTS = {
    EXIT_OK: "ok",
    EXIT_DIFF: "a committed file differs",
    EXIT_USAGE: "nothing to print",
    EXIT_PARTIAL: "a line failed",
    EXIT_NOTHING: "nothing written, the channel's lines were not readable",
    EXIT_CHANNEL: "nothing written, the channel document was refused",
    EXIT_CRASH: "crashed, this is not a verdict",
}

# The channel document's repository and its layer's media type. The artifact type
# and the predicate type are generated constants; this media type is not.
CHANNEL_REPOSITORY = "channel"
CHANNEL_LAYER_MEDIA_TYPE = C.CHANNEL_ARTIFACT_TYPE + "+json"
# Asked for by every listing. A listing this long may be cut off, and tags sort
# oldest first, so what is cut is the newest: rule 6 forbids reading on from there.
LISTING_PAGE = 1000

# ASCII digits and fullmatch, spelled out: `\d` also matches other scripts' digits,
# and `$` also matches before a final newline.
PUBLICATION_TAG_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z")
PRODUCED_AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
STATES = ("supported", "retired")

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

    @property
    def repository(self) -> str:
        """The channel as a predicate names it: the base's repository path."""
        return urlsplit(self.base).path.strip("/")

    @property
    def matrix_url(self) -> str:
        """Where the artifact producer's publish job serves the per-line page."""
        return f"{self.base}/support-matrix.md"


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


# ----------------------------------------------------------- the channel document


class ChannelRefused(Exception):
    """The newest channel document may not be used (rules 1 to 6). `name` is the
    step's name in brackets; `why` carries no value the registry or a predicate
    supplied."""

    def __init__(self, name: str, why: str) -> None:
        super().__init__(f"[{name}] {why}")
        self.name = name
        self.why = why


@dataclass(frozen=True)
class ChannelDocument:
    """A verified predicate, and the tag it was published under."""

    tag: str
    predicate: dict

    @property
    def lines(self) -> dict[str, dict]:
        return self.predicate["lines"]

    @property
    def supported(self) -> list[str]:
        """The lines in state `supported`, in numeric order: lines.json's order."""
        return sorted((n for n, e in self.lines.items() if e["state"] == "supported"), key=_num)


def _json_value(value: object) -> str:
    """A JSON value's own text, so a comparison keeps JSON's types: in Python
    `True == 1` and `1 == 1.0`, and neither is true of a document."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _channel_tags(bases, ctx: Context, policy: FetchPolicy, retry: RetryPolicy) -> list[str]:
    """Rule 1's listing. Only a 200 in the form the registries were measured to
    answer can come back: anything else raises, never an empty list."""
    try:
        resp = fetch_from_bases(
            bases,
            "/tags/list",
            mode="tag",
            accept=("application/json",),
            max_bytes=C.TAGS_LIST_MAX_BYTES,
            policy=policy,
            retry=retry,
            query=f"n={LISTING_PAGE}",
        )
    except TransportError as exc:
        raise ChannelRefused(
            "listing", f"the channel's tag listing could not be read ({type(exc).__name__})"
        ) from exc
    try:
        doc = loads_no_duplicate_keys(resp.body)
    except ArtifactCorruptError as exc:
        raise ChannelRefused("listing-shape", "the channel's tag listing is not JSON") from exc
    want_name = f"{ctx.repository}/{CHANNEL_REPOSITORY}"
    tags = doc.get("tags") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or doc.get("name") != want_name
        or not isinstance(tags, list)
        or not all(isinstance(t, str) for t in tags)
    ):
        raise ChannelRefused(
            "listing-shape", f"the channel's tag listing is not a tag list of {want_name}"
        )
    link = next((v for k, v in resp.headers.items() if k.lower() == "link"), "")
    if "next" in link.lower() or len(tags) >= LISTING_PAGE:
        raise ChannelRefused(
            "listing-truncated",
            f"the channel's tag listing may be cut off at {LISTING_PAGE} tags, "
            "so it cannot name the newest",
        )
    return tags


def _counting_predicates(
    bases, manifest: str, layer: str, keys, policy: FetchPolicy, retry: RetryPolicy
) -> list[dict]:
    """Rule 3: the predicate of every bundle that counts. A bundle that does not
    count is passed over, as a key rotation requires; with none counting, the
    first one's reason is the refusal."""
    try:
        listed = discover_referrers(bases, manifest, C.MEDIA_TYPE_BUNDLE, policy=policy, retry=retry)
    except (TransportError, FetchError, ValueError, AttributeError, TypeError) as exc:
        raise ChannelRefused(
            "referrers", f"the manifest's referrers could not be read ({type(exc).__name__})"
        ) from exc
    if not listed:
        raise ChannelRefused("no-bundle", "the manifest has no signature referrer")
    want_sha = layer.split(":", 1)[1]
    counted: list[dict] = []
    first: ChannelRefused | None = None
    for ref in listed[: C.MAX_REFERRERS]:
        why: ChannelRefused | None = None
        try:
            doc, _ = fetch_manifest_by_digest(bases, ref.digest, policy=policy, retry=retry)
            desc = manifest_single_layer(doc, expected_media_type=C.MEDIA_TYPE_BUNDLE)
            data = fetch_blob_bytes(
                bases, desc.digest, policy=policy, retry=retry, max_bytes=C.BUNDLE_MAX_BYTES
            )
            bundle = json.loads(data)
            verified = verify_bundle(bundle, keys) if isinstance(bundle, dict) else None
        except (TransportError, FetchError, ValueError, KeyError, TypeError, AttributeError) as exc:
            verified = None
            why = ChannelRefused(
                "bundle-unreadable", f"a signature referrer could not be read ({type(exc).__name__})"
            )
        if why is None and verified is None:
            why = ChannelRefused("bundle-untrusted", "no bundle verifies under the channel's keys")
        elif why is None and verified.statement.predicate_type != C.PREDICATE_TYPE_CHANNEL:
            why = ChannelRefused("predicate-type", "the statement's predicateType is not the channel's")
        elif why is None and len(verified.statement.subject_sha256) != 1:
            why = ChannelRefused("subject-count", "the statement does not have exactly one subject")
        elif why is None and verified.statement.subject_sha256[0] != want_sha:
            why = ChannelRefused("subject-digest", "the statement's subject is not the layer's sha256")
        if why is None:
            counted.append(verified.statement.predicate)
        else:
            first = first or why
    if not counted:
        raise first
    return counted


def _basic_form(produced_at: object) -> str:
    """produced_at as a publication tag, or "" when it is not a real UTC time."""
    if not isinstance(produced_at, str) or not PRODUCED_AT_RE.fullmatch(produced_at):
        return ""
    try:
        datetime.strptime(produced_at, "%Y-%m-%dT%H:%M:%SZ")  # noqa: DTZ007
    except ValueError:
        return ""
    return produced_at.replace("-", "").replace(":", "")


def _entry_problem(entry: object) -> str:
    """What is wrong with one `lines` entry, naming the field, never its value."""
    if not isinstance(entry, dict):
        return "it is not an object"
    state = entry.get("state")
    if not isinstance(state, str) or state not in STATES:
        return "its state is neither supported nor retired"
    since = entry.get("since")
    if not isinstance(since, str) or not DATE_RE.fullmatch(since):
        return "its since is not a date"
    try:
        datetime.strptime(since, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        return "its since is not a date"
    commit = entry.get("security_md_commit")
    if not isinstance(commit, str) or (commit != "" and not COMMIT_RE.fullmatch(commit)):
        return "its security_md_commit is neither 40 lowercase hex nor empty"
    return ""


def _check_predicate(predicate: dict, ctx: Context, tag: str) -> None:
    """Rule 5. Unknown keys are ignored, here and in an entry."""
    schema = predicate.get("schema")
    if type(schema) is not int or schema != 1:
        raise ChannelRefused("schema", "the document's schema is not 1")
    if predicate.get("repository") != ctx.repository:
        raise ChannelRefused("repository", f"the document does not name {ctx.repository}")
    if _basic_form(predicate.get("produced_at")) != tag:
        raise ChannelRefused("produced-at", f"the document's produced_at does not equal the tag {tag}")
    lines = predicate.get("lines")
    if not isinstance(lines, dict):
        raise ChannelRefused("entry", "the document's lines is not an object")
    for line, entry in lines.items():
        if not LINE_RE.fullmatch(line):
            raise ChannelRefused("entry", "a key of the document's lines is not a line")
        problem = _entry_problem(entry)
        if problem:
            raise ChannelRefused("entry", f"the entry for {line}: {problem}")


def read_channel_document(options: Options, ctx: Context) -> ChannelDocument | None:
    """Rules 1 to 5 on the newest publication. None means no publication exists,
    and nothing else does; any other trouble raises ChannelRefused."""
    bases = tuple(f"{b.rstrip('/')}/{CHANNEL_REPOSITORY}" for b in options.resolved_bases())
    policy = FetchPolicy(token=options.token, clock=options.clock)
    retry = options.retry
    tags = _channel_tags(bases, ctx, policy, retry)
    stamped = [t for t in tags if PUBLICATION_TAG_RE.fullmatch(t)]
    if not stamped:
        return None
    tag = max(stamped)  # fixed width, so the largest is the newest

    try:
        manifest, _raw, digest, _ = fetch_manifest_by_tag(bases, tag, policy=policy, retry=retry)
    except (TransportError, ValueError) as exc:
        raise ChannelRefused(
            "manifest", f"the manifest of {tag} could not be read ({type(exc).__name__})"
        ) from exc
    if not isinstance(manifest, dict):
        raise ChannelRefused("manifest", f"the manifest of {tag} is not an object")
    if manifest.get("artifactType") != C.CHANNEL_ARTIFACT_TYPE:
        raise ChannelRefused("artifact-type", "the manifest's artifactType is not the channel's")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        raise ChannelRefused("layers", "the manifest does not have exactly one layer")
    if not isinstance(layers[0], dict) or layers[0].get("mediaType") != CHANNEL_LAYER_MEDIA_TYPE:
        raise ChannelRefused("layers", "the manifest's layer is not of the channel media type")
    layer = layers[0].get("digest")
    if not isinstance(layer, str) or not SHA256_DIGEST_RE.fullmatch(layer):
        raise ChannelRefused("layers", "the manifest's layer has no sha256 digest")

    predicates = _counting_predicates(
        bases, digest, layer, options.resolved_trusted_keys(), policy, retry
    )

    try:
        body = fetch_blob_bytes(
            bases, layer, policy=policy, retry=retry, max_bytes=C.MANIFEST_MAX_BYTES
        )
    except ArtifactCorruptError as exc:
        raise ChannelRefused("layer-sha", "the layer's bytes do not hash to its digest") from exc
    except TransportError as exc:
        raise ChannelRefused("layer", f"the layer could not be read ({type(exc).__name__})") from exc
    try:
        document = loads_no_duplicate_keys(body)
    except ArtifactCorruptError as exc:
        raise ChannelRefused("layer-json", "the layer is not JSON") from exc
    for predicate in predicates:
        if _json_value(document) != _json_value(predicate):
            raise ChannelRefused("layer-value", "the layer's JSON is not value-equal to the predicate")

    # Value-equal, so either is the document; rule 7 reads the signed one.
    _check_predicate(predicates[0], ctx, tag)
    return ChannelDocument(tag, predicates[0])


# ---------------------------------------------------------------------- facts


@dataclass(frozen=True)
class Row:
    platform: str
    version: str = ""
    build: str = ""
    glibc: str = ""
    published: bool = True


@dataclass
class LineFacts:
    """One line's builds. `failed` names what kept a build from being read; a line
    with neither a verified row nor a failure simply has no build."""

    rows: list[Row] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return any(r.published for r in self.rows)


@dataclass
class Collected:
    lines: dict[str, LineFacts] = field(default_factory=dict)
    fatal: str = ""

    @property
    def verified_rows(self) -> int:
        return sum(1 for f in self.lines.values() for r in f.rows if r.published)


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
    """Why a verified predicate may not be used, or "" when it may. Names the
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


def collect(options: Options) -> Collected:
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
        facts = LineFacts()
        out.lines[line] = facts
        try:
            outcomes = resolve_each(Request(line), options)
        except FetchError as exc:
            # A tag whose index answers 404, or an SDK ahead of the line: no build.
            # Anything else is a read that failed, which is not the same.
            if exc.code != "CHTYPES_ARTIFACT_UNPUBLISHED":
                facts.failed.append(f"{line}: omitted, the line could not be resolved ({exc.code})")
            continue
        for o in outcomes:
            if o.error is not None and o.error.code == "CHTYPES_ARTIFACT_UNPUBLISHED":
                # The statement verified and says no build answers this SDK's
                # ABI here: absent, which is not unsupported.
                facts.rows.append(Row(o.platform, published=False))
            elif o.error is not None:
                facts.failed.append(
                    f"{line} {o.platform}: omitted, its statement did not verify ({o.error.code})"
                )
            elif o.resolution is None:
                facts.rows.append(Row(o.platform, published=False))
            else:
                predicate = dict(o.resolution.predicate)
                why = _refusal(line, o.platform, predicate)
                if why:
                    facts.failed.append(f"{line} {o.platform}: omitted, {why}")
                    continue
                facts.rows.append(
                    Row(
                        o.platform,
                        version=predicate["clickhouse_version"],
                        build=predicate["build"],
                        glibc=predicate.get("glibc_floor") or "",
                    )
                )
    return out


def judge(data: Collected, document: ChannelDocument | None, say) -> int:
    """The table in this file's header, for every line the listing names and every
    line the document calls supported. Every line that is not plainly fine is named."""
    entries = document.lines if document is not None else {}
    supported = document.supported if document is not None else []
    failed = False
    fine: list[str] = []
    for line in sorted(set(data.lines) | set(supported), key=_num):
        facts = data.lines.get(line, LineFacts())
        entry = entries.get(line)
        state = entry["state"] if entry is not None else None
        for message in facts.failed:
            say(f"support-v2: {message}")
        if state == "retired":
            say(
                f"support-v2: {line}: retired since {entry['since']}: its published builds stay "
                "served, and they are unsupported"
            )
            continue
        if facts.verified:
            if facts.failed:
                failed = True
            elif state == "supported":
                fine.append(line)
            if state is None:
                say(f"support-v2: {line}: support unknown: the line has a verified build")
            continue
        if state == "supported":
            say(f"support-v2: {line}: supported, and no platform of the line verified")
            failed = True
        elif document is not None:
            say(
                f"support-v2: {line}: support unknown, and no platform of the line verified; "
                f"the channel statement {document.tag} does not name it"
            )
            failed = True
        elif facts.failed:
            say(f"support-v2: {line}: support unknown, and a read failed: not excused")
            failed = True
        else:
            say(f"support-v2: {line}: support unknown, no build for this SDK: NOT JUDGED")
    if fine:
        say(f"support-v2: supported with a verified build: {', '.join(fine)}")
    return EXIT_PARTIAL if failed else EXIT_OK


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


def render(ctx: Context) -> str:
    """The page, from the channel's identity and the SDK's own manifests: nothing
    the registry says reaches it, so no publish can make it stale."""
    where = urlsplit(ctx.base)
    out: list[str] = ["# What chtypes supports in v2", ""]
    out += [
        "<!-- Generated by scripts/support-v2/gen.py. Do not edit: regenerate it. -->",
        "",
        "Three independent axes, and a combination works only if all three do: the "
        "**language** you call from, the **platform** you run on, and the **ClickHouse line** "
        "you want answers for. This page states the two the SDK owns and the rule for the "
        f"third; the lines themselves are on the [per-line support page]({ctx.matrix_url}), "
        "published with the builds. Where something is not known it is **support unknown**, "
        "which is not the same as unsupported.",
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
        "loader; a host under it is refused with `CHTYPES_ARTIFACT_INCOMPATIBLE`. musl-based "
        "images are in the same position as any platform not listed: support unknown until a "
        "build says otherwise.",
        "",
        "## ClickHouse lines",
        "",
        "One library per ClickHouse line, each carrying that release's own C++. **A line is "
        "supported exactly while upstream's "
        "[`SECURITY.md`](https://github.com/ClickHouse/ClickHouse/blob/master/SECURITY.md) "
        "lists it.** A line upstream no longer lists is retired: its published builds stay "
        "served, and they are unsupported. A line the channel's signed per-line document does "
        "not name is support unknown.",
        "",
        "The artifact producer's publish job renders the full per-line page and serves it "
        f"with the builds, at <{ctx.matrix_url}>. A 404 there means this channel has not "
        "published one yet.",
        "",
    ]
    if not ctx.production:
        out += [
            f"That address is the development channel (`{ctx.repository}` on `{where.netloc}`). "
            "A development listing is replaceable and is not a support promise; the address "
            "moves to the production registry when v2 is locked.",
            "",
        ]
    out += [
        "Ask for a line, never a nearest match: a request for a line resolves the newest build "
        "of that line and fails if it is absent, rather than quietly handing back a neighbor "
        "whose answers differ. List what the registry publishes with the CLI of any binding:",
        "",
        "```sh",
        "chtypes list                    # what the registry publishes",
        "chtypes resolve <line>          # each platform's build of the line, verified",
        "```",
        "",
        "See [`guides/fetch-v1.md`](guides/fetch-v1.md) for the contract those commands share.",
    ]
    return "\n".join(out).rstrip("\n") + "\n"


# ------------------------------------------------------------------------ run


@dataclass(frozen=True)
class Outcome:
    """One run's judgment. `document` is None until a publication exists, and on a
    refusal; `page` is empty when nothing may be written."""

    status: int
    page: str = ""
    document: ChannelDocument | None = None


def generate(options: Options, ctx: Context, say) -> Outcome:
    """Rules 1 to 7, then the table in this file's header. The channel document is
    read first: with it refused, nothing else can be judged."""
    try:
        document = read_channel_document(options, ctx)
    except ChannelRefused as refused:
        say(
            f"support-v2: channel statement refused [{refused.name}]: {refused.why}; "
            "no older document is read"
        )
        return Outcome(EXIT_CHANNEL)
    if document is None:
        say(
            "support-v2: live check off: no channel statement published yet; "
            "the per-line comparison is NOT JUDGED"
        )
    else:
        say(f"support-v2: channel statement {document.tag} verified")
    data = collect(options)
    if data.fatal:
        say(f"support-v2: nothing written: {data.fatal}")
        return Outcome(EXIT_NOTHING, document=document)
    if data.verified_rows == 0:
        say("support-v2: nothing written: no platform of any line verified")
        return Outcome(EXIT_NOTHING, document=document)
    return Outcome(judge(data, document, say), render(ctx), document)


def verdict_line(status: int, judged: bool) -> str:
    word = VERDICTS[status]
    if status == EXIT_OK and not judged:
        word = "ok, the live comparison NOT JUDGED"
    return f"support-v2: verdict: {word} (exit {status})"


def run(mode: str, out: Path, lines_json: Path, say) -> int:
    ctx = channel_context()
    with tempfile.TemporaryDirectory(prefix="support-v2-cache-") as cache:
        outcome = generate(channel_options(cache), ctx, say)
    return conclude(mode, out, lines_json, outcome, say)


def conclude(mode: str, out: Path, lines_json: Path, outcome: Outcome, say) -> int:
    """Act on a judgment and say the verdict: the status returned is the one said."""
    status = _finish(mode, out, lines_json, outcome, say)
    say(verdict_line(status, outcome.document is not None))
    return status


def _lines_problem(lines_json: Path, document: ChannelDocument) -> str:
    """How lines.json disagrees with the document's supported lines, or ""."""
    try:
        have = json.loads(lines_json.read_bytes())["lines"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"{lines_json.name} could not be read ({type(exc).__name__})"
    if have != document.supported:
        return (
            f"{lines_json.name} names {have}, the channel statement {document.tag}'s supported "
            f"lines are {document.supported}; RELEASING.md has the steps"
        )
    return ""


def _finish(mode: str, out: Path, lines_json: Path, outcome: Outcome, say) -> int:
    status, page, document = outcome.status, outcome.page, outcome.document
    if status in (EXIT_NOTHING, EXIT_CHANNEL):
        return status
    if mode == "document":
        if document is None:
            say("support-v2: there is no channel document to print")
            return EXIT_USAGE
        say(f"support-v2: printing the channel statement {document.tag}")
        sys.stdout.write(json.dumps(document.predicate, indent=1) + "\n")
        sys.stdout.flush()
        return status
    if mode == "check":
        if status != EXIT_OK:
            say("support-v2: not comparing: a line failed")
            return status
        differs = False
        have = out.read_text(encoding="utf-8") if out.exists() else ""
        if have != page:
            say(
                f"support-v2: {out.name} differs from the generator's output; run "
                "`uv run scripts/support-v2/gen.py --write`"
            )
            differs = True
        if document is not None:
            problem = _lines_problem(lines_json, document)
            if problem:
                say(f"support-v2: {problem}")
                differs = True
        return EXIT_DIFF if differs else EXIT_OK
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

# The artifact producer's own dry-run document, byte for byte. These bytes are the
# fixture's identity: a hand edit of the file fails the selftest. Its facts are
# public (upstream's SECURITY.md history). The registry below does not serve these
# bytes: a publication's layer is the document's canonical form, RFC 8785 through
# scripts/abi-v1/jcs.py, signed over the parsed value.
CHANNEL_FIXTURE = ROOT / "tests" / "fixtures" / "support-v2" / "channel-statement.json"
CHANNEL_FIXTURE_SHA256 = "b15339ce332f49c38e0594bebf56dd008ff1a1b723a1371df1f7fde6e7f0c06d"

# The lines a fixture tree's tag listing names, today's shape on the real channel:
# the document's four supported lines with builds, its retired line with none, and
# one line the document does not name, with a build on some platforms.
TREE_LINES = ("26.3", "26.6", "26.7", "26.8", "26.9", "26.10")
ADDED_LINES = {"26.3": "26.3.60.2", "26.9": "26.9.25.15", "26.10": "26.10.2.103"}
NEWEST = "20261106T120000Z"
OLDER = "20261101T000000Z"
LISTING_ROUTE = "/v2/chtypes/v1/channel/tags/list"


def _seed_of(key_dir: str) -> bytes:
    """A fixture key's seed, from its PKCS#8 PEM (the last 32 bytes of the DER)."""
    import base64

    pem = (FIXTURES / key_dir / "private.pem").read_text(encoding="ascii")
    der = base64.b64decode("".join(ln for ln in pem.splitlines() if not ln.startswith("-----")))
    return der[-32:]


def _test_seed() -> bytes:
    """The fixture test key's seed, proved against its public half by the selftest."""
    return _seed_of("test-key")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _jcs():
    """The repository's RFC 8785 canonicalizer, scripts/abi-v1/jcs.py."""
    spec = importlib.util.spec_from_file_location("jcs", ROOT / "scripts" / "abi-v1" / "jcs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_bundle():
    sys.path.insert(0, str(ROOT / "python"))
    from tests.ocifetch._bundle_support import build_bundle  # noqa: PLC0415

    return build_bundle


class Tree:
    """A copy of the fixture registry tree that a case may change."""

    def __init__(self, src: Path, dst: Path) -> None:
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
        st = self.statement(manifest)
        st["predicate"].update(fields)
        keyid = self.bundle(manifest)["verificationMaterial"]["publicKey"]["hint"]
        self.replace_bundle(
            manifest,
            _build_bundle()(_test_seed(), keyid, subject_sha256="", statement=st),
        )

    def add_line(self, line: str, version: str) -> None:
        """Serve `line` with builds of its own: 26.8's index, each platform manifest
        given bytes of its own, and each statement re-signed to name the line."""
        path = self.root / "manifests"
        index = json.loads((path / "26.8").read_text(encoding="utf-8"))
        for m in index["manifests"]:
            old = m["digest"]
            doc = json.loads((path / old).read_text(encoding="utf-8"))
            doc["annotations"] = {"selftest.line": line}
            data = json.dumps(doc).encode("utf-8")
            new = _sha(data)
            (path / new).write_bytes(data)
            shutil.copyfile(self.root / "referrers" / old, self.root / "referrers" / new)
            st = self.statement(old)
            st["predicate"].update(clickhouse_version=version, clickhouse_minor=line)
            keyid = self.bundle(old)["verificationMaterial"]["publicKey"]["hint"]
            self.replace_bundle(
                new, _build_bundle()(_test_seed(), keyid, subject_sha256="", statement=st)
            )
            m.update(digest=new, size=len(data))
        (path / line).write_text(json.dumps(index), encoding="utf-8")


def _served_print(root: Path, routes: list[dict]) -> str:
    """Every byte a case's registry would serve, and its scripted answers: a plant
    that leaves this equal to its baseline's has planted nothing."""
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(str(p.relative_to(root)).encode() + b"\0" + p.read_bytes() + b"\0")
    h.update(json.dumps(routes, sort_keys=True).encode())
    return h.hexdigest()


class ChannelRepo:
    """The channel repository of a fixture tree: what the artifact producer's
    publish job would push, each keyword of `publish` planting one defect. A new
    one answers its listing exactly as the registries were measured to on
    2026-11-06, a 200 with this repository's name and no tags."""

    def __init__(self, tree_root: Path, signer: tuple[bytes, str], document: dict) -> None:
        self.root = tree_root / CHANNEL_REPOSITORY
        for d in ("blobs", "manifests", "referrers", "tags"):
            (self.root / d).mkdir(parents=True)
        self.signer = signer
        self.document = document
        self.tags: list[str] = []
        self.routes: list[dict] = []
        self.write_listing()

    def write_listing(self, **replace_fields) -> None:
        body = {"name": "chtypes/v2-dev/channel", "tags": self.tags, **replace_fields}
        (self.root / "tags" / "list").write_text(json.dumps(body), encoding="utf-8")

    def doc(self) -> dict:
        return copy.deepcopy(self.document)

    def blob(self, data: bytes) -> str:
        digest = _sha(data)
        (self.root / "blobs" / digest).write_bytes(data)
        return digest

    def publish(
        self,
        doc: dict | None = None,
        *,
        tag: str | None = None,
        layer: bytes | None = None,
        statement: Callable[[dict], None] | None = None,
        manifest: Callable[[dict], None] | None = None,
        signers: list[tuple[bytes, str]] | None = None,
        bundle_bytes: bytes | None = None,
    ) -> tuple[str, str]:
        doc = self.doc() if doc is None else doc
        tag = tag or _basic_form(doc.get("produced_at")) or NEWEST
        layer_bytes = _jcs().canonicalize(doc) if layer is None else layer
        d_layer = self.blob(layer_bytes)
        empty = {"mediaType": C.MEDIA_TYPE_EMPTY_CONFIG, "digest": _sha(b"{}"), "size": 2}
        m = {
            "schemaVersion": 2,
            "mediaType": C.MEDIA_TYPE_MANIFEST,
            "artifactType": C.CHANNEL_ARTIFACT_TYPE,
            "config": empty,
            "layers": [
                {"mediaType": CHANNEL_LAYER_MEDIA_TYPE, "digest": d_layer, "size": len(layer_bytes)}
            ],
        }
        if manifest is not None:
            manifest(m)
        data = json.dumps(m).encode("utf-8")
        d_m = _sha(data)
        (self.root / "manifests" / tag).write_bytes(data)
        (self.root / "manifests" / d_m).write_bytes(data)
        st = {
            "_type": C.STATEMENT_TYPE,
            "subject": [{"name": "channel.json", "digest": {"sha256": d_layer.split(":", 1)[1]}}],
            "predicateType": C.PREDICATE_TYPE_CHANNEL,
            "predicate": doc,
        }
        if statement is not None:
            statement(st)
        entries = []
        for seed, keyid in [self.signer] if signers is None else signers:
            bundle = bundle_bytes
            if bundle is None:
                bundle = json.dumps(
                    _build_bundle()(seed, keyid, subject_sha256="", statement=st)
                ).encode("utf-8")
            d_bundle = self.blob(bundle)
            rm = {
                "schemaVersion": 2,
                "mediaType": C.MEDIA_TYPE_MANIFEST,
                "artifactType": C.MEDIA_TYPE_BUNDLE,
                "config": empty,
                "layers": [
                    {"mediaType": C.MEDIA_TYPE_BUNDLE, "digest": d_bundle, "size": len(bundle)}
                ],
                "subject": {"mediaType": C.MEDIA_TYPE_MANIFEST, "digest": d_m, "size": len(data)},
            }
            rm_bytes = json.dumps(rm).encode("utf-8")
            d_rm = _sha(rm_bytes)
            (self.root / "manifests" / d_rm).write_bytes(rm_bytes)
            entries.append(
                {
                    "mediaType": C.MEDIA_TYPE_MANIFEST,
                    "digest": d_rm,
                    "size": len(rm_bytes),
                    "artifactType": C.MEDIA_TYPE_BUNDLE,
                }
            )
        if entries:
            rindex = json.dumps(
                {"schemaVersion": 2, "mediaType": C.MEDIA_TYPE_INDEX, "manifests": entries}
            ).encode("utf-8")
            (self.root / "referrers" / d_m).write_bytes(rindex)
            (self.root / "manifests" / ("sha256-" + d_m.split(":", 1)[1])).write_bytes(rindex)
        self.tags.append(tag)
        self.write_listing()
        return d_m, d_layer

    def listing_answers(self, *responses: dict) -> None:
        self.routes.append({"method": "GET", "path": LISTING_ROUTE, "responses": list(responses)})

    def fingerprint(self) -> str:
        return _served_print(self.root, self.routes)


def _channel_plants(other: tuple[bytes, str]) -> list[tuple[str, str, Callable[[ChannelRepo], None]]]:
    """(case, the refusal it must get, what it plants). Every plant keeps a good
    document available wherever one could be read instead."""

    def statement_doc(ch: ChannelRepo, change: Callable[[dict], None], **kw) -> None:
        doc = ch.doc()
        change(doc)
        ch.publish(doc, **kw)

    def entry(**fields) -> Callable[[ChannelRepo], None]:
        return lambda ch: statement_doc(ch, lambda d: d["lines"]["26.8"].update(fields))

    def without_since(ch: ChannelRepo) -> None:
        statement_doc(ch, lambda d: d["lines"]["26.8"].pop("since"))

    def two_layers(ch: ChannelRepo) -> None:
        def change(m: dict) -> None:
            m["layers"].append(dict(m["layers"][0]))

        ch.publish(manifest=change)

    def layer_404(ch: ChannelRepo) -> None:
        _, d_layer = ch.publish()
        (ch.root / "blobs" / d_layer).unlink()

    def tampered_layer(ch: ChannelRepo) -> None:
        _, d_layer = ch.publish()
        path = ch.root / "blobs" / d_layer
        path.write_bytes(path.read_bytes().replace(b'"retired"', b'"supported"'))

    def manifest_404(ch: ChannelRepo) -> None:
        ch.publish()
        (ch.root / "manifests" / NEWEST).unlink()

    def manifest_not_json(ch: ChannelRepo) -> None:
        ch.publish()
        (ch.root / "manifests" / NEWEST).write_bytes(b"not json")

    def referrers_503(ch: ChannelRepo) -> None:
        d_m, _ = ch.publish()
        ch.routes.append(
            {
                "method": "GET",
                "path": f"/v2/chtypes/v1/channel/referrers/{d_m}",
                "responses": [{"status": 503}],
            }
        )

    def good_listing_answers(*responses: dict) -> Callable[[ChannelRepo], None]:
        def build(ch: ChannelRepo) -> None:
            ch.publish()
            ch.listing_answers(*responses)

        return build

    def listing_body(*a, **kw) -> Callable[[ChannelRepo], None]:
        def build(ch: ChannelRepo) -> None:
            ch.publish()
            ch.write_listing(*a, **kw)

        return build

    def listing_bytes(data: bytes) -> Callable[[ChannelRepo], None]:
        def build(ch: ChannelRepo) -> None:
            ch.publish()
            (ch.root / "tags" / "list").write_bytes(data)

        return build

    def full_page(ch: ChannelRepo) -> None:
        ch.publish()
        ch.tags += [f"sha256-{i:064x}" for i in range(LISTING_PAGE)]
        ch.write_listing()

    def newest_bad(ch: ChannelRepo, plant: Callable[[ChannelRepo], None]) -> None:
        older = ch.doc()
        older["produced_at"] = "2026-11-01T00:00:00Z"
        older["lines"]["26.7"]["state"] = "supported"
        ch.publish(older)
        plant(ch)

    def wrong_key(ch: ChannelRepo) -> None:
        ch.publish(signers=[other])

    def schema_two_newest(ch: ChannelRepo) -> None:
        statement_doc(ch, lambda d: d.update(schema=2))

    return [
        # Rule 1: everything that is not "no such tag".
        ("listing-503", "listing", good_listing_answers({"status": 503})),
        ("listing-500", "listing", good_listing_answers({"status": 500})),
        ("listing-401", "listing", good_listing_answers({"status": 401})),
        ("listing-403", "listing", good_listing_answers({"status": 403})),
        ("listing-404", "listing", good_listing_answers({"status": 404})),
        ("listing-410", "listing", good_listing_answers({"status": 410})),
        ("listing-reset", "listing", good_listing_answers({"close": True})),
        ("listing-not-json", "listing-shape", listing_bytes(b"<html>a proxy said no</html>")),
        ("listing-not-object", "listing-shape", listing_bytes(b"[]")),
        ("listing-another-name", "listing-shape", listing_body(name="chtypes/v2-dev")),
        ("listing-tags-null", "listing-shape", listing_body(tags=None)),
        ("listing-tags-not-strings", "listing-shape", listing_body(tags=[1])),
        ("listing-no-tags-member", "listing-shape", listing_bytes(b'{"name":"chtypes/v2-dev/channel"}')),
        (
            "listing-next-page",
            "listing-truncated",
            good_listing_answers(
                {
                    "status": 200,
                    "headers": {"Link": f'<{LISTING_ROUTE}?n=1000&last=x>; rel="next"'},
                    "body": json.dumps({"name": "chtypes/v2-dev/channel", "tags": [NEWEST]}),
                }
            ),
        ),
        ("listing-full-page", "listing-truncated", full_page),
        # Rule 2.
        ("manifest-404", "manifest", manifest_404),
        ("manifest-not-json", "manifest", manifest_not_json),
        ("wrong-artifact-type", "artifact-type", lambda ch: ch.publish(manifest=lambda m: m.update(artifactType=C.ARTIFACT_TYPE))),
        ("two-layers", "layers", two_layers),
        ("no-layer", "layers", lambda ch: ch.publish(manifest=lambda m: m.update(layers=[]))),
        ("layer-media-type", "layers", lambda ch: ch.publish(manifest=lambda m: m["layers"][0].update(mediaType=C.MEDIA_TYPE_LAYER))),
        # Rule 3.
        ("no-bundle", "no-bundle", lambda ch: ch.publish(signers=[])),
        ("referrers-503", "referrers", referrers_503),
        ("wrong-key", "bundle-untrusted", wrong_key),
        ("bundle-not-json", "bundle-unreadable", lambda ch: ch.publish(bundle_bytes=b"not json")),
        ("wrong-predicate-type", "predicate-type", lambda ch: ch.publish(statement=lambda st: st.update(predicateType=C.PREDICATE_TYPE_ARTIFACT))),
        ("two-subjects", "subject-count", lambda ch: ch.publish(statement=lambda st: st["subject"].append(dict(st["subject"][0])))),
        ("wrong-subject", "subject-digest", lambda ch: ch.publish(statement=lambda st: st["subject"][0]["digest"].update(sha256="0" * 64))),
        # Rule 4.
        ("tampered-layer", "layer-sha", tampered_layer),
        ("layer-404", "layer", layer_404),
        ("layer-not-json", "layer-json", lambda ch: ch.publish(layer=b"not json")),
        ("layer-duplicate-key", "layer-json", lambda ch: ch.publish(layer=b'{"schema":1,"schema":1}')),
        ("layer-other-value", "layer-value", lambda ch: ch.publish(layer=_jcs().canonicalize({**ch.doc(), "schema": 3}))),
        ("layer-true-for-one", "layer-value", lambda ch: ch.publish(layer=_jcs().canonicalize({**ch.doc(), "schema": True}))),
        # Rule 5.
        ("schema-2", "schema", schema_two_newest),
        ("schema-true", "schema", lambda ch: statement_doc(ch, lambda d: d.update(schema=True))),
        ("schema-missing", "schema", lambda ch: statement_doc(ch, lambda d: d.pop("schema"))),
        ("schema-float", "schema", lambda ch: ch.publish({**ch.doc(), "schema": 1.0}, layer=json.dumps({**ch.doc(), "schema": 1.0}).encode())),
        ("wrong-repository", "repository", lambda ch: statement_doc(ch, lambda d: d.update(repository="chtypes/v2"))),
        ("produced-at-not-tag", "produced-at", lambda ch: ch.publish(tag="20261106T120001Z")),
        ("tag-spelled-not-a-time", "produced-at", lambda ch: ch.publish(tag="99999999T999999Z")),
        ("produced-at-no-time", "produced-at", lambda ch: statement_doc(ch, lambda d: d.update(produced_at="2026-11-06"))),
        ("produced-at-no-date", "produced-at", lambda ch: statement_doc(ch, lambda d: d.update(produced_at="2026-13-06T12:00:00Z"), tag="20261306T120000Z")),
        ("bad-state", "entry", entry(state="deprecated")),
        ("state-not-a-string", "entry", entry(state=["supported"])),
        ("state-missing", "entry", lambda ch: statement_doc(ch, lambda d: d["lines"]["26.8"].pop("state"))),
        ("since-not-a-date", "entry", entry(since="2026-02-30")),
        ("since-missing", "entry", without_since),
        ("commit-null", "entry", entry(security_md_commit=None)),
        ("commit-short", "entry", entry(security_md_commit="abc")),
        ("commit-uppercase", "entry", entry(security_md_commit="A" * 40)),
        ("entry-not-object", "entry", lambda ch: statement_doc(ch, lambda d: d["lines"].update({"26.8": "supported"}))),
        ("lines-not-object", "entry", lambda ch: statement_doc(ch, lambda d: d.update(lines=[]))),
        ("line-key-not-a-line", "entry", lambda ch: statement_doc(ch, lambda d: d["lines"].update({"latest": d["lines"]["26.8"]}))),
        # Rule 6: a good older document is there to fall back to, and must not be read.
        ("newest-tampered-older-good", "layer-sha", lambda ch: newest_bad(ch, tampered_layer)),
        ("newest-wrong-key-older-good", "bundle-untrusted", lambda ch: newest_bad(ch, wrong_key)),
        ("newest-schema-2-older-good", "schema", lambda ch: newest_bad(ch, schema_two_newest)),
    ]  # fmt: skip


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
    check(
        dev.repository == "chtypes/v2-dev" and prod.repository == "chtypes/v2",
        "a predicate's repository is the base's repository path",
    )

    # The fixture: the producer's own bytes, and a changed byte fails.
    fixture_bytes = CHANNEL_FIXTURE.read_bytes()
    check(
        hashlib.sha256(fixture_bytes).hexdigest() == CHANNEL_FIXTURE_SHA256,
        "the channel fixture is the producer's dry-run document, byte for byte",
    )
    check(
        hashlib.sha256(fixture_bytes + b" ").hexdigest() != CHANNEL_FIXTURE_SHA256,
        "the channel fixture: a changed byte would fail that check",
    )
    fixture = json.loads(fixture_bytes)
    jcs = _jcs()
    canonical = jcs.canonicalize(fixture)
    check(
        canonical != fixture_bytes and json.loads(canonical) == fixture,
        "a publication's layer is the canonical form (RFC 8785, jcs.py), not the file's bytes",
    )
    fixture_doc = ChannelDocument(NEWEST, fixture)
    fixture_lines = json.loads(LINES_JSON.read_bytes())
    if str(CHANNEL_FIXTURE.relative_to(ROOT)) in fixture_lines.get("source", ""):
        check(
            fixture_lines["lines"] == fixture_doc.supported,
            "lines.json, while it names the fixture as its source, is the fixture's supported lines",
        )

    # The page: what the SDK owns, nothing per line, and the link for its channel.
    page, prod_page = render(dev), render(prod)
    check(
        page == render(dev) and page != prod_page,
        "the page is deterministic, and each channel gets its own",
    )
    check(
        "https://registry-staging.wavehouse.dev/chtypes/v2-dev/support-matrix.md" in page
        and "https://registry.wavehouse.dev/chtypes/v2/support-matrix.md" in prod_page
        and "registry-staging" not in prod_page,
        "the page links its own channel's served per-line page",
    )
    check(
        all(f"## {h}\n" in page for h in ("Languages", "SDK and ABI", "Platforms", "ClickHouse lines")),
        "the page keeps its sections, the #platforms anchor among them",
    )
    check(
        not re.search(r"\b26\.[0-9]", page + prod_page)
        and not BUILD_RE.search(page + prod_page)
        and not re.search(r"\b[0-9]{8}\.[0-9]{6}\b|[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", page + prod_page),
        "the page states no line, version or build id",
    )
    check(
        "served, and they are unsupported" in page and "upstream's" in page,
        "the page states the rule: supported while upstream lists it, retired stays served",
    )

    tmp_ctx = tempfile.TemporaryDirectory(prefix="support-v2-selftest-")
    tmp = Path(tmp_ctx.name)
    seed = _test_seed()
    pub = (FIXTURES / "test-key" / "public.hex").read_text().strip()
    other = (FIXTURES / "other-key" / "public.hex").read_text().strip()
    sys.path.insert(0, str(ROOT / "python"))
    from tests.ocifetch._sign_support import _public_key_for  # noqa: PLC0415

    check(_public_key_for(seed).hex() == pub, "the test key's seed matches its public half")
    other_seed = _seed_of("other-key")
    check(_public_key_for(other_seed).hex() == other, "the other key's seed matches its public half")
    signer = (seed, hashlib.sha256(bytes.fromhex(pub)).hexdigest()[:16])
    other_signer = (other_seed, hashlib.sha256(bytes.fromhex(other)).hexdigest()[:16])

    fixtures = tmp / "fixtures"
    (fixtures / "trees").mkdir(parents=True)
    (fixtures / "http").mkdir()
    case_ids: list[str] = []
    prints: dict[str, str] = {}
    build_failures: list[str] = []

    def channel_only(name: str, build: Callable[[ChannelRepo], None]) -> None:
        """A case that stops at the channel document: no lines are ever read."""
        root = tmp / "trees" / name / TREE / "v2" / "chtypes" / "v1"
        ch = ChannelRepo(root, signer, fixture)
        try:
            build(ch)
        except Exception as exc:  # noqa: BLE001 - a plant that cannot be built is a failure of its own
            build_failures.append(f"{name}: {exc!r}")
        prints[name] = ch.fingerprint()
        register(name, ch)

    def register(name: str, ch: ChannelRepo | None) -> None:
        os.symlink(tmp / "trees" / name / TREE, fixtures / "trees" / name)
        case_ids.append(name)
        if ch is not None and ch.routes:
            (fixtures / "http" / f"{name}.json").write_text(
                json.dumps({"routes": ch.routes, "second_origin_routes": []}), encoding="utf-8"
            )

    # The good publication, every plant's baseline.
    channel_only("doc-good", lambda ch: ch.publish())
    plants = _channel_plants(other_signer)
    for name, _rule, build in plants:
        channel_only(name, build)

    # Publications that must verify: the reader's own positive controls.
    def unknown_keys(ch: ChannelRepo) -> None:
        doc = ch.doc()
        doc["future"] = {"x": 1}
        doc["lines"]["26.8"]["future"] = "ignored"
        ch.publish(doc)

    def rotation(ch: ChannelRepo) -> None:
        ch.publish(signers=[other_signer, signer])

    def other_tags(ch: ChannelRepo) -> None:
        ch.tags += ["latest", "sha256-" + "0" * 64, "20261106T120000", "99999999T999999Z\n"]
        ch.publish()
        ch.tags += ["20261106T120000Zx"]
        ch.write_listing()

    def older_then_newer(ch: ChannelRepo) -> None:
        older = ch.doc()
        older["produced_at"] = "2026-11-01T00:00:00Z"
        older["lines"]["26.7"]["state"] = "supported"
        ch.publish()
        ch.publish(older)  # listed last: the largest tag wins, not the last one

    def older_alone(ch: ChannelRepo) -> None:
        older = ch.doc()
        older["produced_at"] = "2026-11-01T00:00:00Z"
        ch.publish(older)

    def file_bytes(ch: ChannelRepo) -> None:
        ch.publish(layer=fixture_bytes)

    def only_other_tags(ch: ChannelRepo) -> None:
        ch.tags += ["latest", "sha256-" + "0" * 64]
        ch.write_listing()

    def empty_commit(ch: ChannelRepo) -> None:
        doc = ch.doc()
        doc["lines"]["26.8"]["security_md_commit"] = ""
        ch.publish(doc)

    reader_good = {
        "doc-good": NEWEST,
        "reader-unknown-keys": NEWEST,
        "reader-key-rotation": NEWEST,
        "reader-other-tags": NEWEST,
        "reader-newest-wins": NEWEST,
        "reader-older-alone": OLDER,
        "reader-layer-file-bytes": NEWEST,
        "reader-empty-commit": NEWEST,
    }
    channel_only("reader-unknown-keys", unknown_keys)
    channel_only("reader-key-rotation", rotation)
    channel_only("reader-other-tags", other_tags)
    channel_only("reader-newest-wins", older_then_newer)
    channel_only("reader-older-alone", older_alone)
    channel_only("reader-layer-file-bytes", file_bytes)
    channel_only("reader-empty-commit", empty_commit)
    channel_only("reader-empty", lambda ch: None)
    channel_only("reader-only-other-tags", only_other_tags)

    # Whole registries: the fixture tree, each supported line given builds, and a
    # channel repository beside them.
    base = Tree(FIXTURES / "trees" / TREE, tmp / "base" / TREE)
    for line, version in ADDED_LINES.items():
        base.add_line(line, version)
    base.set_tags(list(TREE_LINES))

    full: dict[str, Tree] = {}
    full_docs: dict[str, bool] = {}

    def full_case(
        name: str,
        *,
        doc: dict | None = None,
        tags: tuple[str, ...] = TREE_LINES,
        change: Callable[[Tree], None] | None = None,
        routes: list[dict] | None = None,
    ) -> Tree:
        t = Tree(tmp / "base" / TREE, tmp / "trees" / name / TREE)
        full_docs[name] = doc is not None
        t.set_tags(list(tags))
        ch = ChannelRepo(t.root, signer, fixture)
        if doc is not None:
            ch.publish(doc)
        if change is not None:
            change(t)
        ch.routes += routes or []
        prints[name] = _served_print(t.root, ch.routes)
        register(name, ch)
        full[name] = t
        return t

    def doc_with(change: Callable[[dict], None]) -> dict:
        doc = copy.deepcopy(fixture)
        change(doc)
        return doc

    def tamper_26_8(t: Tree) -> None:
        t.tamper(t.platform_manifest("26.8", "linux-amd64"), "99999999.999999")

    def tamper_all_26_9(t: Tree) -> None:
        for p in ("linux-amd64", "linux-arm64", "darwin-arm64"):
            t.tamper(t.platform_manifest("26.9", p), "99999999.999999")

    index_503 = [{"method": "GET", "path": "/v2/chtypes/v1/manifests/26.7", "responses": [{"status": 503}]}]
    retired_26_8 = doc_with(lambda d: d["lines"]["26.8"].update(state="retired"))
    not_naming_26_7 = doc_with(lambda d: d["lines"].pop("26.7"))
    calls_26_7_supported = doc_with(lambda d: d["lines"]["26.7"].update(state="supported"))

    full_case("published", doc=copy.deepcopy(fixture))
    full_case("unpublished")
    full_case("tampered", change=tamper_26_8)
    full_case("tampered-env", change=tamper_26_8)
    full_case("tampered-allowed", change=tamper_26_8)
    full_case("allbad")
    full_case(
        "planted-rendered",
        change=lambda t: t.plant(t.platform_manifest("26.8", "linux-arm64"), build=PLANTED_NAME),
    )
    full_case(
        "planted-hidden",
        change=lambda t: t.plant(t.platform_manifest("26.8", "linux-arm64"), library=PLANTED_PATH),
    )
    full_case("supported-no-build", doc=calls_26_7_supported)
    full_case("supported-not-listed", doc=copy.deepcopy(fixture), tags=tuple(n for n in TREE_LINES if n != "26.9"))
    full_case("supported-one-platform-bad", doc=copy.deepcopy(fixture), change=tamper_26_8)
    full_case("retired-with-build", doc=retired_26_8)
    full_case("retired-with-bad-statement", doc=retired_26_8, change=tamper_26_8)
    full_case("unknown-no-build-published", doc=not_naming_26_7)
    full_case("unknown-all-bad-unpublished", change=tamper_all_26_9)
    full_case("unknown-read-fails-unpublished", routes=index_503)
    full_case("unknown-read-fails-published", doc=not_naming_26_7, routes=index_503)
    full_case("retired-read-fails", doc=copy.deepcopy(fixture), routes=index_503)

    (fixtures / "cases.json").write_text(
        json.dumps({"schema": 1, "cases": [{"id": n, "tree": n} for n in case_ids]}),
        encoding="utf-8",
    )

    server = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "fetch-v1" / "server.py"),
         "--fixtures", str(fixtures), "--port", "0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip

    @dataclass
    class Result:
        status: int
        page: str
        said: str
        document: ChannelDocument | None
        rc: int
        log: list[dict]

    out_page, out_lines = tmp / "page.md", tmp / "lines.json"
    out_page.write_text(page, encoding="utf-8")
    out_lines.write_text(json.dumps({"lines": fixture_doc.supported}), encoding="utf-8")
    try:
        port = int(server.stdout.readline().split()[1])
        restore = _channel._use(replace(_channel.FETCH_V1_CHANNEL, name="selftest"))
        try:
            results: dict[str, Result] = {}
            crashed: list[str] = []
            for name in case_ids:
                base_url = f"http://127.0.0.1:{port}/s-{name}/chtypes/v1"
                options = Options(
                    bases=(base_url,),
                    trusted_keys=trusted_keys_from_hex((other if name == "allbad" else pub,), "selftest"),
                    cache_dir=str(tmp / "cache" / name),
                    system_dirs=(),
                    # The control: the one way an unsigned platform gets in.
                    allow_unsigned=name == "tampered-allowed",
                    retry=RetryPolicy(attempts=1),
                )
                said: list[str] = []
                # The rider (#438): CHTYPES_ALLOW_UNSIGNED=1 in the environment
                # must not let an unsigned platform in.
                if name == "tampered-env":
                    os.environ[C.ENV_ALLOW_UNSIGNED_NAME] = "1"
                try:
                    outcome = generate(options, dev, said.append)
                    rc = conclude("check", out_page, out_lines, outcome, said.append)
                except Exception as exc:  # noqa: BLE001 - the judge's own failure, reported per case
                    crashed.append(f"{name}: {exc!r}")
                    continue
                finally:
                    if name == "tampered-env":
                        del os.environ[C.ENV_ALLOW_UNSIGNED_NAME]
                log = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/_log/s-{name}").read())  # noqa: S310
                results[name] = Result(outcome.status, outcome.page, "\n".join(said), outcome.document, rc, log)
                if os.environ.get("SUPPORT_V2_SELFTEST_SHOW") == name:
                    print("\n".join(said), f"rc={rc}", sep="\n")
        finally:
            restore()
    finally:
        server.terminate()
        server.wait(timeout=10)

    check(not build_failures, f"every plant could be built {build_failures[:3]}")
    check(not crashed, f"no case crashed the judge {crashed[:3]}")
    if build_failures or crashed:
        print(f"support-v2 gen.py --selftest: {len(failures)} FAILED", file=sys.stderr)
        tmp_ctx.cleanup()
        return 1

    def verdict_agrees(name: str, r: Result) -> bool:
        return (
            r.said.count("support-v2: verdict:") == 1
            and verdict_line(r.rc, r.document is not None) in r.said
        )

    # Every case's verdict line agrees with its status, whatever the case.
    check(
        all(verdict_agrees(n, r) for n, r in results.items()),
        f"all {len(results)} cases: exactly one verdict line, and it agrees with the exit status",
    )
    check(set(VERDICTS) >= {EXIT_OK, EXIT_DIFF, EXIT_USAGE, EXIT_PARTIAL, EXIT_NOTHING, EXIT_CHANNEL, EXIT_CRASH},
          "every exit status has its verdict")  # fmt: skip

    # Rules 1 to 6: each plant changed the registry, got status 5, and got its own
    # named refusal, and nothing else was said to be the reason.
    baseline = prints["doc-good"]
    for name, rule, _build in plants:
        r = results[name]
        check(prints[name] != baseline, f"{name}: the plant changed what the registry serves")
        check(
            r.status == EXIT_CHANNEL
            and r.rc == EXIT_CHANNEL
            and f"refused [{rule}]:" in r.said
            and r.said.count("channel statement refused [") == 1
            and "verdict: nothing written, the channel document was refused (exit 5)" in r.said
            and r.page == "",
            f"{name}: exit 5 with [{rule}], nothing written"
            + ("" if f"refused [{rule}]:" in r.said else f" (said: {r.said[:200]})"),
        )

    # Rule 6's own point: with a good older document there, it was never asked for.
    for name in ("newest-tampered-older-good", "newest-wrong-key-older-good", "newest-schema-2-older-good"):
        check(
            not any(e["path"].endswith(f"/channel/manifests/{OLDER}") for e in results[name].log)
            and any(e["path"].endswith(f"/channel/manifests/{NEWEST}") for e in results[name].log),
            f"{name}: the registry's own log: the newest was asked for, the older one never",
        )
    check(results["reader-older-alone"].status != EXIT_CHANNEL,
          "the control: that older document verifies when it is the newest, so it was a real fallback")  # fmt: skip
    # The refusals above did not come from a plant on the listing that hid the good tag.
    check(results["listing-503"].log and all("/manifests/" not in e["path"] for e in results["listing-503"].log),
          "a failed listing reads no manifest at all")  # fmt: skip

    # The reader's positive controls.
    for name, tag in reader_good.items():
        r = results[name]
        check(
            r.document is not None and r.document.tag == tag and f"channel statement {tag} verified" in r.said,
            f"{name}: the document at {tag} verifies",
        )
    check(results["reader-newest-wins"].document.lines["26.7"]["state"] == "retired",
          "the largest tag is read, not the last one listed")  # fmt: skip
    check("future" in results["reader-unknown-keys"].document.predicate,
          "unknown keys are ignored, at both levels")  # fmt: skip
    # No such tag: the measured empty answer, and a listing with no publication in it.
    for name in ("reader-empty", "reader-only-other-tags"):
        r = results[name]
        check(
            r.document is None and "support-v2: live check off: no channel statement published yet" in r.said,
            f"{name}: no such tag reads as unpublished, loudly",
        )

    # Each registry below differs from the plain one it is judged against, except
    # the two whose plant is what is trusted rather than what is served.
    for name in full:
        plain = "published" if full_docs[name] else "unpublished"
        if name not in ("published", "unpublished", "allbad", "tampered-env", "tampered-allowed"):
            check(prints[name] != prints[plain], f"{name}: the plant changed what the registry serves")
    check(prints["published"] != prints["unpublished"], "published and unpublished: the registries differ")
    check(prints["tampered-env"] != prints["unpublished"] and prints["tampered-allowed"] != prints["unpublished"],
          "tampered-env and tampered-allowed: the registries are tampered")  # fmt: skip

    # The classification.
    r = results["published"]
    check(r.status == EXIT_OK and r.rc == EXIT_OK, "published, every supported line built: exit 0")
    check(r.document is not None and r.document.supported == ["26.3", "26.8", "26.9", "26.10"],
          "the supported lines are the document's, in numeric order")  # fmt: skip
    check("support-v2: supported with a verified build: 26.3, 26.8, 26.9, 26.10" in r.said,
          "each supported line was judged, and is named")  # fmt: skip
    check("26.7: retired since 2026-10-24: its published builds stay served, and they are unsupported" in r.said,
          "retired, no build: named as served and unsupported, and not exit 3")  # fmt: skip
    unknown = [ln for ln in r.said.splitlines() if ln.startswith("support-v2: 26.6:")]
    check(unknown and "support unknown" in unknown[0] and "unsupported" not in unknown[0],
          "a line the document does not name: support unknown, never unsupported")  # fmt: skip
    check("NOT JUDGED" not in r.said, "published: nothing is NOT JUDGED")

    r = results["unpublished"]
    check(r.status == EXIT_OK and r.rc == EXIT_OK and r.document is None, "unpublished, 26.7 with no build: not exit 3")
    check("support-v2: live check off: no channel statement published yet" in r.said
          and "26.7: support unknown, no build for this SDK: NOT JUDGED" in r.said
          and "verdict: ok, the live comparison NOT JUDGED (exit 0)" in r.said,
          "unpublished: the live check is off, loudly, and the line and the verdict say so")  # fmt: skip
    check("unsupported" not in r.said, "unpublished: nothing is called unsupported")
    check(results["unpublished"].page == results["published"].page, "the page does not depend on the registry")

    def exit_3(name: str, what: str, *named: str) -> None:
        r = results[name]
        check(
            r.status == EXIT_PARTIAL and r.rc == EXIT_PARTIAL and all(n in r.said for n in named),
            f"{name}: {what}: exit 3, named" + ("" if r.status == EXIT_PARTIAL else f" (got {r.status})"),
        )

    exit_3("supported-no-build", "supported with no build", "26.7: supported, and no platform of the line verified")
    exit_3("supported-not-listed", "supported, not on the listing at all", "26.9: supported, and no platform of the line verified")
    exit_3("supported-one-platform-bad", "supported, one platform's statement bad", "26.8 linux-amd64: omitted")
    exit_3("unknown-no-build-published", "unknown with no build, once published", "26.7: support unknown, and no platform of the line verified")
    exit_3("unknown-all-bad-unpublished", "a statement that does not verify is not excused before publication", "26.9 linux-amd64: omitted", "26.9: support unknown, and a read failed")
    exit_3("unknown-read-fails-unpublished", "a read that fails is not no build", "26.7: omitted, the line could not be resolved", "26.7: support unknown, and a read failed")
    exit_3("unknown-read-fails-published", "a read that fails, once published", "26.7: omitted, the line could not be resolved")
    check("unsupported" not in results["unknown-no-build-published"].said, "unknown with no build, once published: not called unsupported")

    for name in ("retired-with-build", "retired-with-bad-statement"):
        r = results[name]
        check(
            r.status == EXIT_OK and "26.8: retired since 2026-08-30" in r.said,
            f"{name}: named as retired, never exit 3",
        )
    r = results["retired-read-fails"]
    check(r.status == EXIT_OK and "26.7: omitted, the line could not be resolved" in r.said
          and "26.7: retired since" in r.said, "retired, its read fails: named, never exit 3")  # fmt: skip

    # What the generator always did: the fetch layer's verification decides.
    r = results["tampered"]
    check(r.status == EXIT_PARTIAL, "one platform tampered: the distinct partial status (3)")
    check("26.8 linux-amd64: omitted" in r.said, "one platform tampered: named on stderr")
    check("99999999" not in r.page and "99999999" not in r.said, "one platform tampered: its forged fact is nowhere")
    r = results["tampered-env"]
    check(r.status == EXIT_PARTIAL, "ALLOW_UNSIGNED=1, one platform tampered: still exit 3")
    check("26.8 linux-amd64: omitted" in r.said, "ALLOW_UNSIGNED=1: the platform is named on stderr")
    # The control proves the row can fail: the same tree with the option itself
    # set (not the variable) admits the platform unsigned, with no omission.
    r = results["tampered-allowed"]
    check(r.status == EXIT_OK and "linux-amd64: omitted" not in r.said,
          "the control: allow_unsigned=True admits it, so the rows bite")  # fmt: skip
    r = results["allbad"]
    check(r.status == EXIT_NOTHING and r.page == "", "every statement bad: nothing written (4)")
    check("no platform of any line verified" in r.said, "every statement bad: named")
    for name in ("planted-rendered", "planted-hidden"):
        r = results[name]
        check(r.status == EXIT_PARTIAL, f"{name}: a private-name string fails that build (3)")
        check("26.8 linux-arm64: omitted" in r.said, f"{name}: the build is named")
        check(not any(s in r.page or s in r.said for s in (PLANTED_NAME, PLANTED_PATH)), f"{name}: the string is nowhere")

    # The registry's own log: no line's layer was requested, the signatures were.
    layers = set()
    root = full["published"].root
    for line in ("26.8", "26.3"):
        for m in json.loads((root / "manifests" / line).read_text())["manifests"]:
            doc = json.loads((root / "manifests" / m["digest"]).read_text())
            layers.update(layer["digest"] for layer in doc["layers"])
    log = results["published"].log
    fetched = {e["path"].rsplit("/", 1)[-1] for e in log if e["method"] == "GET"}
    check(bool(layers) and not layers & fetched, "the registry's own log: no build's layer was requested")
    check(any("/referrers/" in e["path"] or "sha256-" in e["path"] for e in log),
          "the registry's own log: the signatures were requested")  # fmt: skip

    # The file-level contract: --check, --write and --print-document on each outcome.
    good = results["published"]
    published = Outcome(EXIT_OK, good.page, good.document)
    unpublished = Outcome(EXIT_OK, good.page, None)

    def concluded(mode: str, page_path: Path, lines_path: Path, outcome: Outcome) -> tuple[int, str]:
        said: list[str] = []
        rc = conclude(mode, page_path, lines_path, outcome, said.append)
        return rc, "\n".join(said)

    def lines_file(name: str, content: object) -> Path:
        path = tmp / f"lines-{name}.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return path

    agreed = lines_file("agreed", {"lines": ["26.3", "26.8", "26.9", "26.10"]})
    check(concluded("check", out_page, agreed, published)[0] == EXIT_OK, "--check: page and lines.json agree (0)")
    rc, said = concluded("check", tmp / "absent.md", agreed, published)
    check(rc == EXIT_DIFF and "absent.md differs" in said, "--check: no page differs (1), named")
    wrong_page = tmp / "wrong.md"
    wrong_page.write_text(page + "\n", encoding="utf-8")
    rc, said = concluded("check", wrong_page, agreed, published)
    check(rc == EXIT_DIFF and "wrong.md differs" in said and "verdict: a committed file differs (exit 1)" in said,
          "--check: a changed page differs (1), named")  # fmt: skip
    for name, content, what in (
        ("kept", {"lines": ["26.3", "26.7", "26.8", "26.9", "26.10"]}, "still lists the retired line"),
        ("dropped", {"lines": ["26.3", "26.8", "26.9"]}, "misses a supported line"),
        ("order", {"lines": ["26.10", "26.3", "26.8", "26.9"]}, "out of order"),
        ("empty", {"lines": []}, "empty"),
        ("nomember", {"schema": "x"}, "no lines member"),
        ("garbage", "not json", "not JSON"),
    ):
        path = lines_file(name, content)
        rc, said = concluded("check", out_page, path, published)
        check(rc == EXIT_DIFF and path.name in said and "verdict: a committed file differs (exit 1)" in said,
              f"--check: lines.json {what}: disagrees with the document (1), named")  # fmt: skip
    rc, said = concluded("check", out_page, tmp / "no-such-lines.json", published)
    check(rc == EXIT_DIFF and "no-such-lines.json could not be read" in said, "--check: no lines.json at all (1), named")
    disagreeing = lines_file("disagree", {"lines": ["1.1"]})
    rc, said = concluded("check", out_page, disagreeing, unpublished)
    check(rc == EXIT_OK and "NOT JUDGED" in said, "--check, unpublished: lines.json is not compared (0), and the verdict says so")
    check(concluded("check", out_page, agreed, Outcome(EXIT_PARTIAL, page, good.document))[0] == EXIT_PARTIAL,
          "--check: a failed line fails (3), not compared")  # fmt: skip
    check(concluded("check", out_page, agreed, Outcome(EXIT_NOTHING))[0] == EXIT_NOTHING, "--check: nothing readable fails (4)")
    check(concluded("check", out_page, agreed, Outcome(EXIT_CHANNEL))[0] == EXIT_CHANNEL, "--check: a refused document fails (5)")
    written = tmp / "written.md"
    check(concluded("write", written, agreed, Outcome(EXIT_CHANNEL))[0] == EXIT_CHANNEL and not written.exists(),
          "--write: a refused document writes nothing (5)")  # fmt: skip
    check(concluded("write", written, agreed, Outcome(EXIT_NOTHING))[0] == EXIT_NOTHING and not written.exists(),
          "--write: nothing readable writes nothing (4)")  # fmt: skip
    check(concluded("write", written, agreed, published)[0] == EXIT_OK and written.read_text(encoding="utf-8") == page,
          "--write: writes the page (0)")  # fmt: skip
    check(concluded("document", written, agreed, unpublished)[0] == EXIT_USAGE,
          "--print-document with nothing published: nothing to print (2)")  # fmt: skip

    # A crash is its own status, never a verdict on the registry.
    said_crash: list[str] = []

    def boom() -> int:
        raise RuntimeError("planted")

    with open(os.devnull, "w", encoding="utf-8") as devnull:
        stderr, sys.stderr = sys.stderr, devnull
        try:
            rc = _guarded(boom, said_crash.append)
        finally:
            sys.stderr = stderr
    check(rc == EXIT_CRASH and verdict_line(EXIT_CRASH, False) in said_crash, "a crash: its own status (70), and the verdict says so")

    tmp_ctx.cleanup()
    if failures:
        print(f"support-v2 gen.py --selftest: {len(failures)} FAILED", file=sys.stderr)
        return 1
    print(f"support-v2 gen.py --selftest: OK ({len(plants)} plants, {len(results)} registry cases)")
    return 0


def _guarded(body: Callable[[], int], say) -> int:
    """Run `body`. An exception here is a defect of this script, and Python's own
    exit status for it (1) would read as `a committed file differs`."""
    try:
        return body()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        say(f"support-v2: {type(exc).__name__} escaped this script")
        say(verdict_line(EXIT_CRASH, False))
        return EXIT_CRASH


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="write docs/support-v2.md")
    g.add_argument("--check", action="store_true", help="fail unless the page and lines.json are current")
    g.add_argument("--print-document", action="store_true", help="print the verified channel document")
    g.add_argument("--selftest", action="store_true", help="prove the rules on a fixture registry")
    ap.add_argument("--out", type=Path, default=PAGE, help=argparse.SUPPRESS)
    ap.add_argument("--lines", type=Path, default=LINES_JSON, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    mode = (
        "write" if args.write else "check" if args.check else "document" if args.print_document else "print"
    )

    def say(message: str) -> None:
        print(message, file=sys.stderr)

    return _guarded(lambda: run(mode, args.out, args.lines, say), say)


if __name__ == "__main__":
    sys.exit(main())
