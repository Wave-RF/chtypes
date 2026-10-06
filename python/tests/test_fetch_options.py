"""`FetchOptions`: the public configuration surface of the fetch layer."""

from __future__ import annotations

from pathlib import Path

import chtypes
from chtypes._ocifetch import _channel
from chtypes._ocifetch._layout import search_roots

# These run under the dev channel, as a user's process does: an explicit cache
# is read through its v2-dev subroot (spec/abi-v2/docs.md, rule r5).


def test_to_options_is_not_public() -> None:
    assert not hasattr(chtypes.FetchOptions, "to_options")
    assert hasattr(chtypes.FetchOptions, "_to_options")


def test_system_dirs_reach_the_search_path(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    seen = [tmp_path / "a", tmp_path / "b"]
    options = chtypes.FetchOptions(cache_dir=cache, system_dirs=seen)._to_options()
    assert options.system_dirs == tuple(seen)
    assert search_roots(options.cache_dir, options.system_dirs) == (cache / "v2-dev", *seen)


def test_none_keeps_the_default_list_and_empty_searches_none(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    default = chtypes.FetchOptions(cache_dir=cache)._to_options()
    assert default.system_dirs is None
    dev_dirs = _channel.DEV_CHANNEL.system_dirs
    assert all(d.endswith("/v2-dev") for d in dev_dirs)  # never a 1.x system dir
    assert search_roots(cache, default.system_dirs) == (cache / "v2-dev", *map(Path, dev_dirs))
    none = chtypes.FetchOptions(cache_dir=cache, system_dirs=())._to_options()
    assert search_roots(cache, none.system_dirs) == (cache / "v2-dev",)
