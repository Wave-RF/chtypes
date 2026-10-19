"""Build-level revocation (docs/guides/fetch-v1.md, "Build-level revocation";
public issue #625).

A revocation is a signed referrer of a platform manifest, read from the listing
its signature lookup reads; one that verifies refuses the build with
`CHTYPES_ARTIFACT_REVOKED` and is stored in the build's cache entry, and one that
does not is ignored with one warning. A 410 whose body's
`errors[0].detail.revoked` is an object is the same code (`_retired.revoked_410`,
`_http.gone_error`). A status that cannot be read fails open with one warning, or
in strict mode is the transport's error. Generation 2 only (`Channel.revocation`):
the v1 contract has none of it.

Four paths check, each with its own budget:

* an install reads its revocation candidates from the trust listing it already
  made (`_ensure._ensure_floating`), under the retry table;
* `--frozen` reads one listing, warm or cold, under the retry table;
* a build already installed and used without a download (a registry open,
  preload, an ensure that finds it installed) is checked at most once per process
  (`_memo`), one attempt per base, no backoff, and one deadline
  (`REVOCATION_CHECK_S`) for the whole check;
* `--lock` refuses a revoked build on every platform it pins.

The functions here take what they need in a `Context` rather than an `Options`
(which lives in `_ensure`, and imports this module). A function that can fail
after it has warned appends to the `warnings` list it is given, so a caller that
catches the failure still has them.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from chtypes._advisory import Advisory, Revocation
from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import TrustedKey, verify_bundle
from chtypes._ocifetch._errors import (
    ArtifactCorruptError,
    ArtifactRevokedError,
    FetchError,
    SourceForbiddenError,
    SourceUnauthorizedError,
    SourceUnreachableError,
)
from chtypes._ocifetch._http import (
    FetchPolicy,
    ForbiddenHttpError,
    GoneHttpError,
    OversizeHttpError,
    RetryPolicy,
    TransportError,
    UnauthorizedHttpError,
    fetch_from_bases,
)
from chtypes._ocifetch._layout import _atomic_write
from chtypes._ocifetch._oci import (
    Descriptor,
    fetch_manifest_by_digest,
    parse_digest,
    verify_json_descriptor,
)
from chtypes._ocifetch._referrers import ReferrerListing, read_listing
from chtypes._ocifetch._retired import sanitize

__all__ = [
    "Context",
    "Found",
    "Outcome",
    "Subject",
    "Unread",
    "check_candidates",
    "check_frozen_installed",
    "check_installed_once",
    "check_installed_stored",
    "check_listing",
    "check_stored",
    "outcome_of_listing",
    "reset_memo_for_tests",
    "revoked_text",
    "settle",
    "store_pending",
    "subject_of",
    "subject_of_statement",
    "verify_revocation",
    "write_revocation_record",
]

# The installed-build check's one deadline, in seconds: `REVOCATION_CHECK_S`; a
# module variable so a test can shorten it.
_check_deadline_s: float = C.REVOCATION_CHECK_S


@dataclass(frozen=True)
class Context:
    """What the checks read of a call's options: whether the contract has
    revocation at all, the bases and transport, the trust, the override and strict
    mode, offline, and the user cache root (the only place a record is written)."""

    enabled: bool
    bases: tuple[str, ...]
    policy: FetchPolicy
    retry: RetryPolicy
    trusted_keys: tuple[TrustedKey, ...]
    allow_revoked: bool
    require_check: bool
    offline: bool
    cache_root: Path


@dataclass(frozen=True)
class Subject:
    """The build a check concerns: its own digests and signed predicate, and its
    installed entry (`dir`) when it has one, `writable` when that entry is in the
    user cache."""

    manifest: str
    layer: str
    predicate: dict
    version: str
    platform: str
    build: str
    request: str
    dir: Path | None
    writable: bool


@dataclass(frozen=True)
class Found:
    """One verified revocation: the bundle, for the stored record, and what it
    says."""

    bundle: bytes
    rev: Revocation


@dataclass(frozen=True)
class Unread:
    """Why a status could not be read: the cause's text, and the code strict mode
    gives it (`unauthorized`, `forbidden` or `unreachable`)."""

    text: str
    kind: str


@dataclass
class Outcome:
    """What one check learned: a verified revocation, the warnings for the
    candidates it ignored, and, when it could not read the status, why."""

    verified: Found | None = None
    warnings: list[str] | None = None
    unread: Unread | None = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


def _under(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return str(rel) != "."


def subject_of(ctx: Context, res) -> Subject:  # noqa: ANN001 - a _ensure.Resolved
    """The subject an installed build's `Resolved` names."""
    directory = Path(res.dir) if res.dir else None
    return Subject(
        manifest=res.digests["manifest"],
        layer=res.digests["layer"],
        predicate=res.predicate,
        version=res.version,
        platform=res.platform,
        build=res.build,
        request=res.request,
        dir=directory,
        writable=directory is not None and _under(ctx.cache_root, directory),
    )


