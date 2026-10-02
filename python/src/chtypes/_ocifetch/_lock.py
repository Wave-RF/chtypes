"""Lock schema 3 (docs/guides/fetch-v1.md §6, spec/fetch-v1/schema/lock3.schema.json).

Requests float; the lock is fixed. Per declared platform, it records the
request, the exact version and build, and the platform manifest/layer/bundle
digests. The index digest is informational only — `--frozen` never fetches
it, because the index is computed (layout-v2 spec §5.1), not a stored
object a later by-digest GET is guaranteed to return (PLAN M2).

Schema 1 and 2 (v0) are refused outright, never read as this shape (R4):
a v0 lock is `CHTYPES_ARTIFACT_PINNED`, naming re-lock as the fix, not a
silent "nothing pinned".
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._errors import ArtifactCorruptError, ArtifactPinnedError

__all__ = ["Lock", "LockPin", "load_lock", "new_lock", "save_lock"]


@dataclass(frozen=True)
class LockPin:
    version: str
    build: str
    manifest: str
    layer: str
    bundle: str
    index: str | None = None

    def to_json(self) -> dict:
        doc = {
            "version": self.version,
            "build": self.build,
            "manifest": self.manifest,
            "layer": self.layer,
            "bundle": self.bundle,
        }
        if self.index is not None:
            doc["index"] = self.index
        return doc

    @classmethod
    def from_json(cls, doc: dict) -> LockPin:
        return cls(
            version=doc["version"],
            build=doc["build"],
            manifest=doc["manifest"],
            layer=doc["layer"],
            bundle=doc["bundle"],
            index=doc.get("index"),
        )


@dataclass(frozen=True)
class Lock:
    schema: int
    abi: int
    platforms: tuple[str, ...]
    # spelling -> platform key -> pin
    requests: dict[str, dict[str, LockPin]]

    def to_json(self) -> dict:
        return {
            "schema": self.schema,
            "abi": self.abi,
            "platforms": list(self.platforms),
            "requests": {
                spelling: {plat: pin.to_json() for plat, pin in by_platform.items()}
                for spelling, by_platform in self.requests.items()
            },
        }

    @classmethod
    def from_json(cls, doc: dict) -> Lock:
        schema = doc.get("schema")
        if schema != C.LOCK_SCHEMA:
            raise ArtifactPinnedError(
                f"chtypes: {doc.get('schema', '<missing>')!r} is not a v1 lock file "
                f"(schema {C.LOCK_SCHEMA} expected). Re-lock with "
                f"`chtypes fetch --lock` to write a current one."
            )
        abi = doc.get("abi")
        if abi != C.ABI_GENERATION:
            raise ArtifactPinnedError(
                f"chtypes: lock file is for ABI generation {abi!r}, not {C.ABI_GENERATION}. "
                f"Re-lock with `chtypes fetch --lock`."
            )
        platforms = doc.get("platforms")
        if not isinstance(platforms, list) or not platforms:
            raise ArtifactCorruptError("lock file had no platforms array")
        requests_doc = doc.get("requests")
        if not isinstance(requests_doc, dict):
            raise ArtifactCorruptError("lock file had no requests object")
        requests: dict[str, dict[str, LockPin]] = {}
        for spelling, by_platform in requests_doc.items():
            if not isinstance(by_platform, dict):
                raise ArtifactCorruptError(f"lock file request {spelling!r} was not an object")
            parsed_by_platform = {}
            for plat, pin_doc in by_platform.items():
                if plat not in platforms:
                    raise ArtifactCorruptError(
                        f"lock file pinned platform {plat!r} not in its own platforms list"
                    )
                parsed_by_platform[plat] = LockPin.from_json(pin_doc)
            requests[spelling] = parsed_by_platform
        return cls(schema=schema, abi=abi, platforms=tuple(platforms), requests=requests)

    def with_pin(self, spelling: str, platform: str, pin: LockPin) -> Lock:
        requests = {k: dict(v) for k, v in self.requests.items()}
        requests.setdefault(spelling, {})[platform] = pin
        platforms = self.platforms if platform in self.platforms else (*self.platforms, platform)
        return replace(self, platforms=platforms, requests=requests)

    def pin_for(self, spelling: str, platform: str) -> LockPin | None:
        return self.requests.get(spelling, {}).get(platform)


def new_lock(platforms: tuple[str, ...] = ()) -> Lock:
    return Lock(schema=C.LOCK_SCHEMA, abi=C.ABI_GENERATION, platforms=platforms, requests={})


def load_lock(path: str | os.PathLike[str]) -> Lock:
    with open(path, encoding="utf-8") as f:
        return Lock.from_json(json.load(f))


def save_lock(path: str | os.PathLike[str], lock: Lock) -> None:
    data = json.dumps(lock.to_json(), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".chtypes-lock-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
