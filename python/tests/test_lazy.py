"""Loading is lazy, and ``preload`` is the one eager path.

The sentence this file exists to hold is the same sentence in all four
bindings: constructing a registry reads ``manifest.json`` files and ``dlopen``s
nothing, and nothing in the library opens an artifact except a request for a
specific version or an explicit ``preload``.

The proof needs no real artifact and inspects no code. Every "library" here is
a text file, so any open at all fails at ``dlopen`` — a registry that opened
one would raise from whichever call opened it. What is counted is
``libraries()``, which is populated by the real load path and by nothing else.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import chtypes

BODY = b"not a shared library"


def stand_in(root: Path, line: str, **overrides: object) -> Path:
    """``<root>/<line>/`` with a manifest and a "library" that is plain text."""
    sub = root / line
    sub.mkdir(parents=True)
    (sub / "libchtypes.so").write_bytes(BODY)
    fields: dict[str, object] = {
        "library": "libchtypes.so",
        "library_bytes": len(BODY),
        "clickhouse_version": f"{line}.1.1",
        "clickhouse_minor": line,
    }
    fields.update(overrides)
    (sub / "manifest.json").write_text(json.dumps(fields))
    return sub


@pytest.fixture
def stand_in_registry(tmp_path: Path, isolated_search_path: Path) -> Path:
    """A directory of three stand-in artifacts, with this machine's own
    registry taken out of the search path so `versions` answers about it."""
    root = tmp_path / "registry"
    for line in ("25.8", "25.10", "26.7"):
        stand_in(root, line)
    return root


def test_construction_and_listing_open_nothing(stand_in_registry: Path) -> None:
    registry = chtypes.Registry(stand_in_registry)
    assert registry.libraries() == ()

    # `versions` answers from the manifest scan, not from what is open, and it
    # is the one meaning all four bindings now share: loaded OR discoverable.
    assert registry.versions() == ("25.8", "25.10", "26.7")

    # None of the listing surfaces opens anything — not `versions`, not
    # `libraries`, not `in`, not `len`, not `repr`. Opening 120 MB of artifact
    # as the side effect of formatting a registry is not something a caller
    # can undo.
    assert "25.8" in registry
    assert len(registry) == 3
    assert "25.8" in repr(registry)
    assert list(registry) == []
    assert registry.libraries() == ()

    # The first request for a line is what opens it — and here that open
    # reaches dlopen and dies there, which is the proof it reached dlopen at
    # all rather than being skipped.
    with pytest.raises(chtypes.ChtypesError):
        registry.for_version("25.8")


def test_preload_opens_exactly_the_named_lines(stand_in_registry: Path) -> None:
    # A preloaded line is opened before the constructor returns, so a text
    # file's dlopen failure arrives from `Registry(...)` rather than from
    # `for_version`.
    with pytest.raises(chtypes.ChtypesError) as caught:
        chtypes.Registry(stand_in_registry, preload=["25.10"])
    assert "25.10" in str(caught.value)

    # A different entry proves it is the LIST that decides which line opens,
    # not the directory listing.
    with pytest.raises(chtypes.ChtypesError) as other:
        chtypes.Registry(stand_in_registry, preload=["26.7"])
    assert "26.7" in str(other.value)

    # An empty list is exactly the default, not a special case.
    assert chtypes.Registry(stand_in_registry, preload=[]).libraries() == ()


def test_preload_of_an_unknown_line_fails_at_construction(stand_in_registry: Path) -> None:
    with pytest.raises(chtypes.ArtifactMissingError) as caught:
        chtypes.Registry(stand_in_registry, preload=["24.8"])
    assert caught.value.line == "24.8"
    assert str(stand_in_registry) in ", ".join(caught.value.looked_in)


def test_preload_never_fetches(stand_in_registry: Path) -> None:
    """Autofetch is a first-use behavior in all four bindings.

    A constructor is a worse place than a request to begin a 250 MB download,
    and TypeScript's constructor is synchronous besides — so a ``preload``
    entry no directory holds is the §7 error even with autofetch on, rather
    than a fetch. There is no source configured here: a fetch would fail with
    a fetch verdict instead of this one.
    """
    with pytest.raises(chtypes.ArtifactMissingError):
        chtypes.Registry(stand_in_registry, autofetch=True, preload=["24.8"])


def test_verification_covers_a_lazily_opened_line_too(
    tmp_path: Path, isolated_search_path: Path
) -> None:
    """`verify_hashes` is a policy on the REGISTRY, not on the preload list.

    A library's checksum is computed immediately before that library is
    `dlopen`ed and at no other time — at construction for a preloaded line, at
    first use for one that was not. Reading it as "only the preload list is
    verified" would let a lazily-opened line load UNHASHED under
    ``verify_hashes=True``, which is a silent regression in a security posture.
    """
    root = tmp_path / "registry"
    stand_in(root, "25.8", library_sha256="00" * 32)
    stand_in(root, "26.7", library_sha256="00" * 32)

    # Preloaded: the refusal arrives from the constructor.
    with pytest.raises(chtypes.RegistryError, match="sha256"):
        chtypes.Registry(root, verify_hashes=True, preload=["25.8"])

    # Not preloaded: the registry constructs, and the refusal arrives from the
    # call that asks for the line — hashed all the same.
    registry = chtypes.Registry(root, verify_hashes=True)
    with pytest.raises(chtypes.RegistryError, match="sha256"):
        registry.for_version("26.7")
    assert registry.libraries() == ()

    # And the hash is consulted only for a line somebody asked for: the same
    # registry never touched 25.8, which is just as broken.
    honest = chtypes.Registry(root, verify_hashes=False)
    with pytest.raises(chtypes.ChtypesError) as caught:
        honest.for_version("25.8")
    assert "sha256" not in str(caught.value), (
        "without verify_hashes the digest must not be consulted; the open must reach dlopen"
    )


def test_library_bytes_is_checked_on_every_load_without_verify_hashes(
    tmp_path: Path, isolated_search_path: Path
) -> None:
    """The size check runs unconditionally, unlike the hash above (issue #82).

    Unlike `verify_hashes`'s sha256 comparison, the manifest's
    ``library_bytes`` size check is NOT conditional on it: it is nearly free
    (one stat, never a re-hash of the library's contents), and it catches the
    commonest shape of a broken artifact directory — a truncated or
    partially-written library file. ``verify_hashes`` is False everywhere
    below, on purpose.
    """
    root = tmp_path / "registry"
    stand_in(root, "25.8", library_bytes=len(BODY) + 1)

    # Preloaded: the refusal arrives from the constructor, BEFORE dlopen.
    with pytest.raises(chtypes.RegistryError, match="is .* bytes, manifest says"):
        chtypes.Registry(root, preload=["25.8"])

    # Lazily opened: the same refusal arrives from the call that asks.
    registry = chtypes.Registry(root)
    with pytest.raises(chtypes.RegistryError, match="is .* bytes, manifest says"):
        registry.for_version("25.8")
    assert registry.libraries() == ()


def test_an_absent_library_bytes_is_not_asked(tmp_path: Path, isolated_search_path: Path) -> None:
    """A manifest predating the field (or none at all) must never become a
    NEW reason a load fails, with or without verification."""
    root = tmp_path / "registry"
    sub = root / "25.8"
    sub.mkdir(parents=True)
    (sub / "libchtypes.so").write_bytes(BODY)
    (sub / "manifest.json").write_text(
        json.dumps(
            {
                "library": "libchtypes.so",
                "clickhouse_version": "25.8.1.1",
                "clickhouse_minor": "25.8",
            }
        )
    )

    # Reaches (and fails at) dlopen — the size check never had a value to
    # compare against.
    with pytest.raises(chtypes.ChtypesError) as caught:
        chtypes.Registry(root, preload=["25.8"])
    assert "bytes, manifest says" not in str(caught.value)


def test_a_named_directory_that_cannot_be_read_fails_at_construction(
    tmp_path: Path, isolated_search_path: Path
) -> None:
    """The typo guard survives lazy loading, and costs no dlopen."""
    missing = tmp_path / "chtyeps"
    with pytest.raises(chtypes.RegistryError) as caught:
        chtypes.Registry(missing)
    assert str(missing) in str(caught.value)
    # With autofetch the directory is the destination the first fetch creates.
    assert chtypes.Registry(missing, autofetch=True).versions() == ()


def test_an_empty_search_path_fails_at_construction(
    tmp_path: Path, isolated_search_path: Path
) -> None:
    """Python's one new construction error, accepted with this design.

    "No directory anywhere holds a readable ``<minor>/manifest.json``" is
    decidable from manifests alone, so it is decided at construction. Python
    was the one binding where `Registry()` could previously succeed against a
    completely empty machine and report the miss three calls later.
    """
    empty = tmp_path / "registry"
    empty.mkdir()
    with pytest.raises(chtypes.RegistryError) as caught:
        chtypes.Registry(empty)
    assert str(empty) in str(caught.value)
    assert str(isolated_search_path) in str(caught.value)
    # Installed after construction still works, which is what `_scan` on a miss
    # is for — so the check is about an empty MACHINE, not an empty moment.
    stand_in(empty, "25.8")
    assert chtypes.Registry(empty).versions() == ("25.8",)


def test_the_checksum_option_is_not_consulted_for_a_line_nobody_asks_for(
    tmp_path: Path, isolated_search_path: Path
) -> None:
    """…and at no other time. A registry over a directory holding one corrupt
    line and one good one constructs, and serves the good one."""
    root = tmp_path / "registry"
    stand_in(root, "25.8", library_sha256="00" * 32)  # corrupt
    stand_in(root, "26.7", library_sha256=hashlib.sha256(BODY).hexdigest())  # honest

    registry = chtypes.Registry(root, verify_hashes=True)
    assert registry.versions() == ("25.8", "26.7")
    # The honest line passes its hash and dies at dlopen, which is the proof
    # the hash ran and passed rather than never running.
    with pytest.raises(chtypes.ChtypesError) as caught:
        registry.for_version("26.7")
    assert "sha256" not in str(caught.value)