def subject_of_statement(
    predicate: dict, manifest: str, layer: str, platform: str, request: str
) -> Subject:
    """The subject of a build not yet installed: the signed statement verified
    for it."""

    def text(name: str) -> str:
        value = predicate.get(name)
        return value if isinstance(value, str) else ""

    return Subject(
        manifest=manifest,
        layer=layer,
        predicate=predicate,
        version=text("clickhouse_version"),
        platform=platform,
        build=text("build"),
        request=request,
        dir=None,
        writable=False,
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def verify_revocation(
    body: bytes, keys: tuple[TrustedKey, ...], sub: Subject
) -> tuple[Revocation | None, str]:
    """Check one revocation bundle against `sub` (the guide's five rules): the
    signature under `keys`, the statement and predicate types, the subject and
    manifest, the build's identity, and the advisory's shape. It returns the
    Revocation, or None and why it does not verify. A signed payload that is not
    JSON is ignored too, never `CHTYPES_ARTIFACT_CORRUPT`."""
    try:
        bundle = json.loads(body.decode("utf-8"))
        if not isinstance(bundle, dict):
            return None, "the bundle is not a JSON object"
        verified = verify_bundle(bundle, keys)
    except (ArtifactCorruptError, ValueError, RecursionError) as exc:
        return None, str(exc)
    if verified is None:
        return None, "no signature verifies under a trusted key"
    st = verified.statement
    if st.statement_type != C.STATEMENT_TYPE:
        return None, f"statement _type {st.statement_type!r}, expected {C.STATEMENT_TYPE!r}"
    if st.predicate_type != C.PREDICATE_TYPE_REVOCATION:
        return None, (
            f"predicateType {st.predicate_type!r}, expected {C.PREDICATE_TYPE_REVOCATION!r}"
        )
    if parse_digest(sub.layer) not in st.subject_sha256:
        return None, f"no subject digest is the build's layer {sub.layer}"
    pred = st.predicate
    manifest = pred.get("manifest")
    if manifest != sub.manifest:
        return None, f"predicate manifest {manifest!r} is not the build's {sub.manifest}"
    got, want = _as_int(pred.get("abi")), _as_int(sub.predicate.get("abi"))
    if got is None or want is None or got != want:
        return None, (
            f"predicate abi {pred.get('abi')} is not the build's {sub.predicate.get('abi')}"
        )
    for name in ("abi_fingerprint", "os", "arch", "clickhouse_version", "build"):
        g, w = pred.get(name), sub.predicate.get(name)
        if not isinstance(g, str) or not isinstance(w, str) or g != w:
            return None, f"predicate {name} {g} is not the build's {w}"
    advisory = pred.get("advisory")
    advisory = advisory if isinstance(advisory, dict) else {}
    adv_id, adv_url = advisory.get("id"), advisory.get("url")
    if not isinstance(adv_id, str) or not adv_id or not isinstance(adv_url, str) or not adv_url:
        return None, "predicate advisory does not name an id and a URL"
    strs: dict[str, str] = {}
    for name in ("class", "summary", "superseded_by", "revoked_at"):
        value = pred.get(name)
        if not isinstance(value, str):
            return None, f"predicate {name} is not a string"
        strs[name] = value
    return (
        Revocation(
            signed=True,
            manifest=sub.manifest,
            advisories=(
                Advisory(
                    id=sanitize(adv_id),
                    url=sanitize(adv_url),
                    class_=sanitize(strs["class"]),
                    summary=sanitize(strs["summary"]),
                    build=sub.build,
                    superseded_by=sanitize(strs["superseded_by"]),
                    revoked_at=sanitize(strs["revoked_at"]),
                ),
            ),
        ),
        "",
    )


def revoked_text(sub: Subject, rev: Revocation) -> str:
    """The guide's message for a verified revocation: `<version> <platform> build
    <build> (manifest <digest>) is revoked: <id> <url> (<class>); superseded by
    build <superseded_by>: <summary>`, a part whose field is empty left out."""
    a = rev.advisories[0]
    msg = (
        f"{sub.version} {sub.platform} build {sub.build} (manifest {sub.manifest}) "
        f"is revoked: {a.id} {a.url}"
    )
    if a.class_:
        msg += f" ({a.class_})"
    if a.superseded_by:
        msg += f"; superseded by build {a.superseded_by}"
    if a.summary:
        msg += f": {a.summary}"
    return msg


def _redacted(base: str) -> str:
    """`base` with any password replaced by `xxxxx`, as Go's `url.Redacted`."""
    parts = urlsplit(base)
    if parts.password is None:
        return base
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    return parts._replace(netloc=f"{user}:xxxxx@{host}").geturl()


def _not_checked_text(ctx: Context, sub: Subject, cause: str) -> str:
    """The guide's fail-open warning, without the `chtypes: ` every warning starts
    with: `revocation status not checked for manifest <digest> (bases: <base>,
    <base>): <cause>`."""
    shown = ", ".join(_redacted(b) for b in ctx.bases)
    return f"revocation status not checked for manifest {sub.manifest} (bases: {shown}): {cause}"


def _unread_of(exc: BaseException, text: str | None = None) -> Unread:
    kind = "unreachable"
    if isinstance(exc, (UnauthorizedHttpError, SourceUnauthorizedError)):
        kind = "unauthorized"
    elif isinstance(exc, (ForbiddenHttpError, SourceForbiddenError)):
        kind = "forbidden"
    return Unread(text if text is not None else str(exc), kind)


def write_revocation_record(directory: Path, bundle: bytes) -> None:
    """Store a verified revocation's bundle, byte for byte, in an installed
    build's entry: a temporary file, then a rename."""
    dest = directory / C.CACHE_REVOCATION_RECORD
    try:
        if dest.read_bytes() == bundle:
            return
    except OSError:
        pass
    _atomic_write(dest, bundle)


def store_pending(ctx: Context, res, pending: Found | None) -> None:  # noqa: ANN001
    """Store the revocation an override let a call install past, in the entry it
    installed."""
    if pending is not None and res is not None and res.dir and _under(ctx.cache_root, res.dir):
        try:
            write_revocation_record(Path(res.dir), pending.bundle)
        except OSError:
            pass  # a read-only cache still refuses, for this call and process


def settle(ctx: Context, sub: Subject, out: Outcome, warnings: list[str], *, store: bool) -> None:
    """Turn an outcome into the call's answer: a verified revocation is stored in
    an installed, writable entry (unless `store` is false) and refuses the build,
    or with the override becomes one warning; an unread status is one warning, or
    in strict mode the transport's error."""
    warnings.extend(out.warnings or [])
    if out.verified is not None:
        if store and sub.writable and sub.dir is not None:
            try:
                write_revocation_record(sub.dir, out.verified.bundle)
            except OSError:
                pass  # a read-only cache still refuses, for this call and process
        text = revoked_text(sub, out.verified.rev)
        if ctx.allow_revoked:
            warnings.append("chtypes: allowing a revoked build: " + text)
            return
        raise ArtifactRevokedError(f"chtypes: {text}", revocation=out.verified.rev)
    if out.unread is not None:
        text = _not_checked_text(ctx, sub, out.unread.text)
        if ctx.require_check:
            if out.unread.kind == "unauthorized":
                raise SourceUnauthorizedError(f"chtypes: {text}")
            if out.unread.kind == "forbidden":
                raise SourceForbiddenError(f"chtypes: {text}")
            raise SourceUnreachableError(f"chtypes: {text}")
        warnings.append("chtypes: " + text)


def check_stored(ctx: Context, sub: Subject, warnings: list[str]) -> bool:
    """Read `sub`'s stored record, if its entry holds one: verified, it decides
    the call (True, or the refusal raised); one that does not verify is a warning,
    and the call goes on (False)."""
    if sub.dir is None:
        return False
    path = sub.dir / C.CACHE_REVOCATION_RECORD
    try:
        body = path.read_bytes()
    except OSError:
        return False
    rev, reason = verify_revocation(body, ctx.trusted_keys, sub)
    if rev is None:
        warnings.append(
            f"chtypes: revocation record {path} for manifest {sub.manifest} "
            f"does not verify and is ignored: {reason}"
        )
        return False
    settle(ctx, sub, Outcome(verified=Found(body, rev)), warnings, store=False)
    return True


def check_installed_stored(ctx: Context, res, warnings: list[str]) -> None:  # noqa: ANN001
    """The check every call that answers from the cache with no request makes
    (`--offline`, `--frozen --offline`): the stored record."""
    if not ctx.enabled or res is None:
        return
    check_stored(ctx, subject_of(ctx, res), warnings)


def _fetch_candidate(
    ctx: Context, cand: Descriptor, *, policy: FetchPolicy, retry: RetryPolicy
) -> bytes:
    """A revocation referrer's bundle bytes: its manifest by digest, checked
    against the listing's descriptor, then its one layer, at most
    `BUNDLE_MAX_BYTES`, checked against its own descriptor."""
    doc, raw = fetch_manifest_by_digest(ctx.bases, cand.digest, policy=policy, retry=retry)
    verify_json_descriptor(raw, cand, what=f"referrer manifest {cand.digest}")
    layers = doc.get("layers")
    if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], dict):
        raise ArtifactCorruptError(
            f"referrer manifest {cand.digest} carries "
            f"{len(layers) if isinstance(layers, list) else 'no'} layers, expected exactly 1"
        )
    layer = layers[0]
    layer_desc = Descriptor(
        media_type=layer.get("mediaType", ""),
        digest=layer.get("digest", ""),
        size=layer.get("size", -1),
    )
    resp = fetch_from_bases(
        ctx.bases,
        f"/blobs/{layer_desc.digest}",
        mode="digest",
        accept=None,
        max_bytes=C.BUNDLE_MAX_BYTES,
        policy=policy,
        retry=retry,
    )
    verify_json_descriptor(resp.body, layer_desc, what=f"referrer blob {layer_desc.digest}")
    return resp.body


