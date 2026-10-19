"""Referrer discovery: the referrers API, with the `sha256-<hex>` tag-schema
fallback (layout-v2 spec §7.2, §10 A4).

Both the API and the fallback are implemented, since our own host serves
the referrers API (with `artifactType` filtering) while a mirror may offer
only the fallback tag. Filtering by `artifactType` is always done here too,
never trusted to the server alone (PLAN §3.2 "Client rules this forces").

**Relayed from the delivery side (2026-10-01):** a referrers-API answer that,
after our own `artifactType` filter, has NO matching entries is not
authoritative proof that nothing is attached — a just-pushed manifest can
read back with an empty (or not-yet-matching) list for a short window before
the host's own indexing catches up. (The host serves an empty referrers list
`no-store`, so this is not a caching artifact to wait out; it is a brief
propagation gap on the write side.) `discover_referrers` therefore always
also tries the fallback tag when the API's own answer is empty, not only
when the API call itself fails. A manifest can carry several referrers of
the same `artifactType` at once (a key rotation adds a second bundle); the
caller tries each until one verifies (`_ensure.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import loads_no_duplicate_keys
from chtypes._ocifetch._errors import ArtifactCorruptError
from chtypes._ocifetch._http import (
    FetchPolicy,
    GoneHttpError,
    NotFoundHttpError,
    RetryPolicy,
    TransportError,
    UnreachableHttpError,
    fetch_from_bases,
    get_json,
)
from chtypes._ocifetch._oci import Descriptor

__all__ = ["ReferrerListing", "discover_referrers", "read_listing"]


def _parse_descriptor_list(doc: dict, *, what: str) -> list[Descriptor]:
    manifests = doc.get("manifests")
    if not isinstance(manifests, list):
        raise ArtifactCorruptError(f"{what}: no manifests array")
    if len(manifests) > C.MAX_REFERRERS:
        raise ArtifactCorruptError(
            f"{what}: {len(manifests)} entries exceeds the {C.MAX_REFERRERS} this fetcher accepts"
        )
    out = []
    for m in manifests:
        out.append(
            Descriptor(
                media_type=m.get("mediaType", ""),
                digest=m.get("digest", ""),
                size=m.get("size", -1),
                artifact_type=m.get("artifactType"),
                annotations=m.get("annotations"),
            )
        )
    return out


def _referrers_api(
    bases, subject_digest: str, artifact_type: str, *, policy: FetchPolicy, retry: RetryPolicy
) -> list[Descriptor]:
    try:
        doc, _resp = get_json(
            bases,
            f"/referrers/{subject_digest}",
            # Not "digest" mode: a 404 here means "no referrers API answer",
            # which is this function's own normal "fall back to the tag"
            # case, not a host fault to retry through as if a content
            # digest had vanished. `mode="tag"` gives the right shape: an
            # immediate NotFoundHttpError on 404, with a genuine transient
            # failure (5xx, connection error) still propagating as
            # UnreachableHttpError rather than being swallowed.
            mode="tag",
            accept=(C.MEDIA_TYPE_INDEX,),
            max_bytes=C.MANIFEST_MAX_BYTES,
            policy=policy,
            retry=retry,
            # Never sent to a `file://` base (PLAN §3.2); server-side
            # filtering here is a convenience on top of our own client-side
            # filter below, never something a fixture tree must honor.
            # Percent-encoded: a raw `+` (in `...bundle.v0.3+json`) is a
            # space to the real host, which then filters everything out.
            query="artifactType=" + quote(artifact_type, safe=""),
        )
    except NotFoundHttpError:
        return []
    return _parse_descriptor_list(doc, what="referrers API response")


def _fallback_tag(
    bases, subject_digest: str, *, policy: FetchPolicy, retry: RetryPolicy
) -> list[Descriptor]:
    # layout-v2 spec §7.2 / §10 A4: the tag-schema fallback, `sha256-<hex>`,
    # for a mirror (or edge cache) that does not serve the referrers API.
    hex_digest = subject_digest.split(":", 1)[1] if ":" in subject_digest else subject_digest
    tag = f"sha256-{hex_digest}"
    try:
        doc, _resp = get_json(
            bases,
            f"/manifests/{tag}",
            mode="tag",
            # A `GET …/manifests/<ref>` is always sent with both media types
            # in Accept, by tag and by digest — our host ignores it, but a
            # mirror may not (relayed from the delivery side, 2026-10-01).
            accept=(C.MEDIA_TYPE_INDEX, C.MEDIA_TYPE_MANIFEST),
            max_bytes=C.MANIFEST_MAX_BYTES,
            policy=policy,
            retry=retry,
        )
    except NotFoundHttpError:
        return []
    return _parse_descriptor_list(doc, what="fallback tag response")


def discover_referrers(
    bases,
    subject_digest: str,
    artifact_type: str,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
) -> list[Descriptor]:
    """Every referrer of `subject_digest` whose `artifactType` equals
    `artifact_type`, from the referrers API, the fallback tag, or both.

    Always filters by `artifactType` itself (never trusts the server's own
    filter alone), and always tries the fallback when the API's own answer
    — after that filter — is empty, per the module docstring above: an
    empty or not-yet-matching referrers response is not distinguishable from
    "nothing was ever attached" during a brief post-push propagation window,
    so refusing on the API's word alone would manufacture a false UNTRUSTED.
    """
    api_descriptors = _referrers_api(
        bases, subject_digest, artifact_type, policy=policy, retry=retry
    )
    matching = [d for d in api_descriptors if d.artifact_type == artifact_type]
    if matching:
        return matching
    fallback_descriptors = _fallback_tag(bases, subject_digest, policy=policy, retry=retry)
    return [d for d in fallback_descriptors if d.artifact_type == artifact_type]


@dataclass
class ReferrerListing:
    """One platform manifest's referrers listing: the first answer that names a
    signature referrer (the referrers API on the bases in order, then the fallback
    tag), its signature and revocation referrers, and, when no answer named a
    signature, the last failure that kept a base from answering (None when every
    base answered 404 or a listing without one)."""

    found: bool = False
    signatures: list[Descriptor] = field(default_factory=list)
    revocations: list[Descriptor] = field(default_factory=list)
    failure: TransportError | None = None


def _parse_listing(body: bytes) -> list[Descriptor]:
    doc = loads_no_duplicate_keys(body)
    manifests = doc.get("manifests") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or (manifests is not None and not isinstance(manifests, list)):
        raise ArtifactCorruptError("not a referrers listing")
    out = []
    for m in manifests or []:
        if not isinstance(m, dict):
            raise ArtifactCorruptError("a listing entry is not an object")
        artifact_type = m.get("artifactType")
        out.append(
            Descriptor(
                media_type=m.get("mediaType", ""),
                digest=m.get("digest", ""),
                size=m.get("size", -1),
                artifact_type=artifact_type if isinstance(artifact_type, str) else None,
                annotations=m.get("annotations"),
            )
        )
    return out


def _listing_index(
    bases,
    path: str,
    accept,
    listing: ReferrerListing,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
) -> list[Descriptor] | None:
    """The entries of the first base that serves `path` with a 200 and a listing,
    recording on `listing` why a base before it did not answer. A 410 is raised."""
    for base in bases:
        try:
            resp = fetch_from_bases(
                [base],
                path,
                mode="tag",
                accept=accept,
                max_bytes=C.MANIFEST_MAX_BYTES,
                policy=policy,
                retry=retry,
            )
        except GoneHttpError:
            raise
        except NotFoundHttpError:
            continue
        except TransportError as exc:
            listing.failure = exc
            continue
        try:
            return _parse_listing(resp.body)
        except ArtifactCorruptError as exc:
            listing.failure = UnreachableHttpError(
                f"{resp.url} is not a referrers listing: {exc}", retryable=False
            )
    return None


def read_listing(
    bases,
    subject_digest: str,
    *,
    policy: FetchPolicy,
    retry: RetryPolicy,
) -> ReferrerListing:
    """`subject_digest`'s listing exactly as the signature lookup reads it
    (docs/guides/fetch-v1.md §4): the referrers API, unfiltered (this filters by
    `artifactType` itself), on the bases in order; then, when no answer of the API
    names a signature referrer, the `sha256-<hex>` fallback tag. The listing that
    names a signature referrer is the one read; its signature and revocation
    referrers are capped at `MAX_REFERRERS` each. A retry policy of one attempt
    makes the installed-build check's budget. Raises only a 410's `GoneHttpError`."""
    hex_digest = subject_digest.split(":", 1)[1] if ":" in subject_digest else subject_digest
    listing = ReferrerListing()
    for path, accept in (
        # The referrers API is not a manifests/<ref> GET, so it sends no Accept.
        (f"/referrers/{subject_digest}", None),
        (f"/manifests/sha256-{hex_digest}", (C.MEDIA_TYPE_INDEX, C.MEDIA_TYPE_MANIFEST)),
    ):
        entries = _listing_index(bases, path, accept, listing, policy=policy, retry=retry)
        if entries is None:
            continue
        signatures = [d for d in entries if d.artifact_type == C.MEDIA_TYPE_BUNDLE]
        if not signatures:
            continue  # not a listing of this manifest's signature: try the fallback tag
        listing.found = True
        listing.signatures = signatures[: C.MAX_REFERRERS]
        listing.revocations = [d for d in entries if d.artifact_type == C.REVOCATION_ARTIFACT_TYPE][
            : C.MAX_REFERRERS
        ]
        return listing
    return listing
