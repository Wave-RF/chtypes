#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["backports.zstd>=1.7; python_version < '3.14'"]
# ///
r"""gen.py: generate docs/support-v2.md, and judge the channel's published lines
against its signed per-line document (public issue #438).

    uv run scripts/support-v2/gen.py                  print the page to stdout
    uv run scripts/support-v2/gen.py --write          write docs/support-v2.md
    uv run scripts/support-v2/gen.py --check-page     the committed page must agree, with no registry read
    uv run scripts/support-v2/gen.py --check          the committed page and lines.json must agree with the channel
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
    failure, a 5xx, a 401, a 403, any other 2xx or an unreadable body is [listing]
    or [listing-shape]. A listing that may be cut off ([listing-truncated]: a next
    page on any `Link` line, or a full page, which is any page of 1000 tags) cannot
    name the newest tag. The registry's own tag listing, from which the lines come,
    is read the same way and held to its repository's `name` too.
 2. The tag's manifest must have the channel artifactType ([artifact-type]) and
    exactly one layer, of the channel media type ([layers]), and no member twice
    ([manifest]): a repeated `layers` would otherwise read as its last one.
 3. A bundle referrer counts only if it verifies under the channel's own keys, the
    ones that verify an artifact there ([bundle-untrusted]), with the channel
    predicateType ([predicate-type]) and exactly one subject ([subject-count]),
    equal to the layer's sha256 ([subject-digest]). None listed is [no-bundle].
 4. The layer's sha256 must match ([layer-sha]), and its JSON must be value-equal
    to the predicate ([layer-value]): JSON values, so `true` is not `1`. NaN, an
    infinity, and a number too large for a double (an integer past its range among
    them) are not JSON values ([layer-json]); nor is an integer past Python's own
    digit limit, which is refused by name too and never left to crash.
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
`live check off`, and as a `::warning::` under GitHub Actions: only a line with no
build gets that leniency. A listing with tags and none spelled like a publication
says exactly that, not "published yet". A statement that does not verify is never
excused, published or not, except on a retired line. A supported line with a
verified build that is absent on some platforms has them named on stderr, and its
status does not change. Messages never carry a predicate's or a registry's value,
and every run ends with one `verdict` line that agrees with its status and names
any --out or --lines it was given.

THE LATCH. That leniency ends with the first publication, once lines.json says so.
spec/abi-v2/where-settings/lines.json names its source in a closed form
(scripts/abi-v2/check-where-settings.py): the dry-run fixture's path until the first
publication, then the tag of the publication its lines were taken from. A tag says
a publication was made, so a listing naming none is a refusal ([no-document]), not
"nothing yet", and so is a newest publication older than that tag
([older-than-lines]): an emptied or rolled-back listing cannot turn the comparison
off. Tags are create-only, so a publication that was made stays listed, and a tag
the listing does not name is no publication ([lines-source]): a made-up tag, or
00010101T000000Z, does not set the latch. A source of any other form, or no readable
lines.json, proves nothing about the past, so the leniency is refused then too
([lines-source]). --check also fails while a document exists and lines.json still
names the fixture: until it moves, the latch is not set. The writer of lines.json,
scripts/abi-v2/regen-where-settings.py, refuses to move the source backward, from a
tag to the fixture marker or to an older tag.

NOTHING PRIVATE. In a build's statement every field checked must match its format
(version, build id, glibc floor), and no string anywhere in it may match a
private-name pattern; a build that fails either counts as failed.

--check, once a document exists, also compares lines.json with the document's
supported lines; while none exists, with the dry-run fixture's, whatever else
lines.json says. --check-page needs no registry: the page, lines.json's source, and
in the fixture state its lines. .github/workflows/ci.yml runs --check-page on every
pull request; a publish cannot redden an unrelated one, because --check runs only
on one that touches these paths.

KNOWN LIMITS. A registry that silently caps a listing under the page asked for, with
no `Link` and short of a full page, hides the newest tag from rule 1, and nothing
here can see that: the answer is indistinguishable from a shorter listing. The latch
narrows it (a publication older than lines.json's is refused) and does not close it.
The listing is cached (`max-age=60` on the development registry, `max-age=300` on
production, measured 2026-11-06), so a new publication can stay invisible for that
long, and a run in that window judges the publication before it. A hand edit of lines.json that moves its source from a tag
back to the fixture marker, while the listing is empty, cannot be seen offline: the
state is the pre-publication one. What stands in for that is `spec/**` being a
protected path. The CI wiring is checked by presence only: the selftest finds the
steps in ci.yml by substring and applies the gate's own pattern to named paths; it
does not evaluate the workflow, and `.github/**` being a protected path is what
guards the steps.

EXIT STATUSES
    0  the page is current or written: every line fine, or NOT JUDGED (named)
    1  --check or --check-page: the committed page or lines.json differs (named)
    2  usage error, or --print-document with nothing published
    3  the page was produced, but a line failed by the table above (named);
       --check does not compare, and fails
    4  nothing was written: the channel's tag listing was unreadable, named no line,
       or no platform of any line verified
    5  nothing was written: the channel document was refused, rules 1 to 6 or the
       latch (named)
   70  a crash in this script, which is never a verdict
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import math
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
from chtypes._ocifetch import _http  # noqa: E402
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


def _script(path: Path, name: str):
    """A standard-library script of this repository, by path."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# lines.json's closed form, and the one way to spell a workflow command.
lines_rules = _script(ROOT / "scripts" / "abi-v2" / "check-where-settings.py", "check_where_settings")
annotate = _script(ROOT / "scripts" / "lib" / "gha_annotate.py", "gha_annotate")

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

# The selftest's planted strings, one per pattern below in its order, built at run
# time; the selftest proves each matches its own pattern and no other.
PLANTED_NAME = "chtypes-" + "zz"
PLANTED_PATH = "/" + "Users" + "/someone/lib.so"
PLANTED_PRIVATE = (
    PLANTED_NAME,
    "wave" + "-rf",
    PLANTED_PATH,
    "core" + "#1",
    "ci" + "/x",
    "AUDIT" + "-x.md",
)

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
class NoPublication:
    """Rule 1's one answer that means no publication exists. `listed` counts the tags
    the listing did name, none of them spelled like a publication."""

    listed: int


@dataclass(frozen=True)
class LinesSource:
    """What lines.json's `source` says of the channel's past: `tag` is the publication
    its lines were taken from, "" for the dry-run fixture (none made when it was
    written); `problem` is why neither can be read from it. See THE LATCH."""

    tag: str = ""
    problem: str = ""


def lines_source(lines_json: Path) -> LinesSource:
    """lines.json's `source`, in its closed form."""
    try:
        doc = lines_rules.strict_json(lines_json.read_bytes())
    except (OSError, ValueError) as exc:
        return LinesSource(problem=f"{lines_json.name} could not be read ({type(exc).__name__})")
    source = doc.get("source") if isinstance(doc, dict) else None
    problem = lines_rules.source_problem(source)
    if problem:
        return LinesSource(problem=f"{lines_json.name}'s source is neither the fixture marker nor a publication tag")
    return LinesSource() if source == lines_rules.FIXTURE_SOURCE else LinesSource(tag=source)


@dataclass(frozen=True)
class ChannelDocument:
    """A verified predicate, and the tag it was published under. `published` is every
    publication tag the channel's listing named, the newest among them."""

    tag: str
    predicate: dict
    published: tuple[str, ...] = ()

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


@contextlib.contextmanager
def _link_headers():
    """Every `Link` line of each answer read inside the block. `HttpResponse.headers`
    is a plain dict of the answer's headers, which keeps only the first of a repeated
    one, so a `rel="next"` behind a `rel="prev"` never reaches it. This watches the
    fetch layer's one opener instead, for this block only. If that layer ever stops
    using it, no answer is seen and the caller refuses."""
    answers: list[list[str]] = []
    real = _http._opener

    def opener(url: str):
        built = real(url)
        unwatched = built.open

        def watched(*args, **kwargs):
            answer = unwatched(*args, **kwargs)
            answers.append(answer.headers.get_all("Link") or [])
            return answer

        built.open = watched
        return built

    _http._opener = opener
    try:
        yield answers
    finally:
        _http._opener = real


def _read_listing(bases, what: str, policy: FetchPolicy, retry: RetryPolicy) -> tuple[object, str]:
    """A tag listing's JSON and every `Link` value it was answered with. Only a 200
    comes back: anything else raises, never an empty list."""
    with _link_headers() as answers:
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
            raise ChannelRefused("listing", f"{what} could not be read ({type(exc).__name__})") from exc
    if resp.status != 200:
        raise ChannelRefused("listing", f"{what} answered {resp.status}, not 200")
    if not answers:
        raise ChannelRefused("listing", f"{what}: the fetch layer's answer was not seen")
    try:
        doc = loads_no_duplicate_keys(resp.body)
    except (ArtifactCorruptError, ValueError) as exc:  # ValueError: an integer past Python's digit limit
        raise ChannelRefused("listing-shape", f"{what} is not JSON") from exc
    return doc, ", ".join(answers[-1])


def _refuse_cut_off(tags: list[str], link: str, what: str) -> None:
    if "next" in link.lower() or len(tags) >= LISTING_PAGE:
        raise ChannelRefused(
            "listing-truncated",
            f"{what} may be cut off at {LISTING_PAGE} tags, so it cannot name the newest",
        )


def _channel_tags(bases, ctx: Context, policy: FetchPolicy, retry: RetryPolicy) -> list[str]:
    """Rule 1's listing, in the form the registries were measured to answer."""
    what = "the channel's tag listing"
    doc, link = _read_listing(bases, what, policy, retry)
    want_name = f"{ctx.repository}/{CHANNEL_REPOSITORY}"
    tags = doc.get("tags") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or doc.get("name") != want_name
        or not isinstance(tags, list)
        or not all(isinstance(t, str) for t in tags)
    ):
        raise ChannelRefused("listing-shape", f"{what} is not a tag list of {want_name}")
    _refuse_cut_off(tags, link, what)
    return tags