def check_candidates(
    ctx: Context,
    cands: Sequence[Descriptor],
    sub: Subject,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
) -> Outcome:
    """Fetch and verify each revocation candidate in turn; the first that
    verifies is the answer. A 410 is raised (`GoneHttpError`)."""
    out = Outcome()
    for cand in cands:
        try:
            body = _fetch_candidate(ctx, cand, policy=policy, retry=retry)
            rev, reason = verify_revocation(body, ctx.trusted_keys, sub)
        except GoneHttpError:
            raise
        except (ArtifactCorruptError, OversizeHttpError) as exc:
            body, rev, reason = b"", None, str(exc)
        except (TransportError, FetchError) as exc:
            out.unread = _unread_of(exc, f"revocation {cand.digest} could not be read: {exc}")
            continue
        if rev is None:
            out.warnings.append(
                f"chtypes: revocation {cand.digest} for manifest {sub.manifest} "
                f"does not verify and is ignored: {reason}"
            )
            continue
        out.verified = Found(bundle=body, rev=rev)
        return out
    return out


def check_listing(
    ctx: Context, sub: Subject, *, policy: FetchPolicy, retry: RetryPolicy
) -> Outcome:
    """One network check of `sub`: its listing, then its candidates. A listing
    that names no signature referrer is an unread status."""
    listing = read_listing(ctx.bases, sub.manifest, policy=policy, retry=retry)
    return outcome_of_listing(ctx, listing, sub, policy=policy, retry=retry)


