"""The cache: an OCI image layout plus `unpacked/sha256/<manifest-hex>/`.

layout-v2 spec §7.6 / PLAN §3.4 "The resolution rule for --offline and
resolve_installed": the source of truth for what is installed is the
immutable `unpacked/sha256/*/verified.json` record, never `index.json` —
losing an `index.json` race costs only `oras` interop, never correctness.
`index.json` is written by atomic rename, then re-read and re-applied, never
in place (PLAN §3.2 "index-race-reapply").
"""

from __future__ import annotations

import json
import os
import re
import shutil
import string
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from chtypes._ocifetch import _constants as C

__all__ = [
    "VerifiedRecord",
    "cache_root",
    "list_verified_records",
    "read_verified_record",
    "resolve_cache_root",
    "search_roots",
    "unpacked_dir_for",
    "write_verified_install",
    "zero_x_hint",
    "zero_x_shape",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _expand_template(template: str) -> str:
    """`${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/v1`: a small, deliberately
    narrow expansion (bash `:-` default plus plain `$VAR`) rather than a
    shell call, so this never depends on a shell being present."""
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    mapping = {"HOME": os.path.expanduser("~")}
    out = template.replace("${XDG_CACHE_HOME:-$HOME/.cache}", xdg)
    return string.Template(out).safe_substitute(mapping)


def resolve_cache_root(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    """`CHTYPES_CACHE` (or an explicit override) names the layout directory
    ITSELF, not a parent `chtypes/` to append to (constants: `cache.root_template`)."""
    if cache_dir is not None:
        return Path(cache_dir)
    env = os.environ.get(C.ENV_CACHE_NAME)
    if env:
        return Path(env)
    return Path(_expand_template(C.CACHE_ROOT_TEMPLATE))


def cache_root(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    return resolve_cache_root(cache_dir)


def search_roots(
    cache_dir: str | os.PathLike[str] | None = None,
    system_dirs: Sequence[str | os.PathLike[str]] | None = None,
) -> tuple[Path, ...]:
    """The cache, then the read-only system directories, in that order
    (layout-v2 spec §0 "cache": "Read-only system directories are searched
    after it"). `system_dirs` replaces the default list when given (an empty
    sequence means none); `None` keeps the generated default."""
    dirs = C.SYSTEM_CACHE_DIRS if system_dirs is None else system_dirs
    return (resolve_cache_root(cache_dir), *(Path(d) for d in dirs))


def unpacked_dir_for(root: Path, manifest_digest_hex: str) -> Path:
    if not _HEX64.match(manifest_digest_hex):
        raise ValueError(f"not a 64-hex manifest digest: {manifest_digest_hex!r}")
    return root / C.CACHE_UNPACKED_DIR / manifest_digest_hex


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
_REQUIRED = (
    "schema",
    "platform",
    "version",
    "channel",
    "build",
    "library",
    "library_sha256",
    "library_bytes",
    "digests",
    "signed_by",
    "predicate",
)
_REQUIRED_DIGESTS = ("index", "manifest", "layer", "bundle", "bundle_manifest")


@dataclass(frozen=True)
class VerifiedRecord:
    """The contents of one `unpacked/sha256/<hex>/verified.json`: the one
    canonical record every binding reads and writes
    (spec/fetch-v1/schema/verified.schema.json, docs/guides/fetch-v1.md §1).
    Optional members are `None` here and `null` on disk."""

    manifest: str
    layer: str
    platform: str
    version: str
    build: str
    channel: str | None
    predicate: dict
    signed_by: str | None
    library: str
    library_sha256: str
    library_bytes: int
    index: str | None = None
    bundle: str | None = None
    bundle_manifest: str | None = None
    schema: int = 1

    def to_json(self) -> dict:
        return {
            "schema": 1,
            "platform": self.platform,
            "version": self.version,
            "channel": self.channel,
            "build": self.build,
            "library": self.library,
            "library_sha256": self.library_sha256,
            "library_bytes": self.library_bytes,
            "digests": {
                "index": self.index,
                "manifest": self.manifest,
                "layer": self.layer,
                "bundle": self.bundle,
                "bundle_manifest": self.bundle_manifest,
            },
            "signed_by": self.signed_by,
            "predicate": self.predicate,
        }

    @classmethod
    def from_json(cls, doc: object) -> VerifiedRecord:
        """Accept exactly the canonical schema-1 record; anything else is a
        `ValueError`, which every caller treats as an ABSENT record."""
        if not isinstance(doc, dict):
            raise ValueError("verified.json is not an object")
        for key in _REQUIRED:
            if key not in doc:
                raise ValueError(f"verified.json: missing member {key!r}")
        digests = doc["digests"]
        if not isinstance(digests, dict):
            raise ValueError("verified.json: digests is not an object")
        for key in _REQUIRED_DIGESTS:
            if key not in digests:
                raise ValueError(f"verified.json: missing digests member {key!r}")
        schema = doc["schema"]
        if type(schema) is not int or schema != 1:
            raise ValueError("verified.json: schema is not 1")

        def text(name: str, nullable: bool = False) -> str | None:
            v = doc[name]
            if v is None and nullable:
                return None
            if not isinstance(v, str):
                raise ValueError(f"verified.json: {name} is not a string")
            return v

        platform = text("platform")
        if not any(p["key"] == platform for p in C.PLATFORMS):
            raise ValueError(f"verified.json: platform {platform!r} is not one of v1's")
        version = text("version")
        if not _VERSION.match(version or ""):
            raise ValueError("verified.json: version is not four-part")
        build = text("build")
        if not build:
            raise ValueError("verified.json: empty build")
        library = text("library")
        if (
            not library
            or "/" in library
            or "\\" in library
            or library in (".", "..")
            or os.path.isabs(library)
        ):
            raise ValueError("verified.json: library is not a plain relative file name")
        sha = text("library_sha256")
        if not _HEX64.match(sha or ""):
            raise ValueError("verified.json: library_sha256 is not 64 lowercase hex")
        size = doc["library_bytes"]
        if type(size) is not int or size < 0:
            raise ValueError("verified.json: library_bytes is not a non-negative integer")
        if not isinstance(doc["predicate"], dict):
            raise ValueError("verified.json: predicate is not an object")

        def digest(name: str, nullable: bool) -> str | None:
            v = digests[name]
            if v is None and nullable:
                return None
            if not isinstance(v, str) or not _DIGEST.match(v):
                raise ValueError(f"verified.json: digests.{name} is not sha256:<hex>")
            return v

        return cls(
            platform=platform or "",
            version=version or "",
            build=build,
            channel=text("channel", True),
            library=library,
            library_sha256=sha or "",
            library_bytes=size,
            index=digest("index", True),
            manifest=digest("manifest", False) or "",
            layer=digest("layer", False) or "",
            bundle=digest("bundle", True),
            bundle_manifest=digest("bundle_manifest", True),
            signed_by=text("signed_by", True),
            predicate=doc["predicate"],
        )


# The modes a new file and a new directory are asked for; the process umask
# alone takes bits away (docs/guides/fetch-v1.md §1, modes; public issue #486).
# At the usual umask 022 a cache one uid writes is readable by every other uid,
# and readable is never writable. `tempfile.mkstemp` (0600) is never used for a
# file that is renamed into the cache: the private mode would survive the
# rename, and another uid would read the cache as empty.
_FILE_MODE = 0o666
_TEMP_ATTEMPTS = 64


def _create_temp_file(directory: Path, prefix: str) -> tuple[int, str]:
    """A new file named `prefix` plus a random suffix in `directory`, opened
    for writing, with mode 0666 less the umask: `(fd, path)`."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_TEMP_ATTEMPTS):
        name = os.path.join(str(directory), prefix + os.urandom(6).hex())
        try:
            return os.open(name, flags, _FILE_MODE), name
        except FileExistsError:
            continue
    raise FileExistsError(f"chtypes: no free temporary name for {prefix}* in {directory}")


def _atomic_write(
    path: Path, data: bytes, *, before_rename: Callable[[], None] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = _create_temp_file(path.parent, ".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if before_rename is not None:
            before_rename()
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_verified_install(
    root: Path,
    manifest_digest: str,
    record: VerifiedRecord,
    *,
    unpacked_tmp_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Install an already-unpacked library directory plus its
    `verified.json`, atomically: the caller unpacks into a sibling temp
    directory first (`_unpack.unpack_tar_zst` does), the canonical record is
    written into it, and the whole directory is renamed into place
    (`_install_dir`). A destination that already carries an acceptable
    record is left alone. One that does not (foreign, torn or older-format)
    was just re-verified by the caller from the cache's own blobs, so it is
    moved aside and replaced."""
    hex_digest = manifest_digest.split(":", 1)[1]
    dest = unpacked_dir_for(root, hex_digest)
    if read_verified_record(dest) is not None:
        if unpacked_tmp_dir is not None:
            shutil.rmtree(str(unpacked_tmp_dir), ignore_errors=True)
        return dest
    if unpacked_tmp_dir is None:
        raise ValueError("write_verified_install: nothing to install")
    tmp = Path(unpacked_tmp_dir)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            tmp / C.CACHE_VERIFIED_RECORD,
            json.dumps(record.to_json(), sort_keys=True).encode("utf-8"),
        )
        _install_dir(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


# _install_dir's bounded retries: each follows another process changing the
# destination under it, so a handful is plenty.
_INSTALL_ATTEMPTS = 8


def _install_dir(src: Path, dest: Path) -> bool:
    """Move `src`, a complete directory carrying its `verified.json`, to
    `dest` by rename, and never remove an entry another process may be using
    (public issue #482). Several processes of any binding may install the
    same build into one cache at once, and each must end up with a usable
    `dest`; Go, TypeScript and Rust follow the same rule:

    - The rename comes first. It fails while `dest` exists (POSIX refuses to
      rename a directory onto a non-empty one), so the first installer wins
      and every later one finds `dest` in place.
    - A `dest` that holds an acceptable record is kept: the same content is
      already installed.
    - Only a `dest` WITHOUT an acceptable record (foreign, torn or older
      format) is moved aside, into a fresh `.stale-*` directory beside it,
      and replaced. If what was moved turns out to carry an acceptable record
      (another installer's rename landed in between), it is put back.

    Returns True when another process's install is the one in place. The
    caller removes `src` if it is still there."""
    last: OSError | None = None
    for _ in range(_INSTALL_ATTEMPTS):
        try:
            os.rename(src, dest)
            return False
        except OSError as exc:
            last = exc
        if read_verified_record(dest) is not None:
            return True
        if not (dest.exists() or dest.is_symlink()):
            continue  # what stood there went away: try the rename again
        stale = Path(tempfile.mkdtemp(dir=str(dest.parent), prefix=".stale-"))
        aside = stale / "old"
        try:
            os.rename(dest, aside)
        except OSError:
            shutil.rmtree(stale, ignore_errors=True)
            continue  # another process moved or replaced it: look again
        if read_verified_record(aside) is not None:
            try:
                os.rename(aside, dest)
                shutil.rmtree(stale, ignore_errors=True)
                return True
            except OSError:
                pass  # re-occupied meanwhile: look again
        shutil.rmtree(stale, ignore_errors=True)
    assert last is not None
    raise last


def read_verified_record(dest_dir: Path) -> VerifiedRecord | None:
    """The directory's canonical record, or `None` when it is missing,
    unparsable, of another schema, or breaks a rule: an unreadable record is
    ABSENT, never fatal and never trusted."""
    record_path = dest_dir / C.CACHE_VERIFIED_RECORD
    try:
        with open(record_path, encoding="utf-8") as f:
            return VerifiedRecord.from_json(json.load(f, object_pairs_hook=_no_duplicates))
    except (OSError, ValueError, RecursionError):
        return None


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def list_verified_records(
    roots: Sequence[Path],
) -> list[tuple[Path, VerifiedRecord]]:
    """Every `verified.json` under `unpacked/sha256/*/` across every search
    root, each paired with the directory it lives in (`Resolved.dir`)."""
    out: list[tuple[Path, VerifiedRecord]] = []
    for root in roots:
        unpacked_root = root / C.CACHE_UNPACKED_DIR
        if not unpacked_root.is_dir():
            continue
        for entry in sorted(unpacked_root.iterdir()):
            if not entry.is_dir() or not _HEX64.match(entry.name):
                continue
            record = read_verified_record(entry)
            if record is not None:
                out.append((entry, record))
    return out


_ZERO_X_MINOR = re.compile(r"^[0-9]+\.[0-9]+$")


def zero_x_shape(root: Path) -> str | None:
    """The `<minor>/manifest.json` a 0.x registry directory holds, when `root`
    has that shape: no `oci-layout`, no `unpacked/`, and at least one
    `<minor>/manifest.json` (the first by name). A missing `oci-layout` alone
    is not the shape (this binding's own 1.x caches have none), and a
    directory with `unpacked/` is a 1.x cache whoever wrote it. `None` for
    anything else, including a directory that is missing or cannot be read."""
    for name in ("oci-layout", "unpacked"):
        try:
            os.lstat(root / name)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        return None
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        if not _ZERO_X_MINOR.match(name):
            continue
        try:
            if not os.path.isdir(root / name) or os.path.islink(root / name):
                continue
            if os.path.isfile(root / name / "manifest.json") and not os.path.islink(
                root / name / "manifest.json"
            ):
                return f"{name}/manifest.json"
        except OSError:
            continue
    return None


def zero_x_hint(root: Path) -> str | None:
    """The sentence a CHTYPES_ARTIFACT_MISSING answer from a cache with the 0.x
    shape carries (docs/guides/fetch-v1.md, "Upgrading from 0.x"), else
    `None`."""
    shape = zero_x_shape(root)
    if shape is None:
        return None
    return (
        f"{root} holds a 0.x registry ({shape}); chtypes 1.x uses an OCI layout at "
        "${XDG_CACHE_HOME:-~/.cache}/chtypes/v1 — point CHTYPES_CACHE at an empty or 1.x directory."
    )


def _read_index_or_default(index_path: Path) -> dict:
    try:
        with open(index_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"schemaVersion": 2, "manifests": []}


def update_index_json(
    root: Path,
    mutate: Callable[[dict], dict],
    *,
    before_rename: Callable[[], None] | None = None,
    max_attempts: int = 10,
) -> None:
    """Read-merge-write, re-checked immediately before the rename, re-applied
    on top of whatever is there if it moved (PLAN §3.2 "index-race-reapply").

    `index.json` is written by atomic rename, never in place — but a bare
    "read, merge, rename" still loses a concurrent writer's update if we
    rename over it blindly: `before_rename` is the exact gap PLAN §3.2 names
    ("between temp-write and rename"), so the live file is read ONE more
    time right there, immediately before our own `os.replace`. If it no
    longer matches what we started from, our temp file is stale (computed
    from a `current` that is no longer current) — we discard it, re-merge
    on top of the fresh read, and retry, so a competing writer's entry is
    folded in rather than clobbered. `before_rename` itself is test-only:
    the conformance runner uses it to inject a competing writer
    deterministically; it is never set outside tests.
    """
    index_path = root / "index.json"
    base = _read_index_or_default(index_path)
    for attempt in range(max_attempts):
        updated = mutate(base)
        data = json.dumps(updated, sort_keys=True).encode("utf-8")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = _create_temp_file(index_path.parent, ".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if before_rename is not None and attempt == 0:
                before_rename()
            live_now = _read_index_or_default(index_path)
            if live_now != base:
                # Someone else wrote in the gap: our temp was computed from a
                # base that is no longer current. Re-merge on top of the
                # fresh read instead of clobbering it.
                base = live_now
                continue
            os.replace(tmp_name, index_path)
            return
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    raise RuntimeError(
        f"chtypes: cache index.json at {index_path} could not converge after "
        f"{max_attempts} attempts (a persistent concurrent writer)"
    )
