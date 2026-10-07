"""Public issue #530: `cache_root` and `search_dirs` report the resolution the
fetch layer runs, in the order it reads, and create nothing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import chtypes

_CASES = json.loads(
    (Path(__file__).resolve().parents[2] / "tests/fixtures/cache-roots/cases.json").read_text()
)


def _subst(value, tmp: Path):
    if isinstance(value, str):
        return value.replace("<TMP>", str(tmp))
    if isinstance(value, list):
        return [_subst(v, tmp) for v in value]
    return value


@pytest.mark.parametrize("case", _CASES["cases"], ids=lambda c: c["name"])
def test_the_shared_table(case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one table every binding's test reads, so all four give one list."""
    for key in ("CHTYPES_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(key, raising=False)
    for key, val in case["env"].items():
        monkeypatch.setenv(key, _subst(val, tmp_path))
    system = _subst(case["system_dirs"], tmp_path)
    options = chtypes.FetchOptions(
        cache_dir=_subst(case["cache_dir"], tmp_path),
        system_dirs=None if system is None else tuple(system),
    )
    want = [Path(p) for p in _subst(case["search_dirs"], tmp_path)]
    assert chtypes.search_dirs(options) == tuple(want)
    assert chtypes.cache_root(options) == want[0]


def test_no_options_is_the_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("CHTYPES_CACHE", str(tmp_path / "env"))
    assert chtypes.cache_root() == tmp_path / "env"
    assert chtypes.search_dirs()[0] == tmp_path / "env"
    assert chtypes.search_dirs() == chtypes.search_dirs(chtypes.FetchOptions())


def test_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHTYPES_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    chtypes.cache_root()
    chtypes.search_dirs()
    chtypes.search_dirs(
        chtypes.FetchOptions(cache_dir=tmp_path / "c", system_dirs=[tmp_path / "s"])
    )
    assert list(tmp_path.iterdir()) == []