def outcome_of_listing(
    ctx: Context,
    listing: ReferrerListing,
    sub: Subject,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
) -> Outcome:
    if not listing.found:
        failure = listing.failure
        if failure is None:
            return Outcome(
                unread=Unread("no listing names a signature for this manifest", "unreachable")
            )
        return Outcome(unread=_unread_of(failure))
    return check_candidates(ctx, listing.revocations, sub, policy=policy, retry=retry)


def check_frozen_installed(ctx: Context, res, warnings: list[str]) -> None:  # noqa: ANN001
    """`--frozen`'s check of the build installed under its pin: the stored record,
    then one listing under the retry table."""
    if not ctx.enabled:
        return
    sub = subject_of(ctx, res)
    if check_stored(ctx, sub, warnings):
        return
    out = check_listing(ctx, sub, policy=ctx.policy, retry=ctx.retry)
    settle(ctx, sub, out, warnings, store=True)


# The installed-build check's in-process outcomes, by platform manifest digest:
# at most one check per build per process.
_NOT_REVOKED = "not-revoked"
_REVOKED = "revoked"
_NOT_CHECKED = "not-checked"


class _MemoEntry:
    __slots__ = ("done", "found", "state")

    def __init__(self) -> None:
        self.done = threading.Event()  # set when the check has an outcome
        self.state: str | None = None
        self.found: Found | None = None  # _REVOKED's revocation


