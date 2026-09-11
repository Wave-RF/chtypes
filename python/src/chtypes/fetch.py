"""Fetching, verifying and installing artifacts — docs/guides/fetch.md, in Python.

    import chtypes
    chtypes.ensure("25.8")            # installed and verified, or an ArtifactError

`ensure` is idempotent: an already-installed line whose library hashes what
the release says is a no-op; otherwise the release is fetched through the
five-link chain (§3) — signature over ``SHA256SUMS`` first, ``index.json``
cross-checked against it, the tarball hashed before it is unpacked, the
manifest inside cross-checked, the installed library re-hashed in place —
and installed atomically into the first registry directory fetch writes to
(§1). Nothing here is a verdict but the chain: not an exit code, not a
``Content-Length``, not "download finished".

The source is ``CHTYPES_ARTIFACTS_URL`` (default the public artifacts host)
plus a release tag, or any ``--url`` base: an ``http(s)://`` host, a
``file://`` URL or a plain directory. Only the standard library is used —
``urllib`` for HTTP, ``tarfile`` for the archive, and ``_ed25519`` for the
signature — so the binding stays zero-dependency.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from ._ed25519 import verify as _ed25519_verify
from ._manifest import (
    Manifest,
    cache_registry_dir,
    host_platform,
    minor_of,
    read_manifest,
    verify_library,
)
from .errors import (
    ArtifactCorruptError,
    ArtifactPinnedError,
    ArtifactUnpublishedError,
    ArtifactUntrustedError,
    SourceUnreachableError,
    UnsignedArtifactWarning,
)

__all__ = [
    "DEFAULT_ARTIFACTS_URL",
    "DEFAULT_TAG",
    "ENV_ALLOW_UNSIGNED",
    "ENV_ARTIFACTS_URL",
    "ENV_AUTOFETCH",
    "ENV_TRUSTED_KEYS",
    "LOCK_SCHEMA",
    "PLATFORMS",
    "RELEASE_KEY_ID",
    "RELEASE_PUBLIC_KEY",
    "SYSTEM_REGISTRY_ROOTS",
    "Fetcher",
    "Release",
    "ReleaseEntry",
    "ensure",
    "fetch_lines",
    "fetch_destination",
    "installed_lines",
    "read_lock",
    "registry_search_path",
    "trusted_keys",
    "verify_registry",
    "write_lock",
]

DEFAULT_ARTIFACTS_URL: Final = "https://artifacts.wavehouse.dev"
DEFAULT_TAG: Final = "artifacts"
ENV_ARTIFACTS_URL: Final = "CHTYPES_ARTIFACTS_URL"
ENV_TRUSTED_KEYS: Final = "CHTYPES_TRUSTED_KEYS"
ENV_ALLOW_UNSIGNED: Final = "CHTYPES_ALLOW_UNSIGNED"
ENV_AUTOFETCH: Final = "CHTYPES_AUTOFETCH"
ENV_REGISTRY: Final = "CHTYPES_REGISTRY"

#: How many times `Fetcher.release` reads an ``http(s)`` release before giving
#: up, and the wait between reads: three attempts span about ten seconds, which
#: comfortably outlasts a one-object publish window.
RELEASE_LOAD_ATTEMPTS: Final = 3
RELEASE_RETRY_DELAY: Final = 4

#: The served golden set: a release-level file, and a row in the signed
#: SHA256SUMS, installed beside the artifacts as ``<registry>/sdk-goldens.json``.
GOLDENS_ASSET: Final = "sdk-goldens.json"

#: The ``-b<N>`` a current artifact file name ends with. A name without one is
#: an old row, which is build 0 by definition.
_BUILD_SUFFIX: Final = re.compile(r"-b([0-9]+)\.tar\.gz$")

#: The release signing key (docs/guides/fetch.md §4): the raw 32-byte ed25519 public
#: key, hex, and its id — the first 16 hex characters of sha256 over the raw
#: key. Every SDK embeds this constant; `CHTYPES_TRUSTED_KEYS` replaces it.
RELEASE_PUBLIC_KEY: Final = "fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc"
RELEASE_KEY_ID: Final = "deb275922dbff76e"

#: Slot 4 of the search path: system locations, reserved for the deferred
#: system packages and for images that bake artifacts in. Never written to.
SYSTEM_REGISTRY_ROOTS: Final = ("/usr/local/share/chtypes/artifacts", "/opt/chtypes/artifacts")

PLATFORMS: Final = ("linux-arm64", "linux-amd64", "darwin-arm64", "darwin-amd64")
LOCK_SCHEMA: Final = 1
#: The lock file ``--frozen`` reads when no ``--lock`` names one (docs/guides/fetch.md,
#: Decisions): relative, so it resolves against the working directory.
DEFAULT_LOCK_FILE: Final = "chtypes.lock"

_USER_AGENT = "chtypes-python (+https://github.com/wave-rf/chtypes)"
_HTTP_ATTEMPTS = 3
_HTTP_TIMEOUT = 60.0
_CHUNK = 1 << 20

log = logging.getLogger("chtypes.fetch")

Progress = Callable[[str], None]


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ §1 paths


def registry_search_path(
    explicit: str | os.PathLike[str] | None = None, *, platform: str | None = None
) -> tuple[Path, ...]:
    """The registry search path (docs/guides/fetch.md §1), in order: the explicit
    path, ``$CHTYPES_REGISTRY``, the per-user cache, then the system
    locations. Directories need not exist; a lookup takes the first one that
    holds the requested line. De-duplicated, order preserved.

    ``$CHTYPES_REGISTRY`` is a directory this host dlopens from, so it is on
    the path for the host's platform only: another platform's artifacts
    (``fetch --platform``) go to that platform's own cache directory
    (docs/guides/fetch.md, Decisions)."""
    plat = platform or host_platform()
    candidates: list[str] = []
    if explicit is not None:
        candidates.append(os.fspath(explicit))
    env = os.environ.get(ENV_REGISTRY)
    if env and plat == host_platform():
        candidates.append(env)
    candidates.append(cache_registry_dir(plat))
    candidates.extend(os.path.join(root, plat) for root in SYSTEM_REGISTRY_ROOTS)
    seen: dict[Path, None] = {}
    for c in candidates:
        seen.setdefault(Path(c).expanduser(), None)
    return tuple(seen)


def fetch_destination(
    explicit: str | os.PathLike[str] | None = None, *, platform: str | None = None
) -> Path:
    """Where fetch writes (docs/guides/fetch.md §1): the explicit path, else
    ``$CHTYPES_REGISTRY``, else the per-user cache — never a system location."""
    return registry_search_path(explicit, platform=platform)[0]


def installed_lines(registry: str | os.PathLike[str]) -> dict[str, tuple[Path, Manifest]]:
    """Every installed line under one registry directory: minor line ->
    (version directory, manifest), in release order. Cheap — reads the
    manifests, loads nothing."""
    root = Path(registry)
    found: dict[str, tuple[Path, Manifest]] = {}
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        manifest = read_manifest(entry)
        if manifest is None:
            continue
        minor = manifest.clickhouse_minor or minor_of(manifest.clickhouse_version) or entry.name
        found.setdefault(minor, (entry, manifest))
    return dict(sorted(found.items(), key=lambda kv: _minor_key(kv[0])))


def verify_registry(
    registry: str | os.PathLike[str],
) -> list[tuple[str, Path, ArtifactCorruptError | None]]:
    """Re-hash every installed line against its own manifest (the
    ``chtypes verify`` command): ``[(minor, dir, None | error), …]``."""
    report: list[tuple[str, Path, ArtifactCorruptError | None]] = []
    for minor, (directory, _) in installed_lines(registry).items():
        try:
            verify_library(directory)
        except Exception as exc:  # RegistryError from verify_library, or an OSError
            report.append((minor, directory, ArtifactCorruptError(str(exc))))
        else:
            report.append((minor, directory, None))
    return report


def _minor_key(minor: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in minor.split("."))
    except ValueError:
        return (1 << 30,)


def _version_key(version: str) -> tuple[int, ...]:
    return _minor_key(version.split("-", 1)[0])


# --------------------------------------------------------------- §4 signature


def trusted_keys(explicit: Iterable[str] | None = None) -> tuple[bytes, ...]:
    """The raw public keys a release may be signed with: the explicit list,
    else ``$CHTYPES_TRUSTED_KEYS`` (comma-separated hex), else the embedded
    release key. Each REPLACES the next (docs/guides/fetch.md §4)."""
    if explicit is None:
        env = os.environ.get(ENV_TRUSTED_KEYS, "")
        explicit = [k for k in env.split(",") if k.strip()] if env.strip() else None
    if explicit is None:
        explicit = [RELEASE_PUBLIC_KEY]
    keys: list[bytes] = []
    for hexkey in explicit:
        try:
            raw = bytes.fromhex(hexkey.strip())
        except ValueError:
            raise ValueError(f"chtypes: trusted key {hexkey!r} is not hex") from None
        if len(raw) != 32:
            raise ValueError(f"chtypes: trusted key {hexkey!r} is not a 32-byte ed25519 key")
        keys.append(raw)
    if not keys:
        raise ValueError("chtypes: no trusted keys given")
    return tuple(keys)


_resolve_keys = trusted_keys  # the parameter of the same name shadows it in Fetcher


def key_id(raw_public_key: bytes) -> str:
    """The key id docs/guides/fetch.md §4 defines: sha256 over the raw key, first 16 hex."""
    return hashlib.sha256(raw_public_key).hexdigest()[:16]


def _parse_signature(sig: bytes) -> tuple[str, bytes]:
    """``SHA256SUMS.sig`` -> (the comment line, the 64 signature bytes)."""
    comment = ""
    body: list[str] = []
    for raw in sig.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("untrusted comment:"):
            comment = line[len("untrusted comment:") :].strip()
            continue
        body.append(line)
    try:
        signature = base64.b64decode("".join(body), validate=True)
    except (ValueError, TypeError):
        raise ArtifactUntrustedError("chtypes: SHA256SUMS.sig is not a base64 signature") from None
    if len(signature) != 64:
        raise ArtifactUntrustedError(
            f"chtypes: SHA256SUMS.sig carries {len(signature)} bytes, an ed25519 signature is 64"
        )
    return comment, signature


def verify_sums_signature(sums: bytes, sig: bytes, keys: Sequence[bytes], source: str) -> str:
    """Step 0 of the chain: the signature over the EXACT bytes of ``SHA256SUMS``
    with one of the trusted keys. Returns the id of the key that verified;
    raises `ArtifactUntrustedError` otherwise."""
    comment, signature = _parse_signature(sig)
    for raw in keys:
        if _ed25519_verify(raw, sums, signature):
            return key_id(raw)
    trusted = ", ".join(key_id(k) for k in keys)
    raise ArtifactUntrustedError(
        f"chtypes: SHA256SUMS at {source} is not signed by a trusted key "
        f"(signature says {comment!r}; trusted: {trusted}). "
        f"Not installing anything from it."
    )


# ------------------------------------------------------------------ §5 lock


def read_lock(path: str | os.PathLike[str]) -> dict[str, dict[str, str]]:
    """The pins in a lock file: ``{"<os>-<arch>/<minor>": {"file", "sha256"}}``;
    ``{}`` when the file does not exist. A malformed file is a `ValueError`."""
    p = Path(path)
    try:
        doc = json.loads(p.read_bytes())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"chtypes: cannot read lock file {p}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != LOCK_SCHEMA:
        raise ValueError(f"chtypes: {p} is not a chtypes lock file (schema {LOCK_SCHEMA})")
    artifacts = doc.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError(f"chtypes: {p} has no 'artifacts' object")
    pins: dict[str, dict[str, str]] = {}
    for key, entry in artifacts.items():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("file"), str)
            or not isinstance(entry.get("sha256"), str)
        ):
            raise ValueError(f"chtypes: {p}: entry {key!r} needs 'file' and 'sha256'")
        pins[key] = {"file": entry["file"], "sha256": entry["sha256"]}
    return pins


def write_lock(path: str | os.PathLike[str], pins: dict[str, dict[str, str]]) -> None:
    """Write a schema-1 lock file atomically (temp sibling + rename), keys sorted."""
    p = Path(path)
    doc = {"schema": LOCK_SCHEMA, "artifacts": dict(sorted(pins.items()))}
    text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent or ".")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ----------------------------------------------------------------- the release


@dataclass(frozen=True, slots=True)
class ReleaseEntry:
    """One row of ``index.json``: one artifact for one platform."""

    file: str
    sha256: str
    bytes: int
    clickhouse_version: str
    clickhouse_minor: str
    library: str
    library_sha256: str
    os: str
    arch: str
    #: The wrapper build for this ClickHouse version. A rebuild of the same
    #: version is a NEW row beside the old one, never a swap, so this is what
    #: separates them. 0 on a row published before builds existed.
    build: int = 0
    #: The core commit the wrapper was built from; ``""`` on an old row.
    core_commit: str = ""

    @property
    def platform(self) -> str:
        return f"{self.os}-{self.arch}"

    @property
    def build_number(self) -> int:
        """The row's own ``build`` when it has one, else the ``-b<N>`` suffix of
        the file name, else 0 — an old row, which is build 0 by definition."""
        if self.build > 0:
            return self.build
        m = _BUILD_SUFFIX.search(self.file)
        return int(m.group(1)) if m else 0

    @property
    def rank(self) -> tuple[tuple[int, ...], int]:
        """Sort key for "which row wins": newest ClickHouse version, then the
        highest wrapper build. Without the build half a rebuild's older sibling
        could win on nothing but its position in the index."""
        return (_version_key(self.clickhouse_version), self.build_number)

    @property
    def minor(self) -> str:
        return self.clickhouse_minor or minor_of(self.clickhouse_version)


@dataclass(frozen=True, slots=True)
class Release:
    """A release as the chain saw it: its entries, the SIGNED sums, and who signed."""

    source: str
    tag: str
    entries: tuple[ReleaseEntry, ...]
    sums: dict[str, str]
    signed_by: str | None  # key id, or None when CHTYPES_ALLOW_UNSIGNED skipped step 0
    license: str = ""
    license_url: str = ""

    def offered(self, platform: str) -> list[ReleaseEntry]:
        """Every line published for a platform, newest patch per line, release order."""
        best: dict[str, ReleaseEntry] = {}
        for e in self.entries:
            if e.platform != platform:
                continue
            cur = best.get(e.minor)
            if cur is None or e.rank > cur.rank:
                best[e.minor] = e
        return [best[m] for m in sorted(best, key=_minor_key)]

    def platforms(self) -> list[str]:
        return sorted({e.platform for e in self.entries})

    def select(self, spelling: str, platform: str) -> ReleaseEntry:
        """A line (``25.8``) resolves to the one patch published for it; an
        exact patch (``25.8.28.1-lts``) is a hard requirement (§2)."""
        line, exact = parse_spelling(spelling)
        offered = self.offered(platform)
        if not offered:
            raise ArtifactUnpublishedError(
                f"chtypes: {self.source} publishes nothing for {platform} "
                f"(it has: {', '.join(self.platforms()) or 'nothing'})"
            )
        if exact is not None:
            bare = exact.split("-", 1)[0]
            # A rebuild publishes the same clickhouse_version twice, so an exact
            # request can match more than one row: take the highest build, never
            # whichever the index happens to list first.
            hits = [
                e
                for e in self.entries
                if e.platform == platform
                and (
                    e.clickhouse_version == exact
                    or ("-" not in exact and e.clickhouse_version.split("-", 1)[0] == bare)
                )
            ]
            if hits:
                return max(hits, key=lambda e: e.rank)
            raise ArtifactUnpublishedError(
                f"chtypes: you asked for exactly ClickHouse {exact} on {platform} and "
                f"{self.source} does not publish it (it has: "
                f"{', '.join(e.clickhouse_version for e in offered)}). "
                f"Ask for the line ({line}) to take what was published."
            )
        for e in offered:
            if e.minor == line:
                return e
        raise ArtifactUnpublishedError(
            f"chtypes: no artifact for ClickHouse line {line} on {platform} at {self.source} "
            f"(it has: {', '.join(e.clickhouse_version for e in offered)})"
        )


def parse_spelling(spelling: str) -> tuple[str, str | None]:
    """``25.8`` -> (``25.8``, None); ``v25.8.28.1-lts`` -> (``25.8``, ``25.8.28.1-lts``).

    The caller's own spelling decides whether a patch is a hard requirement:
    four dotted components name a patch, fewer name a line."""
    s = spelling.strip()
    if s.startswith("v"):
        s = s[1:]
    bare = s.split("-", 1)[0]
    parts = bare.split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"chtypes: cannot make a ClickHouse version out of {spelling!r}")
    line = f"{parts[0]}.{parts[1]}"
    return line, (s if len(parts) >= 4 else None)


def _parse_index(raw: bytes, source: str) -> tuple[list[ReleaseEntry], str, str, str]:
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ArtifactCorruptError(f"chtypes: index.json at {source} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise ArtifactCorruptError(
            f"chtypes: index.json at {source} has schema "
            f"{doc.get('schema') if isinstance(doc, dict) else '?'!r}; this chtypes reads schema 1"
        )
    entries: list[ReleaseEntry] = []
    for row in doc.get("artifacts") or []:
        if not isinstance(row, dict):
            raise ArtifactCorruptError(f"chtypes: index.json at {source} has a malformed row")
        try:
            entries.append(
                ReleaseEntry(
                    file=str(row["file"]),
                    sha256=str(row["sha256"]).lower(),
                    bytes=int(row["bytes"]),
                    clickhouse_version=str(row["clickhouse_version"]),
                    clickhouse_minor=str(row.get("clickhouse_minor") or ""),
                    library=str(row["library"]),
                    library_sha256=str(row["library_sha256"]).lower(),
                    os=str(row["os"]),
                    arch=str(row["arch"]),
                    build=int(row.get("build") or 0),
                    core_commit=str(row.get("core_commit") or ""),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactCorruptError(
                f"chtypes: index.json at {source}: row {row.get('file')!r} is missing {exc}"
            ) from None
    return (
        entries,
        str(doc.get("release_tag") or ""),
        str(doc.get("license") or ""),
        str(doc.get("license_url") or ""),
    )


def _parse_sums(raw: bytes) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        sums[name.lstrip("*").strip()] = digest.lower()
    return sums


# ------------------------------------------------------------------ the source


class _Source:
    """One release base: ``http(s)://…``, ``file://…`` or a plain directory.

    `read` answers None for "not there" (a 404, a missing file) and raises
    `SourceUnreachableError` for a fault reaching the source at all; the
    caller decides which of those is a broken release and which is no release.
    """

    def __init__(self, base: str, progress: Progress) -> None:
        self.base = base.rstrip("/")
        self.progress = progress
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme in ("http", "https"):
            self.kind = "http"
            self.root: Path | None = None
        elif parsed.scheme == "file":
            self.kind = "file"
            self.root = Path(urllib.request.url2pathname(parsed.path))
        elif parsed.scheme == "":
            self.kind = "file"
            self.root = Path(base).expanduser()
        else:
            raise ValueError(f"chtypes: unsupported source URL scheme in {base!r}")

    def __str__(self) -> str:
        return self.base if self.kind == "http" else str(self.root)

    def read(self, name: str) -> bytes | None:
        if self.kind == "file":
            assert self.root is not None
            path = self.root / name
            try:
                return path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise SourceUnreachableError(f"chtypes: cannot read {path}: {exc}") from exc
        with self._open(name) as resp:
            if resp is None:
                return None
            return resp.read()

    def download(self, name: str, into: Path, expected_bytes: int) -> tuple[int, str]:
        """Stream one asset to ``into``, hashing as it goes: (bytes, sha256 hex)."""
        if self.kind == "file":
            assert self.root is not None
            src = self.root / name
            if not src.is_file():
                raise ArtifactCorruptError(
                    f"chtypes: {self} names {name} but does not contain it — a broken release"
                )
            return self._copy(src.open("rb"), into, expected_bytes, name)
        last: Exception | None = None
        for attempt in range(1, _HTTP_ATTEMPTS + 1):
            try:
                with self._open(name) as resp:
                    if resp is None:
                        raise ArtifactCorruptError(
                            f"chtypes: {self} names {name} but does not serve it — a broken release"
                        )
                    return self._copy(resp, into, expected_bytes, name)
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                ConnectionError,
                TimeoutError,
            ) as exc:
                last = exc
                self.progress(f"  {name}: {exc} (attempt {attempt} of {_HTTP_ATTEMPTS})")
                time.sleep(min(2.0 * attempt, 6.0))
        raise SourceUnreachableError(f"chtypes: could not download {name} from {self}: {last}")

    def _copy(self, stream: Any, into: Path, expected_bytes: int, name: str) -> tuple[int, str]:
        digest = hashlib.sha256()
        total = 0
        started = last_said = time.monotonic()
        try:
            with into.open("wb") as out:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    out.write(chunk)
                    total += len(chunk)
                    now = time.monotonic()
                    if now - last_said >= 1.0:
                        last_said = now
                        self.progress(_progress_line(name, total, expected_bytes, now - started))
        finally:
            stream.close()
        self.progress(_progress_line(name, total, expected_bytes, time.monotonic() - started))
        return total, digest.hexdigest()

    def _open(self, name: str) -> _Response:
        assert self.kind == "http"
        url = f"{self.base}/{urllib.parse.quote(name)}"
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        last: Exception | None = None
        for attempt in range(1, _HTTP_ATTEMPTS + 1):
            try:
                return _Response(urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT))
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    exc.close()
                    return _Response(None)
                last = exc
                if exc.code < 500 and exc.code != 429:
                    break  # a definite refusal is not a transient fault
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                last = exc
            self.progress(f"  {name}: {last} (attempt {attempt} of {_HTTP_ATTEMPTS})")
            time.sleep(min(2.0 * attempt, 6.0))
        raise SourceUnreachableError(f"chtypes: cannot reach {url}: {last}")


class _Response:
    """A context manager around an HTTP response that may be "not found" (None)."""

    def __init__(self, resp: Any) -> None:
        self._resp = resp

    def __enter__(self) -> Any:
        return self._resp

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._resp is not None:
            self._resp.close()


def _progress_line(name: str, got: int, want: int, seconds: float) -> str:
    rate = got / seconds / 1e6 if seconds > 0 else 0.0
    if want >= 1e6:
        pct = 100 * got / want
        return f"  {name}: {got / 1e6:.1f} / {want / 1e6:.1f} MB ({pct:.0f}%, {rate:.1f} MB/s)"
    if want:
        return f"  {name}: {got} / {want} bytes"
    return f"  {name}: {got / 1e6:.1f} MB ({rate:.1f} MB/s)"


# ------------------------------------------------------------------- the chain


class Fetcher:
    """The resolved options for one fetch operation, and the chain itself.

    Construct once, then `ensure` one or more lines: the release (signature,
    sums, index) is read from the source once and every line installs
    through it. `ensure()` and `fetch()` are the one-call conveniences.
    """

    def __init__(
        self,
        *,
        dest: str | os.PathLike[str] | None = None,
        platform: str | None = None,
        url: str | None = None,
        tag: str | None = None,
        lock: str | os.PathLike[str] | None = None,
        frozen: bool = False,
        force: bool = False,
        offline: bool = False,
        trusted_keys: Iterable[str] | None = None,
        allow_unsigned: bool | None = None,
        progress: Progress | None = None,
    ) -> None:
        self.platform = platform or host_platform()
        if self.platform not in PLATFORMS:
            raise ValueError(
                f"chtypes: not a known platform key: {self.platform!r} "
                f"(one of {', '.join(PLATFORMS)})"
            )
        if url and tag:
            raise ValueError(
                "chtypes: --url names a full base; --tag selects a release on the artifacts host"
                " — pass one"
            )
        if frozen and lock is None:
            # --frozen alone reads ./chtypes.lock (docs/guides/fetch.md, Decisions).
            lock = DEFAULT_LOCK_FILE
        self.dest = fetch_destination(dest, platform=self.platform)
        self.tag = tag or DEFAULT_TAG
        host = (os.environ.get(ENV_ARTIFACTS_URL) or DEFAULT_ARTIFACTS_URL).rstrip("/")
        base = url or f"{host}/{self.tag}"
        self.progress: Progress = progress or (lambda line: None)
        self.source = _Source(base, self._say)
        self.lock = Path(lock) if lock is not None else None
        self.frozen = frozen
        self.force = force
        self.offline = offline
        self.keys = _resolve_keys(trusted_keys)
        self.allow_unsigned = (
            allow_unsigned if allow_unsigned is not None else _env_flag(ENV_ALLOW_UNSIGNED)
        )
        self._release: Release | None = None
        self._pins: dict[str, dict[str, str]] | None = None

    # ------------------------------------------------------------ plumbing

    def _say(self, line: str) -> None:
        log.info("%s", line)
        self.progress(line)

    def _lock_pins(self) -> dict[str, dict[str, str]]:
        if self._pins is None:
            if self.lock is None:
                self._pins = {}
            else:
                if self.frozen and not self.lock.is_file():
                    raise ArtifactPinnedError(
                        f"chtypes: --frozen, but there is no lock file at {self.lock}; "
                        f"nothing is pinned, so nothing is installed"
                    )
                self._pins = read_lock(self.lock)
        return self._pins

    # ------------------------------------------------------------ the release

    def release(self) -> Release:
        """Steps 0–1, retried through a publish window.

        A publish into the rolling release is three objects — ``SHA256SUMS``,
        ``SHA256SUMS.sig``, ``index.json`` — and object storage cannot swap them
        atomically. They go up in that order, so an old index read against new
        sums still cross-checks; the unsafe window is between the sums and the
        signature that covers them, one small object wide and seconds long.

        The two symptoms of reading inside it — a signature under no trusted key
        (`ArtifactUntrustedError`) and an index that disagrees with the sums
        (`ArtifactCorruptError`) — are retried. Nothing else is, and neither are
        these once the attempts run out: the same exception surfaces, with the
        same exit code, as it did before. A tarball whose hash is wrong is never
        retried; that is the release lying about a byte, not a half-finished
        upload.

        Only an ``http(s)`` source can be mid-publish, so a ``file://`` or
        directory source is read exactly once. Read once per `Fetcher`."""
        if self._release is not None:
            return self._release
        attempts = RELEASE_LOAD_ATTEMPTS if self.source.kind == "http" else 1
        for attempt in range(1, attempts + 1):
            try:
                self._release = self._load_release()
                return self._release
            except (ArtifactUntrustedError, ArtifactCorruptError) as exc:
                if attempt >= attempts:
                    raise
                self._say(
                    f"{exc} (attempt {attempt}/{attempts}) — this is what a release being "
                    f"published looks like from outside; retrying in {RELEASE_RETRY_DELAY}s"
                )
                time.sleep(RELEASE_RETRY_DELAY)
        raise AssertionError("unreachable")  # pragma: no cover

    def _load_release(self) -> Release:
        """One read of the release's three small files. Never memoizes: the
        caller does that, and only on success."""
        if self.offline:
            raise SourceUnreachableError(
                f"chtypes: offline — not reading the release at {self.source}"
            )
        src = str(self.source)
        sums_raw = self.source.read("SHA256SUMS")
        if sums_raw is None:
            raise SourceUnreachableError(f"chtypes: no SHA256SUMS at {src} — not a chtypes release")
        sig_raw = self.source.read("SHA256SUMS.sig")
        signed_by: str | None
        if self.allow_unsigned:
            what = (
                "no SHA256SUMS.sig is published"
                if sig_raw is None
                else "its signature was NOT checked"
            )
            message = (
                f"chtypes: {ENV_ALLOW_UNSIGNED}=1 — installing from {src} WITHOUT verifying "
                f"the release signature ({what}). Only for a source you already trust."
            )
            warnings.warn(message, UnsignedArtifactWarning, stacklevel=3)
            self._say("WARNING: " + message)
            signed_by = None
        elif sig_raw is None:
            raise ArtifactUntrustedError(
                f"chtypes: the release at {src} is unsigned (no SHA256SUMS.sig). "
                f"Not installing anything from it; {ENV_ALLOW_UNSIGNED}=1 overrides, loudly."
            )
        else:
            signed_by = verify_sums_signature(sums_raw, sig_raw, self.keys, src)
            self._say(f"SHA256SUMS signature verified (ed25519 key {signed_by})")
        index_raw = self.source.read("index.json")
        if index_raw is None:
            raise SourceUnreachableError(f"chtypes: no index.json at {src} — not a chtypes release")
        entries, tag, license_, license_url = _parse_index(index_raw, src)
        release = Release(
            source=src,
            tag=tag or self.tag,
            entries=tuple(entries),
            sums=_parse_sums(sums_raw),
            signed_by=signed_by,
            license=license_,
            license_url=license_url,
        )
        # §3 step 2 for the whole release, not just the asset being installed.
        # `install` still checks its own asset; this runs here so a disagreement
        # is seen while `release` can still fix it by reading all three again.
        for entry in release.entries:
            listed = release.sums.get(entry.file)
            if listed is not None and listed != entry.sha256:
                raise ArtifactCorruptError(
                    f"chtypes: index.json says {entry.file} is {entry.sha256} but SHA256SUMS "
                    f"says {listed} — the release disagrees with itself; not installing it"
                )
        if license_:
            self._say(
                f"artifacts are licensed under {license_}{' ' + license_url if license_url else ''}"
                f" — LICENSE and NOTICE ship beside them"
            )
        return release

    # --------------------------------------------------------------- ensure

    def ensure(self, spelling: str) -> Path:
        """One line through the chain; returns ``<registry>/<minor>``."""
        line, exact = parse_spelling(spelling)
        if self.offline:
            return self._ensure_offline(line, exact)
        entry = self.release().select(spelling, self.platform)
        installed = self.install(entry)
        self.install_goldens()
        return installed

    def ensure_all(self) -> list[Path]:
        """Every line the release publishes for the platform."""
        offered = self.release().offered(self.platform)
        if not offered:
            raise ArtifactUnpublishedError(
                f"chtypes: {self.release().source} publishes nothing for {self.platform}"
            )
        out = [self.install(entry) for entry in offered]
        self.install_goldens()
        return out

    def install_goldens(self) -> Path | None:
        """Install the served golden set, if this release publishes one.

        ``sdk-goldens.json`` is a release-level file like ``index.json`` and a
        row in the signed ``SHA256SUMS`` like a tarball, so it verifies through
        the same chain and lands beside the artifacts, where every binding's
        golden test reads it offline.

        Never raises: a release with no such row simply predates the served set,
        and a set that cannot be written leaves the golden tests skipping
        loudly, which is their job when there is nothing to read. What it will
        not do is install bytes the signed ``SHA256SUMS`` does not describe.
        """
        release = self.release()
        want = release.sums.get(GOLDENS_ASSET)
        if want is None:
            self._say(
                f"this release does not publish {GOLDENS_ASSET} "
                f"(the SDKs' golden tests will skip until it does)"
            )
            return None
        try:
            blob = self.source.read(GOLDENS_ASSET)
        except OSError as exc:
            self._say(f"could not read {GOLDENS_ASSET}: {exc} — the golden tests will skip")
            return None
        if blob is None:
            self._say(
                f"SHA256SUMS lists {GOLDENS_ASSET} but {self.source} does not serve it "
                f"— NOT installing it"
            )
            return None
        got = hashlib.sha256(blob).hexdigest()
        if got != want:
            self._say(
                f"NOT installing {GOLDENS_ASSET}: it hashes to {got} but the signed "
                f"SHA256SUMS says {want}"
            )
            return None
        try:
            self.dest.mkdir(parents=True, exist_ok=True)
            path = self.dest / GOLDENS_ASSET
            path.write_bytes(blob)
        except OSError as exc:
            self._say(f"could not write {GOLDENS_ASSET}: {exc} — the golden tests will skip")
            return None
        self._say(f"golden set verified and installed: {path}")
        return path

    def _ensure_offline(self, line: str, exact: str | None) -> Path:
        install = self.dest / line
        manifest = read_manifest(install)
        if manifest is None:
            raise SourceUnreachableError(
                f"chtypes: offline — ClickHouse {exact or line} ({self.platform}) is not installed "
                f"in {self.dest} and nothing may be fetched from {self.source}"
            )
        if exact is not None and manifest.clickhouse_version not in (exact, exact.split("-")[0]):
            raise SourceUnreachableError(
                f"chtypes: offline — {install} holds ClickHouse {manifest.clickhouse_version}, "
                f"not the {exact} asked for, and nothing may be fetched"
            )
        self._verify_in_place(install, expected_sha=None)
        self._say(f"installed (offline: verified against its own manifest): {install}")
        return install

    def install(self, entry: ReleaseEntry) -> Path:
        """Steps 2–4 for one release entry, plus the lock and the atomic move."""
        release = self.release()
        minor = entry.minor
        install = self.dest / minor
        self._say(
            f"{entry.file}  ({entry.bytes} bytes, ClickHouse {entry.clickhouse_version}, "
            f"library {entry.library})"
        )

        # §5: the lock is consulted before anything is downloaded or trusted.
        pin_key = f"{self.platform}/{minor}"
        pins = self._lock_pins()
        pinned = pins.get(pin_key)
        if pinned is not None:
            if pinned["file"] != entry.file or pinned["sha256"].lower() != entry.sha256:
                raise ArtifactPinnedError(
                    f"chtypes: {self.lock} pins {pin_key} to {pinned['file']} "
                    f"(sha256 {pinned['sha256']}) but {release.source} offers {entry.file} "
                    f"(sha256 {entry.sha256}). Refusing the drift; re-run without --frozen "
                    f"and with --lock to re-pin deliberately."
                )
        elif self.frozen:
            raise ArtifactPinnedError(
                f"chtypes: --frozen and {self.lock} has no pin for {pin_key}; "
                f"nothing unpinned is installed under --frozen"
            )

        # Idempotence: installed and hashing what the release says is a no-op.
        if not self.force and self._installed_matches(install, entry):
            self._say(f"already installed and verified: {install / entry.library}")
            self._record_pin(pin_key, entry)
            return install

        # §3 step 2: the signed SHA256SUMS must agree with index.json.
        sums_sha = release.sums.get(entry.file)
        if sums_sha is None:
            raise ArtifactCorruptError(
                f"chtypes: SHA256SUMS at {release.source} has no line for {entry.file} — "
                f"the release disagrees with itself; not installing it"
            )
        if sums_sha != entry.sha256:
            raise ArtifactCorruptError(
                f"chtypes: index.json says {entry.file} is {entry.sha256} but SHA256SUMS says "
                f"{sums_sha} — the release disagrees with itself; not installing it"
            )

        self.dest.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".{minor}.incoming.", dir=self.dest))
        try:
            # §3 step 3: the tarball is hashed BEFORE it is unpacked.
            tarball = work / entry.file
            self._say(f"downloading {entry.file} from {release.source}")
            got_bytes, got_sha = self.source.download(entry.file, tarball, entry.bytes)
            if got_bytes != entry.bytes:
                raise ArtifactCorruptError(
                    f"chtypes: {entry.file} is {got_bytes} bytes, the release says {entry.bytes} "
                    f"— NOT unpacking it"
                )
            if got_sha != entry.sha256:
                raise ArtifactCorruptError(
                    f"chtypes: {entry.file} FAILED its sha256: got {got_sha}, want {entry.sha256} "
                    f"— NOT unpacking it"
                )
            self._say(f"sha256 verified before unpacking: {got_sha}")

            # §3 step 4: the manifest inside names the library and its sha256.
            stage = work / "stage"
            stage.mkdir()
            _extract(tarball, stage, entry.file)
            tarball.unlink()
            manifest = read_manifest(stage)
            if manifest is None:
                raise ArtifactCorruptError(
                    f"chtypes: {entry.file} contains no usable manifest.json at its root"
                )
            claims = (
                manifest.library,
                manifest.clickhouse_version,
                manifest.clickhouse_minor or minor_of(manifest.clickhouse_version),
                manifest.library_sha256.lower(),
            )
            expect = (entry.library, entry.clickhouse_version, minor, entry.library_sha256)
            if claims != expect:
                raise ArtifactCorruptError(
                    f"chtypes: manifest.json inside {entry.file} disagrees with index.json "
                    f"({'/'.join(claims)} vs {'/'.join(expect)}) — the index was built from a "
                    f"different artifact; not installing it"
                )
            if not (stage / manifest.library).is_file():
                raise ArtifactCorruptError(
                    f"chtypes: {entry.file} names library {manifest.library} but does not "
                    f"contain it"
                )
            self._verify_in_place(stage, expected_sha=entry.library_sha256)

            # Atomic: the fully verified sibling is renamed into place; the
            # previous install, if any, is moved aside first and removed after.
            replaced: Path | None = None
            if install.exists() or install.is_symlink():
                replaced = self.dest / f".{minor}.replaced.{os.getpid()}.{work.name[-6:]}"
                os.rename(install, replaced)
            try:
                os.rename(stage, install)
            except OSError:
                if replaced is not None:
                    os.rename(replaced, install)
                raise
            if replaced is not None:
                shutil.rmtree(replaced, ignore_errors=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        # The installed library is hashed again, in place — everything before
        # this proved the bytes were right somewhere else.
        self._verify_in_place(install, expected_sha=entry.library_sha256)
        self._say(f"installed and verified: {install}")
        self._say(f"    {entry.library} sha256 {entry.library_sha256}")
        self._say(
            f"    ClickHouse {entry.clickhouse_version} — Registry({str(self.dest)!r}) "
            f"now serves {minor}"
        )
        self._record_pin(pin_key, entry)
        return install

    def _record_pin(self, key: str, entry: ReleaseEntry) -> None:
        if self.lock is None or self.frozen:
            return
        pins = self._lock_pins()
        want = {"file": entry.file, "sha256": entry.sha256}
        if pins.get(key) == want:
            return
        pins[key] = want
        write_lock(self.lock, pins)
        self._say(f"pinned {key} in {self.lock}")

    def _installed_matches(self, install: Path, entry: ReleaseEntry) -> bool:
        manifest = read_manifest(install)
        if manifest is None or manifest.library != entry.library:
            return False
        path = install / manifest.library
        if not path.is_file():
            return False
        have = _sha256_file(path)
        if have == entry.library_sha256:
            return True
        self._say(
            f"{path} is present but hashes {have} (the release says {entry.library_sha256})"
            f" — replacing"
        )
        return False

    @staticmethod
    def _verify_in_place(directory: Path, *, expected_sha: str | None) -> None:
        try:
            verify_library(directory)
        except Exception as exc:  # RegistryError; the message names the mismatch
            raise ArtifactCorruptError(str(exc)) from None
        if expected_sha is not None:
            manifest = read_manifest(directory)
            assert manifest is not None
            if manifest.library_sha256.lower() != expected_sha:
                raise ArtifactCorruptError(
                    f"chtypes: {directory / manifest.library} hashes {manifest.library_sha256}, "
                    f"the release says {expected_sha}"
                )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract(tarball: Path, into: Path, name: str) -> None:
    """Unpack a verified tarball with no path traversal, no links, no devices."""
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            members = []
            for member in tar:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                    raise ArtifactCorruptError(
                        f"chtypes: {name} contains an unsafe path {member.name!r}; not unpacking it"
                    )
                if member.isdir() or member.isfile():
                    members.append(member)
                    continue
                raise ArtifactCorruptError(
                    f"chtypes: {name} contains {member.name!r}, which is not a regular file "
                    f"(links and devices are refused); not unpacking it"
                )
            if hasattr(tarfile, "data_filter"):
                tar.extractall(into, members=members, filter="data")
            else:  # pragma: no cover - Python < 3.11.4; members were vetted above
                tar.extractall(into, members=members)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ArtifactCorruptError(f"chtypes: {name} is not a readable tar.gz: {exc}") from exc


# ------------------------------------------------------------- the two calls


def ensure(
    line: str,
    *,
    dest: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    url: str | None = None,
    tag: str | None = None,
    lock: str | os.PathLike[str] | None = None,
    frozen: bool = False,
    force: bool = False,
    offline: bool = False,
    trusted_keys: Iterable[str] | None = None,
    allow_unsigned: bool | None = None,
    progress: Progress | None = None,
) -> Path:
    """Make sure one ClickHouse line is installed and verified; return its directory.

    ``line`` is a minor line (``"25.8"``, resolving to the patch the release
    publishes) or an exact patch (``"25.8.28.1-lts"``, a hard requirement).
    Idempotent: installed-and-verified is a no-op. Options mirror the CLI
    (docs/guides/fetch.md §6): ``dest`` (else ``$CHTYPES_REGISTRY``, else the
    per-user cache), ``platform`` (this host's), ``url`` or ``tag``, ``lock``
    with ``frozen``, ``force``, ``offline``; ``trusted_keys`` and
    ``allow_unsigned`` default to the environment (§4). ``progress`` receives
    human-readable lines (the CLI writes them to stderr); the
    ``chtypes.fetch`` logger gets the same at INFO.

    Raises the `ArtifactError` family: `ArtifactUntrustedError`,
    `ArtifactCorruptError`, `ArtifactPinnedError`, `ArtifactUnpublishedError`,
    `SourceUnreachableError` — each with `.code`; `ValueError` for a bad
    option.
    """
    return Fetcher(
        dest=dest,
        platform=platform,
        url=url,
        tag=tag,
        lock=lock,
        frozen=frozen,
        force=force,
        offline=offline,
        trusted_keys=trusted_keys,
        allow_unsigned=allow_unsigned,
        progress=progress,
    ).ensure(line)


def fetch_lines(
    lines: Sequence[str] = (),
    *,
    all_lines: bool = False,
    dest: str | os.PathLike[str] | None = None,
    platform: str | None = None,
    url: str | None = None,
    tag: str | None = None,
    lock: str | os.PathLike[str] | None = None,
    frozen: bool = False,
    force: bool = False,
    offline: bool = False,
    trusted_keys: Iterable[str] | None = None,
    allow_unsigned: bool | None = None,
    progress: Progress | None = None,
) -> list[Path]:
    """`ensure` for several lines, or with ``all_lines`` every line the release
    publishes for the platform; the release is read once. Returns the
    installed directories in the order installed."""
    if all_lines and lines:
        raise ValueError(
            f"chtypes: --all installs every published line; drop the version argument "
            f"({', '.join(lines)})"
        )
    if not all_lines and not lines:
        raise ValueError("chtypes: a ClickHouse version is required (or --all)")
    fetcher = Fetcher(
        dest=dest,
        platform=platform,
        url=url,
        tag=tag,
        lock=lock,
        frozen=frozen,
        force=force,
        offline=offline,
        trusted_keys=trusted_keys,
        allow_unsigned=allow_unsigned,
        progress=progress,
    )
    if all_lines:
        return fetcher.ensure_all()
    return [fetcher.ensure(line) for line in lines]
