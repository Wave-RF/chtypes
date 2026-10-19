"""What a `CHTYPES_ARTIFACT_REVOKED` error carries for programs
(docs/guides/fetch-v1.md, "Build-level revocation"; public issue #625).

A leaf module, so the fetch layer's errors and the public ones share the two
types without importing each other."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Advisory", "Revocation"]


@dataclass(frozen=True)
class Advisory:
    """One advisory a revocation names, every string made safe to print. A
    signed revocation's carries its `revoked_at`; a 410's carries the `build` it
    names, newest first. A field the source did not send is "". `class_` is the
    advisory's `class` (a keyword in Python)."""

    id: str = ""
    url: str = ""
    class_: str = ""
    summary: str = ""
    build: str = ""
    superseded_by: str = ""
    revoked_at: str = ""


@dataclass(frozen=True)
class Revocation:
    """`signed` is true for a verified, signed revocation of `manifest`, whose
    one advisory it is, and false for a 410's body, which is unsigned, names no
    manifest (`manifest` is "") and lists one advisory per revoked build."""

    signed: bool = False
    manifest: str = ""
    advisories: tuple[Advisory, ...] = ()