_memo_lock = threading.Lock()
_memo: dict[str, _MemoEntry] = {}


def reset_memo_for_tests() -> None:
    """Forget every kept outcome, as a new process would. A conformance runner
    calls it before every case, because the cases reuse manifests across cache
    directories, which one process never sees in use. Raises outside a test."""
    _channel._test_only("reset_revocation_memo_for_tests")
    with _memo_lock:
        _memo.clear()


def _keep(manifest: str, out: Outcome) -> None:
    """Record `out` as `manifest`'s kept outcome, replacing a "not checked"."""
    with _memo_lock:
        entry = _memo.get(manifest)
        if entry is None:
            return
        if out.verified is not None:
            entry.state, entry.found = _REVOKED, out.verified
        else:
            entry.state = _NOT_REVOKED


def _check_with_deadline(ctx: Context, sub: Subject) -> Outcome:
    """One network check under the installed-build check's budget: one attempt
    per request, no backoff, one deadline for the whole. Passing the deadline is
    an unread status."""
    deadline = time.monotonic() + _check_deadline_s
    policy = replace(ctx.policy, deadline=deadline)
    retry = replace(ctx.retry, attempts=1)
    out = check_listing(ctx, sub, policy=policy, retry=retry)
    if out.verified is None and out.unread is not None and time.monotonic() >= deadline:
        out.unread = Unread(
            f"the check passed its {_check_deadline_s:g}s deadline: {out.unread.text}",
            out.unread.kind,
        )
    return out


def check_installed_once(ctx: Context, res, warnings: list[str]) -> None:  # noqa: ANN001
    """The installed-build check: the stored record, then, unless offline, the
    kept outcome or one check of its own, with one attempt per base, no backoff and
    one deadline. Concurrent checks of one build share one; a kept "not checked"
    answers only a call without strict mode, and a check that ends in an error keeps
    nothing. Raises the refusal, or in strict mode the transport's error; a 410 is
    raised as `GoneHttpError`."""
    if not ctx.enabled or res is None:
        return
    sub = subject_of(ctx, res)
    if check_stored(ctx, sub, warnings) or ctx.offline:
        return
    while True:
        with _memo_lock:
            entry = _memo.get(sub.manifest)
            lead = entry is None
            if entry is None:
                entry = _memo[sub.manifest] = _MemoEntry()

        if not lead:
            entry.done.wait()
            with _memo_lock:
                current = _memo.get(sub.manifest) is entry
                state, kept = entry.state, entry.found
            if not current:
                continue  # that check kept nothing: check again
            if state == _REVOKED:
                settle(ctx, sub, Outcome(verified=kept), warnings, store=True)
            elif state == _NOT_CHECKED and ctx.require_check:
                # A strict call never takes a kept "not checked": it checks for
                # itself, and keeps a better answer when it gets one.
                out = _check_with_deadline(ctx, sub)
                if out.unread is None:
                    _keep(sub.manifest, out)
                settle(ctx, sub, out, warnings, store=True)
            return  # not revoked, or not checked and already warned

        out: Outcome | None = None
        failure: BaseException | None = None
        try:
            out = _check_with_deadline(ctx, sub)
            settle(ctx, sub, out, warnings, store=True)
        except BaseException as exc:
            failure = exc
        finally:
            with _memo_lock:
                if failure is not None and (out is None or out.verified is None):
                    if _memo.get(sub.manifest) is entry:
                        del _memo[sub.manifest]  # a 410 or a strict failure: keep nothing
                elif out is not None and out.verified is not None:
                    entry.state, entry.found = _REVOKED, out.verified
                elif out is not None and out.unread is not None:
                    entry.state = _NOT_CHECKED
                else:
                    entry.state = _NOT_REVOKED
            entry.done.set()
        if failure is not None:
            raise failure
        return
