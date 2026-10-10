"""The private v1 OCI + zstd fetch layer (docs/guides/fetch-v1.md).

Not part of this package's public API: every name here is subject to change
without notice until the v1 switch. `_constants` is generated; the fetch
logic lives in sibling modules. The seam (docs/guides/fetch-v1.md "The seam")
is the only surface anything outside this package may use: `ensure`,
`resolve_installed`, `list_installed`, `verify_installed` and
`fetch_signed`, plus the `Request`/`Options`/`Resolved`/
`VerifyResult` shapes they take and return; and beside it the CLI's `resolve`
and `prune` and the registry's in-use `hold` (§1, "In use"). Every other module here
(`_http`, `_oci`, `_referrers`, `_dsse`, `_unpack`, `_layout`,
`_lock`, `_errors`) is a private implementation detail of `_ensure`.

`_channel` says which fetch contract the process speaks: a 2.0.0-dev SDK speaks
the ABI v2 dev channel (spec/abi-v2/docs.md, rules r5 and r6), the v1 contract
narrowed to the staging base and key, with no override, no pinning, schema-2
records and a `v2-dev` cache. The v1 contract stays here, reachable only from a
test run, because the fetch-v1 conformance cases specify it.
"""

from __future__ import annotations

from chtypes._ocifetch._ensure import (
    Options,
    Request,
    Resolved,
    VerifyResult,
    ensure,
    fetch_signed,
    list_installed,
    resolve_installed,
    verify_installed,
)
from chtypes._ocifetch._hold import hold
from chtypes._ocifetch._prune import Superseded, prune
from chtypes._ocifetch._resolve import PlatformOutcome, Resolution, resolve, resolve_each

__all__ = [
    "Options",
    "PlatformOutcome",
    "Request",
    "Resolution",
    "Resolved",
    "Superseded",
    "VerifyResult",
    "ensure",
    "fetch_signed",
    "hold",
    "list_installed",
    "prune",
    "resolve",
    "resolve_each",
    "resolve_installed",
    "verify_installed",
]
