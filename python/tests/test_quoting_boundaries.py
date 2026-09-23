"""The quoting trio, measured against real artifacts (issue #119, from #52).

Four things are pinned here, and none of them is a spelling:

1. **The per-line boundaries.** `quote_identifier_if_needed` answers the LOADED
   BUILD's own rule, and that rule moves between ClickHouse lines. The 0.3.0
   CHANGELOG states which names move where; these cases execute that statement
   against whatever lines the registry holds, on BOTH sides of every boundary.
2. **An empty identifier comes back quoted, never bare** (`include/chtypes.h`).
3. **A literal carrying a NUL byte survives end to end** — the one case where
   the counted-input contract is load-bearing, because a `strlen` would
   truncate at the NUL and nothing else would say so.
4. **Two loaded libraries answer for themselves**, asked in one process and
   interleaved. Per-line divergence is the entire reason these calls hang off a
   library rather than off the package, and one library cannot show it.

HOW A SPELLING IS NEVER WRITTEN DOWN HERE. Every expectation is stated as one
of the library's OWN two answers: `quote_identifier` always quotes, so it IS
this build's quoted spelling, and the input itself is the bare spelling. A case
asserts `answer == library.quote_identifier(name)` or `answer == name` and
never names a quote character — which is what keeps
`scripts/check-quoting-passthrough.py` true and keeps these cases from
re-asserting the hand-written rule issue #52 deleted. Each line is first
checked to spell the two forms DIFFERENTLY, so "quoted" and "bare" cannot both
be satisfied by the same bytes.

THE TWO ENUMERATIONS BELOW ARE EXPECTATIONS, NOT A RULE. Nothing here computes
which names a build quotes; the sets say what the release MEASURED, per line,
and a disagreement is a finding about the artifact rather than a test to
adjust. They are closed downward on purpose: the lines named are the published
lines that sit below a boundary, and any other line is at or above the last
one, so a line published after this was written reads as "quotes all three"
rather than silently dropping out of the count.

COUNTS, AND WHY A QUIET RUN IS A FAILURE. Every case counts what it actually
ran and asserts the total. Without a registry the whole module skips, loudly,
by name, like every other artifact-backed suite here; WITH one, a case that
observed only one side of a boundary — or only one library — fails by name
rather than passing on the half it could see. A boundary case that skips
forever is exactly the failure this file exists to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

import chtypes

# The 0.3.0 CHANGELOG's claim, split into the part that holds everywhere and
# the part that moves between lines.
QUOTED_ON_EVERY_LINE = ("all", "distinct", "table", "null")
BARE_ON_EVERY_LINE = ("where",)
MOVES_BETWEEN_LINES = ("select", "from", "values")
EVERY_NAME = QUOTED_ON_EVERY_LINE + BARE_ON_EVERY_LINE + MOVES_BETWEEN_LINES

# Published lines below every boundary: all three of the moving names bare.
LINES_BELOW_EVERY_BOUNDARY = frozenset({"24.8", "25.3", "25.8", "25.10", "26.2"})
# Published lines that quote `select` and not yet `from`/`values`.
LINES_QUOTING_SELECT_ONLY = frozenset({"26.3", "26.4"})


def expected_bare(line: str, name: str) -> bool:
    """Whether this line is expected to spell `name` bare, per the CHANGELOG."""
    if name in QUOTED_ON_EVERY_LINE:
        return False
    if name in BARE_ON_EVERY_LINE:
        return True
    # Anything left is one of the names the release says MOVES between lines,
    # and nothing else may reach the per-line answer below.
    assert name in MOVES_BETWEEN_LINES, f"{name!r} has no expectation in this file"
    if line in LINES_BELOW_EVERY_BOUNDARY:
        return True
    if line in LINES_QUOTING_SELECT_ONLY:
        return name != "select"
    return False


@pytest.fixture(scope="session")
def loaded(registry: chtypes.Registry) -> Sequence[tuple[str, chtypes.Library]]:
    """Every line this registry can answer for, OPENED, in release order.

    A line that refuses to open — an artifact from an older ABI revision left
    on the search path, say — is reported and left out rather than failing
    every case here: whether enough lines opened is decided below, by name,
    where the reason can be stated.
    """
    out: list[tuple[str, chtypes.Library]] = []
    for line in registry.versions():
        try:
            out.append((line, registry.for_version(line)))
        except chtypes.ChtypesError as exc:  # pragma: no cover - environment-dependent
            print(f"[chtypes] line {line} did not open and is not measured here: {exc}")
    return out


def _both_forms(library: chtypes.Library, name: str) -> tuple[str, str]:
    """(the always-quoted spelling, the if-needed answer) for one name.

    The first is this build's own quoted form — that is what `quote_identifier`
    IS — so a case never has to know what quoting looks like.
    """
    return library.quote_identifier(name), library.quote_identifier_if_needed(name)


def test_the_per_line_boundaries_are_what_the_release_documents(
    loaded: Sequence[tuple[str, chtypes.Library]],
) -> None:
    ran = 0
    seen_below = [line for line, _ in loaded if line in LINES_BELOW_EVERY_BOUNDARY]
    seen_above = [
        line
        for line, _ in loaded
        if line not in LINES_BELOW_EVERY_BOUNDARY and line not in LINES_QUOTING_SELECT_ONLY
    ]
    for line, library in loaded:
        for name in EVERY_NAME:
            always, answer = _both_forms(library, name)
            assert always != name, (
                f"{line}: the always-quoted form of {name!r} is the bare name, so this case "
                f"cannot tell quoted from bare"
            )
            if expected_bare(line, name):
                assert answer == name, f"{line}: {name!r} -> {answer!r}, documented bare"
            else:
                assert answer == always, (
                    f"{line}: {name!r} -> {answer!r}, documented as this build's quoted form "
                    f"{always!r}"
                )
            ran += 1
    assert ran == len(EVERY_NAME) * len(loaded), "a case was skipped inside the loop"
    assert ran > 0, "no line was examined"
    # Both sides, or nothing is being measured. CI fetches 24.8 alongside the
    # newest -lts and -stable precisely so this holds.
    if not seen_below:
        pytest.fail(
            "the registry holds no line below every documented boundary "
            f"(one of {sorted(LINES_BELOW_EVERY_BOUNDARY)}), so the boundary cannot be "
            "observed at all — `scripts/fetch.sh 24.8` installs one. Lines loaded: "
            f"{[line for line, _ in loaded]}"
        )
    if not seen_above:
        pytest.fail(
            "the registry holds no line at or above the last documented boundary, so the "
            "quoted side of it cannot be observed — `scripts/fetch.sh 26.8` installs one. "
            f"Lines loaded: {[line for line, _ in loaded]}"
        )


def test_an_empty_identifier_comes_back_quoted_never_bare(
    loaded: Sequence[tuple[str, chtypes.Library]],
) -> None:
    ran = 0
    for line, library in loaded:
        always, answer = _both_forms(library, "")
        assert answer, f"{line}: an empty identifier came back empty, which is bare"
        assert answer == always, (
            f"{line}: an empty identifier -> {answer!r}, want this build's quoted form {always!r}"
        )
        ran += 1
    assert ran == len(loaded) and ran > 0, "no line answered for an empty identifier"


def test_a_literal_carrying_a_nul_byte_survives_end_to_end(
    loaded: Sequence[tuple[str, chtypes.Library]],
) -> None:
    ran = 0
    for line, library in loaded:
        plain = library.quote_literal("a")
        with_nul = library.quote_literal("a\0b")
        # A strlen'd input would quote just the leading "a" and say nothing.
        assert with_nul != plain, f"{line}: the input was truncated at the NUL byte"
        assert len(with_nul) > len(plain), f"{line}: {with_nul!r} is no longer than {plain!r}"
        assert "b" in with_nul, f"{line}: the byte after the NUL is missing from {with_nul!r}"
        # The header's other half: every byte the server escapes comes back
        # escaped, so the ANSWER is NUL-free even when the input was not.
        assert "\0" not in with_nul, f"{line}: the answer {with_nul!r} carries a NUL byte"
        # Same delimiters as an ordinary value: this is one literal, not two.
        assert with_nul[0] == plain[0] and with_nul[-1] == plain[-1], (
            f"{line}: {with_nul!r} is not delimited like {plain!r}"
        )
        ran += 1
    assert ran == len(loaded) and ran > 0, "no line quoted a literal carrying a NUL byte"


def test_two_loaded_libraries_each_answer_for_themselves(
    loaded: Sequence[tuple[str, chtypes.Library]],
) -> None:
    if len(loaded) < 2:
        pytest.fail(
            "this case needs TWO libraries in one process — a single library cannot show a "
            "cache-across-versions bug, which is the whole reason these calls hang off a "
            f"library. Lines loaded: {[line for line, _ in loaded]}"
        )
    (old_line, old_lib), (new_line, new_lib) = loaded[0], loaded[-1]

    ran = 0
    first: dict[str, tuple[str, str]] = {}
    second: dict[str, tuple[str, str]] = {}
    # Interleaved, and then interleaved again: each library is asked the same
    # name after the other one has answered it, so an answer cached across
    # versions would show up as one library repeating the other's.
    for round_ in (first, second):
        for name in EVERY_NAME:
            round_[name] = (
                old_lib.quote_identifier_if_needed(name),
                new_lib.quote_identifier_if_needed(name),
            )
            ran += 2
    assert first == second, (
        f"{old_line}/{new_line}: asking one library changed the other's answer: "
        f"{first} then {second}"
    )
    assert ran == 2 * 2 * len(EVERY_NAME), "a name was skipped inside the loop"

    # And where the two lines sit on opposite sides of a boundary, they must
    # DISAGREE — the divergence the library-scoped API exists for.
    divergent = [
        name
        for name in EVERY_NAME
        if expected_bare(old_line, name) != expected_bare(new_line, name)
    ]
    if not divergent:
        pytest.fail(
            f"{old_line} and {new_line} sit on the same side of every documented boundary, so "
            "no divergence can be observed — fetch a line below 26.3 (24.8) alongside the "
            "newest one"
        )
    for name in divergent:
        old_answer, new_answer = first[name]
        assert old_answer != new_answer, (
            f"{name!r} is documented as differing between {old_line} and {new_line}, and both "
            f"answered {old_answer!r}"
        )
