"""A live smoke of the production generation-2 channel against a real registry,
opt-in: `CHTYPES_PRODV2_SMOKE_BASE` names the repository (the staging dev
repository, whose builds are abi 2 and signed with the staging key) and
`CHTYPES_PRODV2_SMOKE_KEY` the key it is signed with. It skips loudly when either
is unset, and the sandboxed CI runs never set them. It proves the channel honors
a base and a trust override, writes a schema-2 record signed by that key, and that
a lock then a frozen fetch pin one manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from chtypes._ocifetch import _channel
from chtypes._ocifetch._dsse import TrustedKey
from chtypes._ocifetch._ensure import Options, Request, ensure


def test_prod_v2_live_smoke(tmp_path: Path) -> None:
    base = os.environ.get("CHTYPES_PRODV2_SMOKE_BASE")
    key = os.environ.get("CHTYPES_PRODV2_SMOKE_KEY")
    if not base or not key:
        pytest.skip("CHTYPES_PRODV2_SMOKE_BASE and CHTYPES_PRODV2_SMOKE_KEY are not set")
    spelling = os.environ.get("CHTYPES_PRODV2_SMOKE_SPELLING", "26.9")
    restore = _channel.use_prod_v2_for_tests()
    try:
        lock = tmp_path / "chtypes.lock"
        raw_key = bytes.fromhex(key)
        keyid = hashlib.sha256(raw_key).hexdigest()[:16]  # the constants' sha256-first16hex
        trust = (TrustedKey(keyid=keyid, public_key=raw_key),)

        def options(cache: str, **kw: bool) -> Options:
            return Options(
                bases=(base,),
                trusted_keys=trust,
                cache_dir=tmp_path / cache,
                system_dirs=(),
                lock_path=lock,
                **kw,
            )

        first = ensure(Request(spelling), options("cache1", lock_write=True))
        record = json.loads((Path(first.dir) / "verified.json").read_text())
        manifest = first.digests["manifest"]
        print(f"fetched {spelling}: {first.version} build {first.build} manifest {manifest}")
        print(
            f"record: schema {record['schema']}, signed_by {record['signed_by']}, "
            f"predicate abi {record['predicate']['abi']}, under {Path(first.dir).parents[2].name}"
        )
        assert record["schema"] == 2 and record["predicate"]["abi"] == 2
        assert record["signed_by"]
        assert Path(first.dir).parents[2].name == "v2"

        again = ensure(Request(spelling), options("cache2", frozen=True))
        print(f"frozen: manifest {again.digests['manifest']}")
        assert again.digests["manifest"] == first.digests["manifest"]
    finally:
        restore()