def _line_tags(options: Options, ctx: Context) -> list[str]:
    """The published lines, by the same spelling `chtypes list` keeps (the CLI's
    `_LINE_SPELLING`), read here because a listing cut off would drop lines from the
    judgment with nothing said: the CLI's own reader has no such check."""
    what = "the registry's tag listing"
    policy = FetchPolicy(token=options.token, clock=options.clock)
    doc, link = _read_listing(options.resolved_bases(), what, policy, options.retry)
    tags = doc.get("tags") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or doc.get("name") != ctx.repository
        or not isinstance(tags, list)
        or not all(isinstance(t, str) for t in tags)
    ):
        raise ChannelRefused("listing-shape", f"{what} is not a tag list of {ctx.repository}")
    _refuse_cut_off(tags, link, what)
    return [t for t in tags if cli._LINE_SPELLING.fullmatch(t)]


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


def _non_finite(value: object) -> bool:
    """Whether `value` holds a number JSON cannot carry: json.loads reads NaN and
    Infinity as numbers, 1e400 as infinity, and an integer past a double's range as
    itself, so two different texts compare equal."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        try:
            float(value)
        except OverflowError:
            return True
        return False
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_non_finite(v) for v in value.values())
    if isinstance(value, list):
        return any(_non_finite(v) for v in value)
    return False


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


def read_channel_document(options: Options, ctx: Context) -> ChannelDocument | NoPublication:
    """Rules 1 to 5 on the newest publication. NoPublication means no publication
    exists, and nothing else does; any other trouble raises ChannelRefused."""
    bases = tuple(f"{b.rstrip('/')}/{CHANNEL_REPOSITORY}" for b in options.resolved_bases())
    policy = FetchPolicy(token=options.token, clock=options.clock)
    retry = options.retry
    tags = _channel_tags(bases, ctx, policy, retry)
    stamped = [t for t in tags if PUBLICATION_TAG_RE.fullmatch(t)]
    if not stamped:
        return NoPublication(len(tags))
    tag = max(stamped)  # fixed width, so the largest is the newest

    try:
        _parsed, raw, digest, _ = fetch_manifest_by_tag(bases, tag, policy=policy, retry=retry)
        # The fetch layer's own parse keeps the last of a repeated member.
        manifest = loads_no_duplicate_keys(raw)
    except (TransportError, ValueError, ArtifactCorruptError) as exc:
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
    except (ArtifactCorruptError, ValueError) as exc:  # ValueError: an integer past Python's digit limit
        raise ChannelRefused("layer-json", "the layer is not JSON") from exc
    if _non_finite(document):
        raise ChannelRefused("layer-json", "the layer holds a number JSON cannot carry")
    for predicate in predicates:
        if _json_value(document) != _json_value(predicate):
            raise ChannelRefused("layer-value", "the layer's JSON is not value-equal to the predicate")

    # Value-equal, so either is the document; rule 7 reads the signed one.
    _check_predicate(predicates[0], ctx, tag)
    return ChannelDocument(tag, predicates[0], tuple(sorted(stamped)))


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
    failed_platforms: set[str] = field(default_factory=set)

    @property
    def verified(self) -> bool:
        return any(r.published for r in self.rows)

    @property
    def absent(self) -> list[str]:
        """The platforms with neither a verified build nor a read that failed."""
        here = {r.platform for r in self.rows if r.published} | self.failed_platforms
        return [p["key"] for p in C.PLATFORMS if p["key"] not in here]


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


def collect(options: Options, ctx: Context) -> Collected:
    out = Collected()
    try:
        tags = _line_tags(options, ctx)
    except ChannelRefused as refused:
        out.fatal = f"[{refused.name}] {refused.why}"
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
                facts.failed_platforms.add(o.platform)
            elif o.resolution is None:
                facts.rows.append(Row(o.platform, published=False))
            else:
                predicate = dict(o.resolution.predicate)
                why = _refusal(line, o.platform, predicate)
                if why:
                    facts.failed.append(f"{line} {o.platform}: omitted, {why}")
                    facts.failed_platforms.add(o.platform)
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
            elif facts.absent:
                say(f"support-v2: {line}: supported, and absent on {', '.join(facts.absent)}")
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


def _hold_to_latch(found: ChannelDocument | NoPublication, source: LinesSource) -> None:
    """THE LATCH, in this file's header: what lines.json's `source` forbids the
    listing's answer to be."""
    if isinstance(found, NoPublication):
        if source.problem:
            raise ChannelRefused(
                "lines-source", f"the listing names no publication, and {source.problem}, so nothing says none was made"
            )
        if source.tag:
            raise ChannelRefused(
                "no-document",
                f"lines.json was taken from the publication {source.tag}, and the listing names no publication",
            )
    elif source.tag and found.tag < source.tag:
        raise ChannelRefused(
            "older-than-lines",
            f"the newest publication {found.tag} is older than {source.tag}, the one lines.json was taken from",
        )
    elif source.tag and source.tag not in found.published:
        # Tags are create-only, so a publication that was made stays listed.
        raise ChannelRefused(
            "lines-source",
            f"lines.json names the publication {source.tag}, and the channel's listing does not",
        )


def generate(options: Options, ctx: Context, say, source: LinesSource) -> Outcome:
    """Rules 1 to 7, the latch, then the table in this file's header. The channel
    document is read first: with it refused, nothing else can be judged."""
    try:
        found = read_channel_document(options, ctx)
        _hold_to_latch(found, source)
    except ChannelRefused as refused:
        say(
            f"support-v2: channel statement refused [{refused.name}]: {refused.why}; "
            "no older document is read"
        )
        return Outcome(EXIT_CHANNEL)
    if isinstance(found, NoPublication):
        document = None
        reason = (
            f"the channel's tag listing names {found.listed} tags and none is spelled like a publication"
            if found.listed
            else "no channel statement published yet"
        )
        line = f"live check off: {reason}; the per-line comparison is NOT JUDGED"
        say(f"support-v2: {line}")
        if os.environ.get("GITHUB_ACTIONS"):
            say(annotate.format_command("warning", line, title="support-v2"))
    else:
        document = found
        say(f"support-v2: channel statement {document.tag} verified")
    data = collect(options, ctx)
    if data.fatal:
        say(f"support-v2: nothing written: {data.fatal}")
        return Outcome(EXIT_NOTHING, document=document)
    if data.verified_rows == 0:
        say("support-v2: nothing written: no platform of any line verified")
        return Outcome(EXIT_NOTHING, document=document)
    return Outcome(judge(data, document, say), render(ctx), document)


def verdict_line(status: int, judged: bool, selection: str = "", word: str = "") -> str:
    word = word or VERDICTS[status]
    if status == EXIT_OK and not judged:
        word = "ok, the live comparison NOT JUDGED"
    named = f"; judged {selection}, not the committed files" if selection else ""
    return f"support-v2: verdict: {word} (exit {status}){named}"


def run(mode: str, out: Path, lines_json: Path, say, selection: str = "") -> int:
    source = lines_source(lines_json)
    ctx = channel_context()
    with tempfile.TemporaryDirectory(prefix="support-v2-cache-") as cache:
        outcome = generate(channel_options(cache), ctx, say, source)
    return conclude(mode, out, lines_json, outcome, say, selection)


def conclude(mode: str, out: Path, lines_json: Path, outcome: Outcome, say, selection: str = "") -> int:
    """Act on a judgment and say the verdict: the status returned is the one said."""
    status = _finish(mode, out, lines_json, outcome, say)
    say(verdict_line(status, outcome.document is not None, selection))
    return status


def _page_problem(out: Path, page: str) -> str:
    """How the committed page differs from the generator's output, or ""."""
    # Bytes, not text: reading text translates a CRLF or a CR to a newline.
    have = out.read_bytes() if out.exists() else b""
    if have != page.encode("utf-8"):
        return (
            f"{out.name} differs from the generator's output; run "
            "`uv run scripts/support-v2/gen.py --write`"
        )
    return ""


def _lines_member(lines_json: Path) -> tuple[object, str]:
    """lines.json's `lines`, and why it could not be read."""
    try:
        return lines_rules.strict_json(lines_json.read_bytes())["lines"], ""
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"{lines_json.name} could not be read ({type(exc).__name__})"


def _fixture_problem(lines_json: Path) -> str:
    """While no document exists: how lines.json disagrees with the dry-run fixture's
    supported lines, or "". It does not ask lines.json's `source`: a line dropped
    with a reworded source is still a line dropped."""
    have, why = _lines_member(lines_json)
    if why:
        return why
    fixture_supported = ChannelDocument("", json.loads(CHANNEL_FIXTURE.read_bytes())).supported
    if have != fixture_supported:
        return (
            f"{lines_json.name} names {have}, and with no channel statement published its lines "
            f"are the dry-run fixture's, {fixture_supported}"
        )
    return ""


