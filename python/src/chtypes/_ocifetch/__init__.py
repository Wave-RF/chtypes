"""The private v1 OCI + zstd fetch layer (docs/guides/fetch-v1.md).

Not part of this package's public API: every name here is subject to change
without notice until the v1 switch. `_constants` is generated; the fetch
logic lives in sibling modules. The seam (docs/guides/fetch-v1.md "The seam")
is the only surface anything outside this package may use: ``ensure``,
``resolve_installed``, ``list_installed``, ``verify_installed`` and
``fetch_signed``, plus the ``Request``/``Options``/``Resolved``/
``VerifyResult`` shapes they take and return. Every other module here
(``_http``, ``_oci``, ``_referrers``, ``_dsse``, ``_unpack``, ``_layout``,
``_lock``, ``_errors``) is a private implementation detail of ``_ensure``.
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

__all__ = [
    "Options",
    "Request",
    "Resolved",
    "VerifyResult",
    "ensure",
    "fetch_signed",
    "list_installed",
    "resolve_installed",
    "verify_installed",
]
