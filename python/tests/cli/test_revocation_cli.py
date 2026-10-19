"""`chtypes verify` and `chtypes fetch` over a build whose entry holds a stored
revocation (docs/guides/fetch-v1.md, "Build-level revocation"): the production
generation-2 contract, over the fetch-v2 corpus's stored-record layout and its
test key. `verify` exits 11 naming the advisory, an offline fetch is refused with
11, and `--allow-revoked` lets that one fetch use it, with the warning."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from chtypes import __main__ as cli
from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._ensure import reset_revocation_memo_for_tests

FETCH_V2 = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "fetch-v2"


@pytest.fixture(autouse=True)
def _prod_v2_contract() -> Iterator[None]:
    restore = _channel.use_prod_v2_for_tests()
    reset_revocation_memo_for_tests()
    try:
        yield
    finally:
        reset_revocation_memo_for_tests()
        restore()


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_a_stored_revocation_exits_eleven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cache = tmp_path / "cache"
    shutil.copytree(FETCH_V2 / "layouts" / "revocation-stored", cache / C.PROD_V2_CACHE_DIR)
    monkeypatch.setenv(
        "CHTYPES_TRUSTED_KEYS", (FETCH_V2 / "test-key" / "public.hex").read_text().strip()
    )
    monkeypatch.delenv("CHTYPES_ARTIFACTS_URL", raising=False)
    monkeypatch.setattr(cli, "detect_host_platform", lambda: "linux-arm64")

    code, _, err = run(capsys, "verify", "--cache", str(cache))
    assert code == 11, err
    assert "REVOKED 26.13.2.1 linux-arm64 " in err and "GHSA-fx01-test-0102" in err

    argv = ("fetch", "26.13.2.1", "--offline", "--platform", "linux-arm64", "--cache", str(cache))
    code, out, err = run(capsys, *argv)
    assert code == 11 and out == "", (code, out, err)
    assert "is revoked: GHSA-fx01-test-0102" in err and "CHTYPES_ARTIFACT_REVOKED" in err

    code, out, err = run(capsys, *argv, "--allow-revoked")
    assert code == 0 and out.strip(), (code, out, err)
    assert "allowing a revoked build: 26.13.2.1 linux-arm64" in err