def _lines_problem(lines_json: Path, document: ChannelDocument) -> str:
    """How lines.json disagrees with the document, or "": its lines must be the
    document's supported lines, and its source a publication's tag."""
    have, why = _lines_member(lines_json)
    if why:
        return why
    if have != document.supported:
        return (
            f"{lines_json.name} names {have}, the channel statement {document.tag}'s supported "
            f"lines are {document.supported}; RELEASING.md has the steps"
        )
    source = lines_source(lines_json)
    if source.problem:
        return source.problem
    if not source.tag:
        return (
            f"{lines_json.name}'s source still names the dry-run fixture, and the channel published "
            f"{document.tag}, so the latch is not set; RELEASING.md has the steps"
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
        problem = _page_problem(out, page)
        if problem:
            say(f"support-v2: {problem}")
            differs = True
        if document is not None:
            problem = _lines_problem(lines_json, document)
        else:
            problem = _fixture_problem(lines_json)
        if problem:
            say(f"support-v2: {problem}")
            differs = True
        return EXIT_DIFF if differs else EXIT_OK
    if mode == "write":
        out.write_bytes(page.encode("utf-8"))
    else:
        sys.stdout.write(page)
        sys.stdout.flush()
    return status


def check_page(out: Path, lines_json: Path, say, selection: str = "") -> int:
    """--check-page: what can be judged with no registry at all, so it can run on every
    pull request. The page depends on the SDK's own manifests and the active channel's
    identity. lines.json's source must be in its closed form, and in the fixture state
    its lines are the fixture's; a tag can only be judged against the channel."""
    differs = False
    problem = _page_problem(out, render(channel_context()))
    if problem:
        say(f"support-v2: {problem}")
        differs = True
    source = lines_source(lines_json)
    problem = source.problem or ("" if source.tag else _fixture_problem(lines_json))
    if problem:
        say(f"support-v2: {problem}")
        differs = True
    status = EXIT_DIFF if differs else EXIT_OK
    word = "ok, the page and lines.json's source, no registry read" if status == EXIT_OK else ""
    say(verdict_line(status, True, selection, word))
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
    return _script(ROOT / "scripts" / "abi-v1" / "jcs.py", "jcs")


def _build_bundle():
    sys.path.insert(0, str(ROOT / "python"))
    from tests.ocifetch._bundle_support import build_bundle  # noqa: PLC0415

    return build_bundle


class Tree:
    """A copy of the fixture registry tree that a case may change."""

    def __init__(self, src: Path, dst: Path, repository: str = "chtypes/v2-dev") -> None:
        shutil.copytree(src, dst)
        self.root = dst / "v2" / "chtypes" / "v1"
        self.repository = repository

    def set_tags(self, tags: list[str], **replace_fields) -> None:
        """The line listing, named for the repository it is served for."""
        body = {"name": self.repository, "tags": tags, **replace_fields}
        (self.root / "tags" / "list").write_text(json.dumps(body), encoding="utf-8")

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

    def __init__(
        self, tree_root: Path, signer: tuple[bytes, str], document: dict, repository: str = "chtypes/v2-dev"
    ) -> None:
        self.root = tree_root / CHANNEL_REPOSITORY
        self.repository = repository
        for d in ("blobs", "manifests", "referrers", "tags"):
            (self.root / d).mkdir(parents=True)
        self.signer = signer
        self.document = document
        self.tags: list[str] = []
        self.routes: list[dict] = []
        self.write_listing()

    def write_listing(self, **replace_fields) -> None:
        body = {"name": f"{self.repository}/{CHANNEL_REPOSITORY}", "tags": self.tags, **replace_fields}
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
        statement_of: list[Callable[[dict], None] | None] | None = None,
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
        for i, (seed, keyid) in enumerate([self.signer] if signers is None else signers):
            bundle = bundle_bytes
            if bundle is None:
                # `statement_of` plants one defect per bundle of the referrers list.
                signed = copy.deepcopy(st)
                if statement_of is not None and statement_of[i] is not None:
                    statement_of[i](signed)
                bundle = json.dumps(
                    _build_bundle()(seed, keyid, subject_sha256="", statement=signed)
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


def _token_flow(case: str, on_token: dict, on_listing: dict) -> Callable[[ChannelRepo], None]:
    """The listing is asked for twice, behind an anonymous token: the answers the
    Link watch sees are the token's, then the listing's, and only the last is the
    listing's own."""

    def build(ch: ChannelRepo) -> None:
        ch.publish()
        body = json.dumps({"name": f"{ch.repository}/{CHANNEL_REPOSITORY}", "tags": [NEWEST]})
        challenge = f'Bearer realm="{{origin}}/v2/s-{case}/chtypes/v1/token"'
        ch.listing_answers(
            {"status": 401, "headers": {"WWW-Authenticate": challenge}},
            {"status": 200, "headers": on_listing, "body": body},
        )
        ch.routes.append(
            {"method": "GET", "path": "/v2/chtypes/v1/token", "responses": [{"status": 200, "headers": on_token, "body": '{"token": "t"}'}]}
        )

    return build


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

    def empty_answer(status: int) -> dict:
        return {"status": status, "body": json.dumps({"name": "chtypes/v2-dev/channel", "tags": []})}

    def manifest_bytes(change: Callable[[bytes], bytes]) -> Callable[[ChannelRepo], None]:
        """Serve other bytes for the tag's manifest, its referrers still found: the
        digest the registry is asked about is the served bytes'."""

        def build(ch: ChannelRepo) -> None:
            d_m, _ = ch.publish()
            path = ch.root / "manifests" / NEWEST
            data = change(path.read_bytes())
            path.write_bytes(data)
            shutil.copyfile(ch.root / "referrers" / d_m, ch.root / "referrers" / _sha(data))

        return build

    def repeat_member(name: bytes, first: bytes) -> Callable[[bytes], bytes]:
        def change(data: bytes) -> bytes:
            planted = data.replace(b'"' + name + b'":', b'"' + name + b'": ' + first + b', "' + name + b'":', 1)
            if planted == data:
                raise ValueError(f"no {name!r} member to repeat")
            return planted

        return change

    def other_value(st: dict) -> None:
        st["predicate"]["lines"]["26.7"]["state"] = "supported"

    def wrong_type(st: dict) -> None:
        st["predicateType"] = C.PREDICATE_TYPE_ARTIFACT

    def layer_with(number: bytes, predicate_value: float) -> Callable[[ChannelRepo], None]:
        """A document whose unknown key holds `number` in the layer's text, and whose
        predicate holds the value json.loads reads it as."""

        def build(ch: ChannelRepo) -> None:
            doc = ch.doc()
            doc["future"] = predicate_value
            ch.publish(doc, layer=json.dumps(ch.doc())[:-1].encode() + b', "future": ' + number + b"}")

        return build

    def exactly_a_page(ch: ChannelRepo) -> None:
        ch.publish()
        ch.tags += [f"sha256-{i:064x}" for i in range(LISTING_PAGE - len(ch.tags))]
        ch.write_listing()

    def predicate_only_infinite(ch: ChannelRepo) -> None:
        doc = ch.doc()
        doc["future"] = float("inf")
        ch.publish(doc, layer=json.dumps({**ch.doc(), "future": 1}).encode())

    return [
        # Rule 1: everything that is not "no such tag".
        ("listing-201", "listing", good_listing_answers(empty_answer(201))),
        ("listing-202", "listing", good_listing_answers(empty_answer(202))),
        ("listing-203", "listing", good_listing_answers(empty_answer(203))),
        ("listing-206", "listing", good_listing_answers(empty_answer(206))),
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
        ("listing-repeated-member", "listing-shape", listing_bytes(b'{"name":"chtypes/v2-dev/channel","tags":["20261106T120000Z"],"tags":[]}')),
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
        # Exactly a page is a page that may have been cut off, and so is a `NEXT` in capitals.
        ("listing-exactly-a-page", "listing-truncated", exactly_a_page),
        (
            "listing-next-page-capitals",
            "listing-truncated",
            good_listing_answers(
                {
                    "status": 200,
                    "headers": {"Link": f'<{LISTING_ROUTE}?n=1000&last=x>; rel="NEXT"'},
                    "body": json.dumps({"name": "chtypes/v2-dev/channel", "tags": [NEWEST]}),
                }
            ),
        ),
        # The Link watch reads the listing's own answer, the last one seen.
        ("listing-token-flow-link-on-listing", "listing-truncated", _token_flow("listing-token-flow-link-on-listing", {}, {"Link": f'<{LISTING_ROUTE}?n=1000&last=x>; rel="next"'})),
        ("listing-integer-5001-digits", "listing-shape", listing_bytes(b'{"name":"chtypes/v2-dev/channel","tags":[],"x":1' + b"0" * 5000 + b"}")),
        # Two `Link` lines: the second is the one a mapping of the headers loses. The
        # header names differ in case because the fixture server's are a mapping too.
        (
            "listing-next-page-second-line",
            "listing-truncated",
            good_listing_answers(
                {
                    "status": 200,
                    "headers": {
                        "Link": f'<{LISTING_ROUTE}?n=1000&last=w>; rel="prev"',
                        "link": f'<{LISTING_ROUTE}?n=1000&last=x>; rel="next"',
                    },
                    "body": json.dumps({"name": "chtypes/v2-dev/channel", "tags": [NEWEST]}),
                }
            ),
        ),
        # Rule 2.
        ("manifest-404", "manifest", manifest_404),
        ("manifest-not-json", "manifest", manifest_not_json),
        ("manifest-not-an-object", "manifest", manifest_bytes(lambda _: b"[]")),
        ("manifest-artifact-type-twice", "manifest", manifest_bytes(repeat_member(b"artifactType", b'"application/vnd.other"'))),
        ("manifest-layers-twice", "manifest", manifest_bytes(repeat_member(b"layers", b"[]"))),
        ("wrong-artifact-type", "artifact-type", lambda ch: ch.publish(manifest=lambda m: m.update(artifactType=C.ARTIFACT_TYPE))),
        ("two-layers", "layers", two_layers),
        ("no-layer", "layers", lambda ch: ch.publish(manifest=lambda m: m.update(layers=[]))),
        ("layer-media-type", "layers", lambda ch: ch.publish(manifest=lambda m: m["layers"][0].update(mediaType=C.MEDIA_TYPE_LAYER))),
        ("layer-digest-not-sha256", "layers", lambda ch: ch.publish(manifest=lambda m: m["layers"][0].update(digest="md5:abc"))),
        ("layer-digest-not-a-string", "layers", lambda ch: ch.publish(manifest=lambda m: m["layers"][0].update(digest=1))),
        # Rule 3.
        ("no-bundle", "no-bundle", lambda ch: ch.publish(signers=[])),
        ("referrers-503", "referrers", referrers_503),
        ("wrong-key", "bundle-untrusted", wrong_key),
        ("bundle-not-json", "bundle-unreadable", lambda ch: ch.publish(bundle_bytes=b"not json")),
        ("wrong-predicate-type", "predicate-type", lambda ch: ch.publish(statement=lambda st: st.update(predicateType=C.PREDICATE_TYPE_ARTIFACT))),
        ("two-subjects", "subject-count", lambda ch: ch.publish(statement=lambda st: st["subject"].append(dict(st["subject"][0])))),
        ("wrong-subject", "subject-digest", lambda ch: ch.publish(statement=lambda st: st["subject"][0]["digest"].update(sha256="0" * 64))),
        # With none counting, the first bundle's reason is the refusal.
        ("first-reason-kept", "bundle-untrusted", lambda ch: ch.publish(signers=[other, ch.signer], statement_of=[None, wrong_type])),
        # Rule 4.
        ("tampered-layer", "layer-sha", tampered_layer),
        ("layer-404", "layer", layer_404),
        ("layer-not-json", "layer-json", lambda ch: ch.publish(layer=b"not json")),
        ("layer-duplicate-key", "layer-json", lambda ch: ch.publish(layer=b'{"schema":1,"schema":1}')),
        ("layer-other-value", "layer-value", lambda ch: ch.publish(layer=_jcs().canonicalize({**ch.doc(), "schema": 3}))),
        ("layer-true-for-one", "layer-value", lambda ch: ch.publish(layer=_jcs().canonicalize({**ch.doc(), "schema": True}))),
        # Every bundle that counts must be value-equal, in either order.
        ("second-counted-bundle-differs", "layer-value", lambda ch: ch.publish(signers=[ch.signer, ch.signer], statement_of=[None, other_value])),
        ("first-counted-bundle-differs", "layer-value", lambda ch: ch.publish(signers=[ch.signer, ch.signer], statement_of=[other_value, None])),
        ("layer-nan", "layer-json", layer_with(b"NaN", float("nan"))),
        ("layer-infinity", "layer-json", layer_with(b"Infinity", float("inf"))),
        ("layer-overflow", "layer-json", layer_with(b"1e400", float("inf"))),
        # An integer past a double's range is no more a JSON value than 1e400 is; one past
        # Python's own digit limit is refused by name too, not left to crash.
        ("layer-integer-401-digits", "layer-json", layer_with(b"1" + b"0" * 400, 10**400)),
        ("layer-integer-5001-digits", "layer-json", layer_with(b"1" + b"0" * 5000, 1)),
        ("predicate-infinite-layer-not", "layer-value", predicate_only_infinite),
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
        ("since-not-padded", "entry", entry(since="2026-2-3")),
        ("commit-null", "entry", entry(security_md_commit=None)),
        ("commit-short", "entry", entry(security_md_commit="abc")),
        ("commit-uppercase", "entry", entry(security_md_commit="A" * 40)),
        ("commit-41-hex", "entry", entry(security_md_commit="a" * 41)),
        ("commit-trailing-newline", "entry", entry(security_md_commit="a" * 40 + "\n")),
        ("entry-not-object", "entry", lambda ch: statement_doc(ch, lambda d: d["lines"].update({"26.8": "supported"}))),
        ("lines-not-object", "entry", lambda ch: statement_doc(ch, lambda d: d.update(lines=[]))),
        ("line-key-not-a-line", "entry", lambda ch: statement_doc(ch, lambda d: d["lines"].update({"latest": d["lines"]["26.8"]}))),
        # Rule 6: a good older document is there to fall back to, and must not be read.
        ("newest-tampered-older-good", "layer-sha", lambda ch: newest_bad(ch, tampered_layer)),
        ("newest-wrong-key-older-good", "bundle-untrusted", lambda ch: newest_bad(ch, wrong_key)),
        ("newest-schema-2-older-good", "schema", lambda ch: newest_bad(ch, schema_two_newest)),
    ]  # fmt: skip


# The child a command-line case runs: this script itself, run as `__main__` (runpy, the
# way `python gen.py` runs it, so its `sys.exit(main())` is the one in play), in a real
# process against a fixture registry. What it sets up first is the active channel,
# through the seam the selftest itself uses, because no option or environment variable
# can redirect a real run; run() still derives everything from that channel. A crash
# is planted in the fetch layer's resolve_each, which the script binds when it loads.
CLI_DRIVER = """
import json, runpy, sys
from dataclasses import replace

script, setup, *argv = sys.argv[1:]
setup = json.loads(setup)
sys.path.insert(0, setup["src"])
import chtypes._ocifetch as fetch
from chtypes._ocifetch import _channel

channel = replace(_channel.FETCH_V1_CHANNEL, name="selftest", bases=(setup["base"],), keys=tuple(setup["keys"]))
_channel._use(channel)
if setup.get("crash"):
    def boom(*_a, **_k):
        raise RuntimeError("planted")
    fetch.resolve_each = boom
sys.argv = [script, *argv]
runpy.run_path(script, run_name="__main__")
"""

# What a pull request must touch for --check to run, and what it must not.
LIVE_GATE_PATHS = (
    "docs/support-v2.md",
    "scripts/support-v2/gen.py",
    "spec/abi-v2/where-settings/lines.json",
    "spec/abi-v2/where-settings/26.8.json",
    "tests/fixtures/support-v2/channel-statement.json",
    "scripts/abi-v2/check-where-settings.py",
    "scripts/abi-v2/regen-where-settings.py",
)
LIVE_GATE_NOT = {
    "rust/Cargo.toml": "a binding's manifest changes the page, which --check-page judges on every pull request; the live comparison reads nothing of it",
    "python/src/chtypes/_ocifetch/_channel.py": "the fetch layer is checked by the selftest; gating it on the live registry would redden it whenever the delivery side publishes",
    "README.md": "a file no part of the comparison reads",
}


def _workflow_problems(text: str) -> list[str]:
    """How .github/workflows/ci.yml has come apart from what this script's header and
    RELEASING.md say it does. A presence check, not an evaluation of the workflow: the
    steps are found by substring, and the one thing applied is the gate's `grep -E`
    pattern, to the paths named above. `.github/**` being a protected path is what
    guards the steps themselves."""
    problems = []
    steps = text.split("\n      - ")
    page = [t for t in steps if "gen.py --check-page" in t]
    if len(page) != 1 or "\n        if:" in page[0]:
        problems.append("--check-page is not one step that always runs")
    live = [t for t in steps if re.search(r"gen\.py --check\b(?!-)", t)]
    if len(live) != 1 or "\n        if: steps.support-v2.outputs.touched == 'true'" not in live[0]:
        problems.append("--check is not one step gated on the paths")
    gate = [t for t in steps if "id: support-v2" in t]
    found = re.search(r"grep -qE '([^']+)'", gate[0]) if len(gate) == 1 else None
    if found is None:
        problems.append("the gate step and its filter were not found")
    else:
        if "git diff --name-only --no-renames HEAD^1 HEAD" not in gate[0]:
            problems.append("the gate's diff is not --no-renames: a rename out of a path would not count")
        problems += [f"the gate does not fire on {x}" for x in LIVE_GATE_PATHS if not re.search(found.group(1), x)]
        problems += [f"the gate fires on {x}" for x in LIVE_GATE_NOT if re.search(found.group(1), x)]
    return problems


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
    # The committed lines.json: closed form, and in the fixture state the fixture's lines.
    committed = lines_source(LINES_JSON)
    check(not committed.problem, f"the committed lines.json's source is in its closed form {committed.problem}")
    if not committed.tag:
        check(not _fixture_problem(LINES_JSON), "the committed lines.json, in the fixture state, is the fixture's supported lines")
    check(lines_rules.FIXTURE_SOURCE == str(CHANNEL_FIXTURE.relative_to(ROOT)), "the fixture marker is this fixture's path")
    # The CI wiring, read from the workflow: a step removed would pass everything else.
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    check(not _workflow_problems(workflow), f"ci.yml holds --check-page always and --check behind the paths {_workflow_problems(workflow)}")
    for what, planted, named in (
        ("no --check-page step", workflow.replace("gen.py --check-page", "gen.py --print-page"), "--check-page is not one step that always runs"),
        ("--check-page behind the gate", workflow.replace("run: uv run scripts/support-v2/gen.py --check-page", "if: always()\n        run: uv run scripts/support-v2/gen.py --check-page"), "--check-page is not one step that always runs"),
        ("--check ungated", workflow.replace("if: steps.support-v2.outputs.touched == 'true'", "if: always()"), "--check is not one step gated on the paths"),
        ("a diff that follows renames", workflow.replace("--no-renames ", ""), "the gate's diff is not --no-renames"),
        ("the fixture out of the gate", workflow.replace("|tests/fixtures/support-v2/", ""), "the gate does not fire on tests/fixtures/support-v2/channel-statement.json"),
        ("the page out of the gate", workflow.replace("docs/support-v2\\.md|", ""), "the gate does not fire on docs/support-v2.md"),
        ("the gate firing on README", workflow.replace("regen-where-settings\\.py)'", "regen-where-settings\\.py|README\\.md)'"), "the gate fires on README.md"),
    ):
        problems = _workflow_problems(planted)
        check(planted != workflow and any(named in x for x in problems), f"ci.yml: {what} is caught, as {named!r}")
    check(all(LIVE_GATE_NOT.values()), "every path the gate must not fire on says why")
    # Rule 5's other copy, which the writer of lines.json needs with no binding. Each
    # plant is refused by both, so neither can drift into accepting what the other refuses.
    regen = _script(ROOT / "scripts" / "abi-v2" / "regen-where-settings.py", "regen_where_settings")

    def rule_5_says(doc: dict) -> str:
        try:
            _check_predicate(doc, dev, NEWEST)
        except ChannelRefused as refused:
            return refused.name
        return ""

    check(not rule_5_says(fixture) and not regen.document_problem(fixture), "rule 5's two copies accept the fixture")
    for what, change in (
        ("schema 2", lambda d: d.update(schema=2)),
        ("schema true", lambda d: d.update(schema=True)),
        ("no schema", lambda d: d.pop("schema")),
        ("no repository", lambda d: d.pop("repository")),
        ("produced_at no date", lambda d: d.update(produced_at="2026-13-06T12:00:00Z")),
        ("no produced_at", lambda d: d.pop("produced_at")),
        ("lines a list", lambda d: d.update(lines=[])),
        ("a key no line", lambda d: d["lines"].update(latest=d["lines"]["26.8"])),
        ("an entry a string", lambda d: d["lines"].update({"26.8": "supported"})),
        ("a state outside the two", lambda d: d["lines"]["26.8"].update(state="deprecated")),
        ("no state", lambda d: d["lines"]["26.8"].pop("state")),
        ("since not a date", lambda d: d["lines"]["26.8"].update(since="2026-02-30")),
        ("since not padded", lambda d: d["lines"]["26.8"].update(since="2026-2-3")),
        ("no since", lambda d: d["lines"]["26.8"].pop("since")),
        ("a commit of 41", lambda d: d["lines"]["26.8"].update(security_md_commit="a" * 41)),
        ("a commit in capitals", lambda d: d["lines"]["26.8"].update(security_md_commit="A" * 40)),
        ("a commit with a trailing newline", lambda d: d["lines"]["26.8"].update(security_md_commit="a" * 40 + "\n")),
        ("no commit", lambda d: d["lines"]["26.8"].pop("security_md_commit")),
        ("schema 1.0", lambda d: d.update(schema=1.0)),
        ("a repository that is not a string", lambda d: d.update(repository=1)),
        ("a key with a trailing newline", lambda d: d["lines"].update({"26.8\n": d["lines"]["26.8"]})),
        ("a key with three parts", lambda d: d["lines"].update({"26.8.1": d["lines"]["26.8"]})),
        ("a key with a letter", lambda d: d["lines"].update({"x.8": d["lines"]["26.8"]})),
        ("since with a trailing newline", lambda d: d["lines"]["26.8"].update(since="2026-02-03\n")),
    ):
        doc = copy.deepcopy(fixture)
        change(doc)
        check(bool(rule_5_says(doc)) and bool(regen.document_problem(doc)), f"rule 5's two copies both refuse: {what}")

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

    def finite_float(ch: ChannelRepo) -> None:
        doc = ch.doc()
        doc["future"] = 1.5
        ch.publish(doc, layer=json.dumps(doc).encode())

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
        "reader-finite-float": NEWEST,
        "reader-key-rotation": NEWEST,
        "reader-other-tags": NEWEST,
        "reader-newest-wins": NEWEST,
        "reader-older-alone": OLDER,
        "reader-layer-file-bytes": NEWEST,
        "reader-empty-commit": NEWEST,
        "reader-token-link-on-token": NEWEST,
        "reader-below-a-page": NEWEST,
    }
    channel_only("reader-unknown-keys", unknown_keys)
    channel_only("reader-finite-float", finite_float)
    channel_only("reader-key-rotation", rotation)
    channel_only("reader-other-tags", other_tags)
    channel_only("reader-newest-wins", older_then_newer)
    channel_only("reader-older-alone", older_alone)
    channel_only("reader-layer-file-bytes", file_bytes)
    channel_only("reader-empty-commit", empty_commit)
    # Controls to the listing plants: a `next` on the token's answer is not the listing's,
    # and a listing one short of a page is not cut off.
    channel_only("reader-token-link-on-token", _token_flow("reader-token-link-on-token", {"Link": f'<{LISTING_ROUTE}?n=1000&last=x>; rel="next"'}, {}))

    def below_a_page(ch: ChannelRepo) -> None:
        ch.publish()
        ch.tags += [f"sha256-{i:064x}" for i in range(LISTING_PAGE - 1 - len(ch.tags))]
        ch.write_listing()

    channel_only("reader-below-a-page", below_a_page)
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
    # One registry per private pattern, in a field nothing renders. PLANTED_PRIVATE's
    # comment claims one each: the check below proves each is its pattern's alone.
    for i, planted_string in enumerate(PLANTED_PRIVATE):
        full_case(
            f"private-pattern-{i}",
            change=lambda t, x=planted_string: t.plant(t.platform_manifest("26.8", "linux-arm64"), library=x),
        )

    def plant_26_8(**fields: str) -> Callable[[Tree], None]:
        return lambda t: t.plant(t.platform_manifest("26.8", "linux-arm64"), **fields)

    def darwin_only_26_8(t: Tree) -> None:
        path = t.root / "manifests" / "26.8"
        index = json.loads(path.read_text(encoding="utf-8"))
        index["manifests"] = [m for m in index["manifests"] if m["platform"]["os"] == "darwin"]
        path.write_text(json.dumps(index), encoding="utf-8")

    full_case("refused-version-shape", change=plant_26_8(clickhouse_version="26.8"))
    full_case("refused-version-of-another-line", change=plant_26_8(clickhouse_version="26.9.25.15"))
    full_case("refused-build-shape", change=plant_26_8(build="not-a-build"))
    full_case("refused-glibc-shape", change=plant_26_8(glibc_floor="not-a-glibc"))
    full_case("supported-served-on-one-platform", doc=copy.deepcopy(fixture), change=darwin_only_26_8)

    # The line listing is held to what the channel's is: a line cut off must not read as
    # a line that does not exist.
    lines_route = "/v2/chtypes/v1/tags/list"
    lines_body = json.dumps({"name": "chtypes/v2-dev", "tags": list(TREE_LINES)})

    def listing_route(*responses: dict) -> list[dict]:
        return [{"method": "GET", "path": lines_route, "responses": list(responses)}]

    full_case("lines-listing-next-page", routes=listing_route({"status": 200, "headers": {"Link": f'<{lines_route}?last=x>; rel="next"'}, "body": lines_body}))
    full_case("lines-listing-next-page-second-line", routes=listing_route({"status": 200, "headers": {"Link": f'<{lines_route}?last=w>; rel="prev"', "link": f'<{lines_route}?last=x>; rel="next"'}, "body": lines_body}))
    full_case("lines-listing-full-page", change=lambda t: t.set_tags([*TREE_LINES, *(f"sha256-{i:064x}" for i in range(LISTING_PAGE))]))
    full_case("lines-listing-203", routes=listing_route({"status": 203, "body": lines_body}))
    # Exactly a page, a `NEXT` in capitals, and a listing that is not of this repository
    # (the channel's is held to its name, and so is this one).
    full_case("lines-listing-exactly-a-page", change=lambda t: t.set_tags([*TREE_LINES, *(f"sha256-{i:064x}" for i in range(LISTING_PAGE - len(TREE_LINES)))]))
    full_case("lines-listing-next-page-capitals", routes=listing_route({"status": 200, "headers": {"Link": f'<{lines_route}?last=x>; rel="NEXT"'}, "body": lines_body}))
    full_case("lines-listing-no-name", routes=listing_route({"status": 200, "body": json.dumps({"tags": list(TREE_LINES)})}))
    full_case("lines-listing-another-name", change=lambda t: t.set_tags(list(TREE_LINES), name="someone/else"))
    full_case("lines-listing-integer-5001-digits", routes=listing_route({"status": 200, "body": lines_body[:-1] + ', "x": 1' + "0" * 5000 + "}"}))
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

    # What the command-line runs below read, each named for the channel the child's
    # active channel is: its listing and its document say that repository.
    cli_case_ids: list[str] = []

    def cli_case(name: str, doc: dict | None, plant: Callable[[ChannelRepo], None] | None = None) -> None:
        repository = f"s-{name}/chtypes/v1"
        t = Tree(tmp / "base" / TREE, tmp / "trees" / name / TREE, repository)
        t.set_tags(list(TREE_LINES))
        ch = ChannelRepo(t.root, signer, {**fixture, "repository": repository}, repository=repository)
        if doc is not None:
            ch.publish({**doc, "repository": repository})
        if plant is not None:
            plant(ch)
        os.symlink(tmp / "trees" / name / TREE, fixtures / "trees" / name)
        cli_case_ids.append(name)

    cli_case("cli-published", copy.deepcopy(fixture))
    cli_case("cli-unpublished", None)
    cli_case("cli-refused", None, lambda ch: ch.publish(manifest=lambda m: m.update(artifactType=C.ARTIFACT_TYPE)))
    cli_case("cli-line-failed", calls_26_7_supported)

    (fixtures / "cases.json").write_text(
        json.dumps({"schema": 1, "cases": [{"id": n, "tree": n} for n in case_ids + cli_case_ids]}),
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

    def lines_doc(source: object, lines: list[str]) -> str:
        return json.dumps({"schema": lines_rules.LINES_SCHEMA, "source": source, "lines": lines})

    out_page, out_lines = tmp / "page.md", tmp / "lines.json"
    out_page.write_text(page, encoding="utf-8")
    out_lines.write_text(lines_doc(NEWEST, fixture_doc.supported), encoding="utf-8")
    latch_results: dict[str, Result] = {}
    cli_runs = 0
    try:
        port = int(server.stdout.readline().split()[1])
        restore = _channel._use(replace(_channel.FETCH_V1_CHANNEL, name="selftest"))
        try:
            results: dict[str, Result] = {}
            crashed: list[str] = []

            def run_case(name: str, source: LinesSource) -> Result | None:
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
                    outcome = generate(options, dev, said.append, source)
                    rc = conclude("check", out_page, out_lines, outcome, said.append)
                except Exception as exc:  # noqa: BLE001 - the judge's own failure, reported per case
                    crashed.append(f"{name}: {exc!r}")
                    return None
                finally:
                    if name == "tampered-env":
                        del os.environ[C.ENV_ALLOW_UNSIGNED_NAME]
                log = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/_log/s-{name}").read())  # noqa: S310
                if os.environ.get("SUPPORT_V2_SELFTEST_SHOW") == name:
                    print("\n".join(said), f"rc={rc}", sep="\n")
                return Result(outcome.status, outcome.page, "\n".join(said), outcome.document, rc, log)

            for name in case_ids:
                result = run_case(name, LinesSource())
                if result is not None:
                    results[name] = result
            # The latch: the same registries, under what lines.json says of the past.
            for label, name, source in (
                ("empty", "reader-empty", LinesSource(tag=NEWEST)),
                ("other-tags", "reader-only-other-tags", LinesSource(tag=NEWEST)),
                ("older", "reader-older-alone", LinesSource(tag=NEWEST)),
                ("no-form-empty", "reader-empty", LinesSource(problem="planted")),
                ("same", "doc-good", LinesSource(tag=NEWEST)),
                # An older publication that is listed: lines.json may have been taken from it.
                ("older-listed", "reader-newest-wins", LinesSource(tag=OLDER)),
                ("no-form-document", "doc-good", LinesSource(problem="planted")),
                # A tag the listing does not name: tags are create-only, so none was made.
                ("year-one", "doc-good", LinesSource(tag="00010101T000000Z")),
                ("made-up", "doc-good", LinesSource(tag="20261105T000000Z")),
                ("made-up-listing-has-two", "reader-newest-wins", LinesSource(tag="20261105T000000Z")),
            ):
                result = run_case(name, source)
                if result is not None:
                    latch_results[label] = result
        finally:
            restore()
        cli_runs = _cli_selftest(
            check, tmp / "cli", port, lines_doc, fixture_doc.supported, fixture, page, signer[1], pub, other, cli_case_ids,
        )
    finally:
        server.terminate()
        server.wait(timeout=10)

    check(not build_failures, f"every plant could be built {build_failures[:3]}")
    check(not crashed, f"no case crashed the judge {crashed[:3]}")
    # A selftest that saw a failure ends the process itself: it is the one mode whose
    # status no selftest can read, and CI's whole trust in it is that status.
    if build_failures or crashed:
        print(f"support-v2 gen.py --selftest: {len(failures)} FAILED", file=sys.stderr)
        tmp_ctx.cleanup()
        sys.exit(1)

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
    # No such tag: the measured empty answer, and a listing with no publication in it,
    # each saying exactly which.
    r = results["reader-empty"]
    check(
        r.document is None and "support-v2: live check off: no channel statement published yet" in r.said,
        "reader-empty: no such tag reads as unpublished, loudly",
    )
    r = results["reader-only-other-tags"]
    check(
        r.document is None
        and "live check off: the channel's tag listing names 2 tags and none is spelled like a publication" in r.said
        and "published yet" not in r.said,
        "reader-only-other-tags: tags, none a publication: it says that, not that nothing is published",
    )

    # The latch: once lines.json names a publication, no answer turns the comparison off.
    for label, rule in (
        ("empty", "no-document"),
        ("other-tags", "no-document"),
        ("older", "older-than-lines"),
        ("no-form-empty", "lines-source"),
        ("year-one", "lines-source"),
        ("made-up", "lines-source"),
        ("made-up-listing-has-two", "lines-source"),
    ):
        r = latch_results[label]
        check(
            r.status == EXIT_CHANNEL and r.rc == EXIT_CHANNEL and f"refused [{rule}]:" in r.said and "live check off" not in r.said,
            f"latch, {label}: exit 5 with [{rule}], and the comparison stays on",
        )
    for label in ("same", "older-listed", "no-form-document"):
        r = latch_results[label]
        check(
            r.status != EXIT_CHANNEL and r.document is not None and r.document.tag == NEWEST,
            f"latch, {label}: the document verifies, so the latch has nothing to refuse",
        )

    # Each registry below differs from the plain one it is judged against. These are
    # the exceptions, and an entry that no longer is one fails rather than hides a plant.
    serves_the_plain_registry = {
        "published": "it is the plain registry with a document",
        "unpublished": "it is the plain registry with none",
        "allbad": "its plant is the key the run trusts, not what the registry serves",
    }
    for name, why in serves_the_plain_registry.items():
        plain = "published" if full_docs.get(name) else "unpublished"
        check(name in full and prints[name] == prints[plain], f"{name} serves the plain registry: {why}")
    for name in full:
        plain = "published" if full_docs[name] else "unpublished"
        if name not in serves_the_plain_registry:
            check(prints[name] != prints[plain], f"{name}: the plant changed what the registry serves")
    check(prints["published"] != prints["unpublished"], "published and unpublished: the registries differ")

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

    # One plant per private pattern, each caught by its own pattern and no other.
    check(
        len(PLANTED_PRIVATE) == len(PRIVATE_PATTERNS)
        and all(
            p.search(x) and sum(bool(q.search(x)) for q in PRIVATE_PATTERNS) == 1
            for p, x in zip(PRIVATE_PATTERNS, PLANTED_PRIVATE, strict=True)
        ),
        "each private pattern has one plant, which only it catches",
    )
    for i, planted_string in enumerate(PLANTED_PRIVATE):
        r = results[f"private-pattern-{i}"]
        check(
            r.status == EXIT_PARTIAL
            and "26.8 linux-arm64: omitted, a field" in r.said
            and planted_string not in r.said
            and r.page == page,
            f"private pattern {i}: its string fails that build (3), named, said nowhere, and the page is unchanged",
        )
    # A statement that verifies and whose facts are not of the shape they claim.
    exit_3("refused-version-shape", "a version of another shape", "26.8 linux-arm64: omitted, its version is not a four-part ClickHouse version")
    exit_3("refused-version-of-another-line", "a version of another line", "26.8 linux-arm64: omitted, its statement did not verify")
    # The fetch layer refuses that one first (a version outside the request is corrupt),
    # so a registry cannot reach this script's own rule: it is judged directly.
    good_predicate = {"clickhouse_version": "26.8.34.13", "build": "20261106.120000", "glibc_floor": "2.17"}
    check(_refusal("26.8", "linux-arm64", good_predicate) == "", "the control: a build of the line is not refused")
    check(
        _refusal("26.8", "linux-arm64", {**good_predicate, "clickhouse_version": "26.9.25.15"}) == "its version does not belong to the line",
        "a version of another line is refused by this script's own rule too",
    )
    exit_3("refused-build-shape", "a build id of another shape", "26.8 linux-arm64: omitted, its build id is not a build id")
    exit_3("refused-glibc-shape", "a glibc floor of another shape", "26.8 linux-arm64: omitted, its glibc floor is not a glibc version")

    # A supported line absent on a platform is named, and changes no status; served
    # on all of them, nothing is said.
    r = results["supported-served-on-one-platform"]
    check(
        r.status == EXIT_OK and "26.8: supported, and absent on linux-amd64, linux-arm64" in r.said,
        "a supported line on one platform of three: exit 0, the absent platforms named",
    )
    check("absent on" not in results["published"].said, "a supported line on every platform: no platform named")
    check("absent on" not in results["supported-one-platform-bad"].said, "a platform whose statement failed is named as that, not as absent")

    # A line listing that may be cut off stops the run: a line it hides is not judged.
    for name, rule in (
        ("lines-listing-next-page", "listing-truncated"),
        ("lines-listing-next-page-second-line", "listing-truncated"),
        ("lines-listing-full-page", "listing-truncated"),
        ("lines-listing-203", "listing"),
        ("lines-listing-exactly-a-page", "listing-truncated"),
        ("lines-listing-next-page-capitals", "listing-truncated"),
        ("lines-listing-no-name", "listing-shape"),
        ("lines-listing-another-name", "listing-shape"),
        ("lines-listing-integer-5001-digits", "listing-shape"),
    ):
        r = results[name]
        check(r.status == EXIT_NOTHING and f"nothing written: [{rule}]" in r.said, f"{name}: nothing written (4), [{rule}]")

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

    agreed = lines_file("agreed", {"source": NEWEST, "lines": ["26.3", "26.8", "26.9", "26.10"]})
    check(concluded("check", out_page, agreed, published)[0] == EXIT_OK, "--check: page and lines.json agree (0)")
    rc, said = concluded("check", tmp / "absent.md", agreed, published)
    check(rc == EXIT_DIFF and "absent.md differs" in said, "--check: no page differs (1), named")
    wrong_page = tmp / "wrong.md"
    wrong_page.write_text(page + "\n", encoding="utf-8")
    rc, said = concluded("check", wrong_page, agreed, published)
    check(rc == EXIT_DIFF and "wrong.md differs" in said and "verdict: a committed file differs (exit 1)" in said,
          "--check: a changed page differs (1), named")  # fmt: skip
    # The page is compared as bytes: text mode would read a CRLF or a CR as a newline.
    for name, translated in (("crlf", page.replace("\n", "\r\n")), ("cr", page.replace("\n", "\r"))):
        translated_page = tmp / f"{name}.md"
        translated_page.write_bytes(translated.encode("utf-8"))
        check(translated != page, f"the {name} page is not the page")
        rc, said = concluded("check", translated_page, agreed, published)
        check(rc == EXIT_DIFF and f"{name}.md differs" in said, f"--check: a page with {name} line ends differs (1), named")
        said_page: list[str] = []
        at_marker = lines_file(f"marker-{name}", {"source": lines_rules.FIXTURE_SOURCE, "lines": fixture_doc.supported})
        check(check_page(translated_page, at_marker, said_page.append) == EXIT_DIFF, f"--check-page: a page with {name} line ends differs (1)")

    for name, content, what in (
        ("kept", {"source": NEWEST, "lines": ["26.3", "26.7", "26.8", "26.9", "26.10"]}, "still lists the retired line"),
        ("dropped", {"source": NEWEST, "lines": ["26.3", "26.8", "26.9"]}, "misses a supported line"),
        ("order", {"source": NEWEST, "lines": ["26.10", "26.3", "26.8", "26.9"]}, "out of order"),
        ("empty", {"source": NEWEST, "lines": []}, "empty"),
        ("nomember", {"source": NEWEST, "schema": "x"}, "no lines member"),
        ("garbage", "not json", "not JSON"),
        ("repeated", '{"source": "' + NEWEST + '", "lines": [], "lines": ["26.3", "26.8", "26.9", "26.10"]}', "a repeated member, the last one right"),
        ("fixture", {"source": lines_rules.FIXTURE_SOURCE, "lines": ["26.3", "26.8", "26.9", "26.10"]}, "still names the fixture, so the latch is not set"),
        ("noform", {"source": "a note", "lines": ["26.3", "26.8", "26.9", "26.10"]}, "a source of no form"),
    ):
        path = lines_file(name, content)
        rc, said = concluded("check", out_page, path, published)
        check(rc == EXIT_DIFF and path.name in said and "verdict: a committed file differs (exit 1)" in said,
              f"--check: lines.json {what}: disagrees with the document (1), named")  # fmt: skip
    rc, said = concluded("check", out_page, tmp / "no-such-lines.json", published)
    check(rc == EXIT_DIFF and "no-such-lines.json could not be read" in said, "--check: no lines.json at all (1), named")
    fixture_state = lines_file("fixture-state", {"source": lines_rules.FIXTURE_SOURCE, "lines": fixture_doc.supported})
    rc, said = concluded("check", out_page, fixture_state, unpublished)
    check(rc == EXIT_OK and "NOT JUDGED" in said, "--check, unpublished, lines.json the fixture's: fine (0), and the verdict says the live comparison was not judged")
    # While nothing is published lines.json is the fixture's, whatever its source says.
    for name, source in (("fixture", lines_rules.FIXTURE_SOURCE), ("reworded", "a note"), ("tag", NEWEST), ("none", None)):
        path = lines_file(f"dropped-{name}", {"source": source, "lines": ["26.3", "26.8", "26.9"]})
        rc, said = concluded("check", out_page, path, unpublished)
        check(rc == EXIT_DIFF and "dry-run fixture" in said, f"--check, unpublished, a line dropped with source {name}: differs (1), named")
        check(_fixture_problem(path) != "", f"the fixture guard: a line dropped with source {name}")
    check(_fixture_problem(fixture_state) == "", "the fixture guard's control: the fixture's own lines pass")
    repeated_right_last = lines_file("repeated-fixture", '{"lines": [], "lines": ' + json.dumps(fixture_doc.supported) + "}")
    check(_fixture_problem(repeated_right_last) != "", "the fixture guard: a repeated `lines` is not read as its last one")

    # lines.json's source, each form.
    for name, source, want in (
        ("fixture", lines_rules.FIXTURE_SOURCE, LinesSource()),
        ("tag", NEWEST, LinesSource(tag=NEWEST)),
    ):
        check(lines_source(lines_file(f"form-{name}", {"source": source})) == want, f"lines.json's source: {name} reads as itself")
    for name, content in (
        ("reworded", {"source": "the producer's dry-run document"}),
        ("absent", {}),
        ("not-a-string", {"source": 1}),
        ("a-tag-no-time", {"source": "20261399T999999Z"}),
        ("not-json", "not json"),
        ("not-an-object", "[]"),
    ):
        check(bool(lines_source(lines_file(f"noform-{name}", content)).problem), f"lines.json's source: {name} has no form, and proves nothing")
    check(bool(lines_source(tmp / "no-such-lines.json").problem), "lines.json's source: no file proves nothing")
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

    # If the fetch layer stops going through the opener this script watches, the `Link`
    # lines go unseen: that is a refusal, not an answer with none. The same stand-in
    # records what each listing is asked for.
    unwatched = fetch_from_bases
    asked: list[object] = []

    def stand_in(*_a, **kw):
        asked.append(kw.get("query"))
        return _http.HttpResponse(200, {}, b'{"tags": []}', "planted")

    globals()["fetch_from_bases"] = stand_in
    try:
        _read_listing((), "a listing", FetchPolicy(), RetryPolicy())
        unseen = ""
    except ChannelRefused as refused:
        unseen = refused.name
    finally:
        globals()["fetch_from_bases"] = unwatched
    check(unseen == "listing", "an answer the Link watch did not see: refused [listing]")
    check(LISTING_PAGE == 1000 and asked == ["n=1000"], "a listing is asked for with n=1000")

    # The opener the watch swaps in is put back when the read fails, not only when it succeeds.
    real_opener = _http._opener

    def failing(*_a, **_k):
        raise TransportError("planted")

    globals()["fetch_from_bases"] = failing
    try:
        _read_listing((), "a listing", FetchPolicy(), RetryPolicy())
        failed = ""
    except ChannelRefused as refused:
        failed = refused.name
    finally:
        globals()["fetch_from_bases"] = unwatched
    check(failed == "listing" and _http._opener is real_opener, "a failed listing read puts the fetch layer's opener back")

    # lines.json's source is read with the reader that refuses a repeated member.
    check(
        bool(lines_source(lines_file("repeated-source", '{"source": "' + lines_rules.FIXTURE_SOURCE + '", "source": "' + NEWEST + '"}')).problem),
        "lines.json's source: a repeated `source` member is not read as its last one",
    )

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
    said_crash.clear()
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        stderr, sys.stderr = sys.stderr, devnull
        try:
            _guarded(boom, said_crash.append, "--out x.md")
        finally:
            sys.stderr = stderr
    check(verdict_line(EXIT_CRASH, False, "--out x.md") in said_crash and "judged --out x.md" in said_crash[-1],
          "a crash on a selection names it, like every other verdict")  # fmt: skip

    tmp_ctx.cleanup()
    if failures:
        print(f"support-v2 gen.py --selftest: {len(failures)} FAILED", file=sys.stderr)
        sys.exit(1)
    print(
        f"support-v2 gen.py --selftest: OK ({len(plants)} plants, {len(results)} registry cases, "
        f"{cli_runs} command-line runs, each process's status equal to its verdict line)"
    )
    return 0


def _cli_selftest(check, tmp: Path, port: int, lines_doc, supported, fixture, page, keyid, pub, other, cases) -> int:
    """main() in a real process, in every mode, on a planted tree: nothing else reads
    the status CI reads. Each run must end with one verdict line, whose status is the
    process's and the one expected. `cases` are registries served for this."""
    tmp.mkdir()
    verdict_re = re.compile(r"^support-v2: verdict: .*\(exit ([0-9]+)\)", re.M)
    me = str(Path(__file__).resolve())
    env_base = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS" and not k.startswith("CHTYPES_")}
    runs = 0

    def setup(case: str, keys_hex: str = pub, crash: bool = False) -> str:
        return json.dumps(
            {
                "base": f"http://127.0.0.1:{port}/s-{case}/chtypes/v1",
                "keys": [{"keyid": keyid if keys_hex == pub else hashlib.sha256(bytes.fromhex(keys_hex)).hexdigest()[:16], "ed25519_hex": keys_hex}],
                "crash": crash,
                "src": str(ROOT / "python" / "src"),
            }
        )

    def cli(
        what: str,
        args: list[str],
        want: int,
        *,
        case: str = "",
        keys_hex: str = pub,
        crash: bool = False,
        said: tuple[str, ...] = (),
        unsaid: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        script: str = me,
        cwd: Path = tmp,
    ) -> subprocess.CompletedProcess:
        """`case` runs the script as `__main__` under that fixture channel (CLI_DRIVER);
        no case runs the script itself as a command, which is only ever an offline
        mode or a usage error."""
        nonlocal runs
        runs += 1
        command = (
            [sys.executable, "-c", CLI_DRIVER, script, setup(case, keys_hex, crash), *args]
            if case
            else [sys.executable, script, *args]
        )
        r = subprocess.run(command, capture_output=True, text=True, cwd=cwd, timeout=120, env={**env_base, **(env or {})})
        found = verdict_re.findall(r.stderr)
        agrees = r.returncode == want and (want == EXIT_USAGE and not case and not found or found == [str(want)])
        missing = [x for x in said if x not in r.stderr]
        spoken = [x for x in unsaid if x in r.stderr]
        check(
            agrees and not missing and not spoken,
            f"command line, {what}: status {want} and its verdict line"
            + ("" if agrees and not missing and not spoken else f" (status {r.returncode}, verdicts {found}, missing {missing}, said {spoken}: {r.stderr[-400:]})"),
        )
        return r

    def write(name: str, content: str) -> str:
        path = tmp / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    tag = NEWEST
    agreed = write("agreed.json", lines_doc(tag, supported))
    at_fixture = write("at-fixture.json", lines_doc(lines_rules.FIXTURE_SOURCE, supported))
    made_up = write("made-up.json", lines_doc("20261105T000000Z", supported))
    dropped_tag = write("dropped-tag.json", lines_doc(tag, supported[:-1]))
    dropped_fixture = write("dropped-fixture.json", lines_doc(lines_rules.FIXTURE_SOURCE, supported[:-1]))
    dropped_reworded = write("dropped-reworded.json", lines_doc("a note", supported[:-1]))
    no_form = write("no-form.json", lines_doc("a note", supported))

    def page_of(case: str) -> str:
        """The page the child's channel generates, a file it must agree with, and stale."""
        channel = replace(_channel.FETCH_V1_CHANNEL, name="selftest", bases=(f"http://127.0.0.1:{port}/s-{case}/chtypes/v1",))
        return render(channel_context(channel))

    def selected(page_path: str, lines_path: str) -> list[str]:
        return ["--out", page_path, "--lines", lines_path]

    def release_flow(document_text: str, document_tag: str) -> None:
        """RELEASING.md's steps after the first publication, end to end, on a copy of
        the tree: print the verified document, write lines.json from it, regenerate the
        page, then every judge of the result. A step that works alone and leaves a judge
        failing (a selftest that assumed the committed lines.json) fails here, where CI
        would have found it after the merge."""
        flow = tmp / "flow"
        for sub in ("support-v2", "abi-v2", "lib"):
            shutil.copytree(ROOT / "scripts" / sub, flow / "scripts" / sub, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "spec", flow / "spec")
        shutil.copytree(ROOT / "docs", flow / "docs")
        for entry in ROOT.iterdir():
            if entry.name not in ("scripts", "spec", "docs", ".git", ".worktrees"):
                (flow / entry.name).symlink_to(entry)
        where = flow / "spec" / "abi-v2" / "where-settings"
        writer = _script(flow / "scripts" / "abi-v2" / "regen-where-settings.py", "regen_for_the_flow")
        # The flow starts where a repository is before the first publication: lines.json at
        # the fixture marker. Only possible while its lines are the fixture's.
        raw, readme = lines_rules.load(where)
        lines_now = json.loads(raw[lines_rules.LINES_FILE])["lines"]
        if lines_now != ChannelDocument("", json.loads(CHANNEL_FIXTURE.read_bytes())).supported:
            check(True, "the release flow: skipped, lines.json no longer names the fixture's lines (the flow is the one before the first publication)")
            return
        raw[lines_rules.LINES_FILE] = (
            json.dumps({"schema": lines_rules.LINES_SCHEMA, "source": lines_rules.FIXTURE_SOURCE, "lines": lines_now}, indent=1) + "\n"
        ).encode()
        (where / lines_rules.LINES_FILE).write_bytes(raw[lines_rules.LINES_FILE])
        (where / "README.md").write_text(writer.with_table(readme, raw), encoding="utf-8")
        (flow / "document.json").write_text(document_text, encoding="utf-8")
        copy_of_me = str(flow / "scripts" / "support-v2" / "gen.py")
        stdlib = {
            name: str(flow / "scripts" / "abi-v2" / f"{name}.py")
            for name in ("check-where-settings", "regen-where-settings", "check-documented-platform")
        }

        def runs_clean(what: str, *argv: str) -> None:
            r = subprocess.run([sys.executable, "-I", *argv], capture_output=True, text=True, cwd=flow, timeout=300, env=env_base)
            check(r.returncode == 0, f"the release flow, {what}: exit 0" + ("" if r.returncode == 0 else f" (got {r.returncode}: {(r.stdout + r.stderr)[-500:]})"))

        runs_clean("the dry-run state passes check-where-settings --check", stdlib["check-where-settings"], "--check")
        runs_clean("regen-where-settings --lines-document --lines-source <tag>", stdlib["regen-where-settings"], "--lines-document", "document.json", "--lines-source", document_tag)
        state = json.loads((where / lines_rules.LINES_FILE).read_text(encoding="utf-8"))
        check(state["source"] == document_tag != "", "the release flow: lines.json now names the publication's tag")
        cli("the release flow, --write", ["--write"], EXIT_OK, case="cli-published", script=copy_of_me, cwd=flow)
        cli("the release flow, --check against the channel", ["--check"], EXIT_OK, case="cli-published", script=copy_of_me, cwd=flow, said=("channel statement",))
        runs_clean("check-where-settings --check on the result", stdlib["check-where-settings"], "--check")
        runs_clean("check-where-settings --selftest on the result", stdlib["check-where-settings"], "--selftest")
        runs_clean("regen-where-settings --selftest on the result", stdlib["regen-where-settings"], "--selftest")
        runs_clean("check-documented-platform --selftest on the result", stdlib["check-documented-platform"], "--selftest")
        runs_clean("check-documented-platform --check on the result", stdlib["check-documented-platform"], "--check")

    check(set(cases) == {"cli-published", "cli-unpublished", "cli-refused", "cli-line-failed"}, "the command-line registries are the ones used below")

    # A document published, every supported line built.
    case = "cli-published"
    text = page_of(case)
    good = write("good.md", text)
    stale = write("stale.md", text + "\n")
    cli("--check, all agrees", ["--check", *selected(good, agreed)], EXIT_OK, case=case, said=("channel statement", "verdict: ok (exit 0)", "--out", "--lines", "not the committed files"))
    cli("--check, a stale page", ["--check", *selected(stale, agreed)], EXIT_DIFF, case=case, said=("stale.md differs",))
    cli("--check, a missing page", ["--check", *selected(str(tmp / "absent.md"), agreed)], EXIT_DIFF, case=case, said=("absent.md differs",))
    cli("--check, lines.json misses a line", ["--check", *selected(good, dropped_tag)], EXIT_DIFF, case=case, said=("dropped-tag.json names",))
    cli("--check, lines.json still at the fixture", ["--check", *selected(good, at_fixture)], EXIT_DIFF, case=case, said=("the latch is not set",))
    cli("--check, lines.json's source of no form", ["--check", *selected(good, no_form)], EXIT_DIFF, case=case, said=("neither the fixture marker",))
    cli("--check, lines.json names a tag the listing does not", ["--check", *selected(good, made_up)], EXIT_CHANNEL, case=case, said=("refused [lines-source]",))
    # An environment that tries to redirect the base and the trust changes nothing: the
    # channel is pinned, not read from the environment. The base here answers nothing.
    cli(
        "--check, the environment tries to redirect the base and the trust",
        ["--check", *selected(good, agreed)],
        EXIT_OK,
        case=case,
        said=("channel statement", "verdict: ok (exit 0)"),
        env={C.ENV_BASES_NAME: "http://127.0.0.1:1/chtypes/v2-dev", C.ENV_TRUSTED_KEYS_NAME: other},
    )
    r = cli("no mode prints the page", selected(str(tmp / "absent.md"), agreed), EXIT_OK, case=case)
    check(r.stdout == text, "command line, no mode: stdout is the page")
    written = tmp / "written.md"
    cli("--write writes", ["--write", *selected(str(written), agreed)], EXIT_OK, case=case)
    check(written.is_file() and written.read_text(encoding="utf-8") == text, "command line, --write: the file is the page")
    r = cli("--print-document", ["--print-document", *selected(good, agreed)], EXIT_OK, case=case, said=("printing the channel statement",))
    document_text = r.stdout
    found_tag = re.search(r"printing the channel statement ([0-9T]+Z)", r.stderr)
    document_tag = found_tag.group(1) if found_tag else ""
    document = {**fixture, "repository": f"s-{case}/chtypes/v1"}
    check(json.loads(r.stdout or "null") == document, "command line, --print-document: stdout is the signed document")

    # Nothing published.
    case = "cli-unpublished"
    text = page_of(case)
    good = write("good-unpublished.md", text)
    stale = write("stale-unpublished.md", text + "\n")
    words = ("live check off: no channel statement published yet", "verdict: ok, the live comparison NOT JUDGED (exit 0)")
    cli("--check, the fixture state", ["--check", *selected(good, at_fixture)], EXIT_OK, case=case, said=words, unsaid=("::warning",))
    cli("--check, the fixture state, in CI", ["--check", *selected(good, at_fixture)], EXIT_OK, case=case, said=(*words, "::warning title=support-v2::live check off"), env={"GITHUB_ACTIONS": "true"})
    cli("--check, a stale page", ["--check", *selected(stale, at_fixture)], EXIT_DIFF, case=case, said=("differs",))
    cli("--check, a line dropped", ["--check", *selected(good, dropped_fixture)], EXIT_DIFF, case=case, said=("dry-run fixture",))
    cli("--check, latched", ["--check", *selected(good, agreed)], EXIT_CHANNEL, case=case, said=("refused [no-document]",), unsaid=("NOT JUDGED",))
    cli("--check, a line dropped behind a made-up tag", ["--check", *selected(good, dropped_tag)], EXIT_CHANNEL, case=case, said=("refused [no-document]",))
    cli("--check, a source of no form", ["--check", *selected(good, no_form)], EXIT_CHANNEL, case=case, said=("refused [lines-source]",))
    cli("--check, no lines.json", ["--check", *selected(good, str(tmp / "absent.json"))], EXIT_CHANNEL, case=case, said=("refused [lines-source]",))
    r = cli("--print-document, nothing published", ["--print-document", *selected(good, at_fixture)], EXIT_USAGE, case=case, said=("no channel document to print",))
    check(r.stdout == "", "command line, --print-document with nothing published: stdout is empty")
    unwritten = tmp / "unwritten-latched.md"
    cli("--write, latched", ["--write", *selected(str(unwritten), agreed)], EXIT_CHANNEL, case=case)
    check(not unwritten.exists(), "command line, --write latched: nothing was written")
    cli("--check, every statement bad", ["--check", *selected(good, at_fixture)], EXIT_NOTHING, case=case, keys_hex=other, said=("no platform of any line verified",))
    cli("--check, a crash", ["--check", *selected(good, at_fixture)], EXIT_CRASH, case=case, crash=True, said=("RuntimeError escaped",))

    # The document refused.
    case = "cli-refused"
    good = write("good-refused.md", page_of(case))
    cli("--check, refused", ["--check", *selected(good, agreed)], EXIT_CHANNEL, case=case, said=("refused [artifact-type]",))
    r = cli("no mode, refused", selected(good, agreed), EXIT_CHANNEL, case=case)
    check(r.stdout == "", "command line, no mode refused: stdout is empty")
    unwritten = tmp / "unwritten-refused.md"
    cli("--write, refused", ["--write", *selected(str(unwritten), agreed)], EXIT_CHANNEL, case=case)
    check(not unwritten.exists(), "command line, --write refused: nothing was written")
    r = cli("--print-document, refused", ["--print-document", *selected(good, agreed)], EXIT_CHANNEL, case=case)
    check(r.stdout == "", "command line, --print-document refused: stdout is empty")

    # A supported line with no build: the page is produced, and the status says 3.
    case = "cli-line-failed"
    text = page_of(case)
    good = write("good-failed.md", text)
    cli("--check, a line failed", ["--check", *selected(good, agreed)], EXIT_PARTIAL, case=case, said=("26.7: supported, and no platform", "not comparing"))
    r = cli("no mode, a line failed", selected(good, agreed), EXIT_PARTIAL, case=case)
    check(r.stdout == text, "command line, no mode with a line failed: the page is still printed")
    written = tmp / "written-failed.md"
    cli("--write, a line failed", ["--write", *selected(str(written), agreed)], EXIT_PARTIAL, case=case)
    check(written.is_file(), "command line, --write with a line failed: the page is still written")
    r = cli("--print-document, a line failed", ["--print-document", *selected(good, agreed)], EXIT_PARTIAL, case=case)
    check(bool(r.stdout), "command line, --print-document with a line failed: the document is printed, the status is 3")

    release_flow(document_text, document_tag)

    # --check-page needs no channel: this is the script itself, as a command. Its page is
    # the active channel's, the dev one in a real process.
    good = write("good-page.md", page)
    stale = write("stale-page.md", page + "\n")
    cli("--check-page, all agrees", ["--check-page", *selected(good, at_fixture)], EXIT_OK, said=("no registry read", "--out", "--lines", "not the committed files"))
    cli("--check-page, a stale page", ["--check-page", *selected(stale, at_fixture)], EXIT_DIFF, said=("stale-page.md differs",))
    cli("--check-page, a missing page", ["--check-page", *selected(str(tmp / "absent.md"), at_fixture)], EXIT_DIFF, said=("absent.md differs",))
    cli("--check-page, a line dropped", ["--check-page", *selected(good, dropped_fixture)], EXIT_DIFF, said=("dry-run fixture",))
    cli("--check-page, a source of no form with a line dropped", ["--check-page", *selected(good, dropped_reworded)], EXIT_DIFF, said=("neither the fixture marker",))
    cli("--check-page, a publication tag is judged live, not here", ["--check-page", *selected(good, dropped_tag)], EXIT_OK)
    # With no selection, on the committed tree: the status must still be the verdict's.
    r = subprocess.run([sys.executable, me, "--check-page"], capture_output=True, text=True, cwd=tmp, timeout=120, env=env_base)
    runs += 1
    found = verdict_re.findall(r.stderr)
    check(found == [str(r.returncode)] and r.returncode in (EXIT_OK, EXIT_DIFF), "command line, --check-page on the committed tree: its status is its verdict line's")
    check("not the committed files" not in r.stderr, "command line, the default selection names nothing")
    cli("two modes at once", ["--check", "--write"], EXIT_USAGE)
    return runs


def _guarded(body: Callable[[], int], say, selection: str = "") -> int:
    """Run `body`. An exception here is a defect of this script, and Python's own
    exit status for it (1) would read as `a committed file differs`."""
    try:
        return body()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        say(f"support-v2: {type(exc).__name__} escaped this script")
        say(verdict_line(EXIT_CRASH, False, selection))
        return EXIT_CRASH


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="write docs/support-v2.md")
    g.add_argument("--check", action="store_true", help="fail unless the page and lines.json are current")
    g.add_argument("--check-page", action="store_true", help="the same for what needs no registry")
    g.add_argument("--print-document", action="store_true", help="print the verified channel document")
    g.add_argument("--selftest", action="store_true", help="prove the rules on a fixture registry")
    ap.add_argument("--out", type=Path, default=PAGE, help=argparse.SUPPRESS)
    ap.add_argument("--lines", type=Path, default=LINES_JSON, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    picked = [("--out", args.out, PAGE), ("--lines", args.lines, LINES_JSON)]
    selection = " ".join(f"{flag} {value}" for flag, value, default in picked if value.resolve() != default)
    mode = (
        "write" if args.write else "check" if args.check else "document" if args.print_document else "print"
    )

    def say(message: str) -> None:
        print(message, file=sys.stderr)

    if args.check_page:
        return _guarded(lambda: check_page(args.out, args.lines, say, selection), say, selection)
    return _guarded(lambda: run(mode, args.out, args.lines, say, selection), say, selection)


if __name__ == "__main__":
    sys.exit(main())
