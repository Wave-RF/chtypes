"""`Library`, `Schema`, `Filter` and `Block`: the public v1 operations over the
generated layer (docs/reference/bindings-v1.md sections 2 and 3).

Every public call makes exactly one ABI call, plus the generated read-out of its
buffers and error. Nothing here contains a ClickHouse rule: no scalar,
comparison, coercion, timestamp, zone, quoting or classification logic. A
settings value is a string and is written verbatim; a document is decoded one
to one (`_decode`); a `UsageError` is raised before the call for a closed object.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType

from . import _decode, _setup
from ._abi1 import _decls, _loader
from ._abi1._vocab import EXPORT_NONE, DocFlags, Format
from ._guard import CloseGuard
from ._input import (
    BytesIn,
    Settings,
    body_bytes,
    columns_json,
    settings_json,
    string_map_json,
    to_bytes,
)
from .results import (
    BatchResult,
    BuildInfo,
    Discovery,
    ErrorCodeTable,
    FilterResult,
    RowResult,
    SchemaDescription,
)

__all__ = ["Block", "Filter", "Library", "Schema", "open_unverified"]

# One image per file, process-wide: keyed on the realpath's device and inode, so
# two registries, two spellings or a hardlink of one artifact share one image
# and one Library. Guarded by the setup lock, which an open holds throughout.
_IMAGES: dict[tuple[int, int], Library] = {}


def _image_key(path: str) -> tuple[int, int]:
    st = os.stat(os.path.realpath(path))
    return (st.st_dev, st.st_ino)


def open_image(path: str, predicate: object, resolved: object | None) -> Library:
    """Open (or find) the image at `path`, verified against `predicate`.

    A new image runs loader steps 1 to 7 once, under the setup in effect, and
    latches the setup when step 7 completes (`_setup.latch`). A failed load is
    never cached, so the next open of the same image runs every step again; the
    caller settles a failure with `_setup.open_failed`, whatever failed. An
    image already open is still checked against this signed statement (steps 1,
    4 and 5): a mismatch refuses this request and leaves the image open for the
    requests it did match.
    """
    with _setup.LOCK:
        state = _setup.effective()
        key = _image_key(path)
        existing = _IMAGES.get(key)
        if existing is not None:
            _loader.recheck(existing._load, path, predicate)  # type: ignore[arg-type]
            return existing
        load = _loader.open(
            path,
            predicate,  # type: ignore[arg-type]
            timezone=state.zone_bytes(),
            defaults=state.defaults_json(),
        )
        _setup.latch()
        library = Library(load, resolved)
        _IMAGES[key] = library
        return library


def open_unverified(path: str | os.PathLike[str], *, allow: bool = False) -> Library:
    """Open a local build with no signed statement, for the artifact producer's own
    suites over an unpublished library. It refuses with a `UsageError` unless
    `allow` is passed AND `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1` is set, warns once
    per path, and skips loader steps 1 and 5. It runs step 7 under the process
    setup like any open. It is not reachable through a registry, and its
    `Library` has no `resolved`. Like any open, a failure clears the setup
    record while no image has completed step 7 (`_setup.open_failed`)."""
    spelled = os.fspath(path)
    began = _setup.generation()
    try:
        _loader.check_unverified_allowed(spelled, allow)
        with _setup.LOCK:
            state = _setup.effective()
            existing = _IMAGES.get(_image_key(spelled))
            if existing is not None:
                return existing
            load = _loader.open_unverified(
                spelled,
                allow=allow,
                timezone=state.zone_bytes(),
                defaults=state.defaults_json(),
            )
            _setup.latch()
            library = Library(load, None)
            _IMAGES[_image_key(spelled)] = library
            return library
    except Exception:
        _setup.open_failed(began)
        raise


@contextlib.contextmanager
def _hold(*guards: CloseGuard) -> Iterator[None]:
    """Enter several close guards for one call; each is released on the way out,
    including when a later one refuses."""
    with contextlib.ExitStack() as stack:
        for guard in guards:
            stack.enter_context(guard)
        yield


class _Handle:
    """Shared plumbing of `Schema`, `Filter` and `Block`: the close guard, the
    context manager, and the finalizer (the generated handle frees itself when
    nothing refers to it)."""

    __slots__ = ("__weakref__", "_guard", "_handle", "_library")

    _what = "object"

    def __init__(self, library: Library, handle: _decls.Handle) -> None:
        self._library = library
        self._handle = handle
        self._guard = CloseGuard(self._what)

    def close(self) -> None:
        """Release this object's reference. Idempotent, any order is safe: the
        library counts references, so a filter or block outlives a closed
        schema. Waits for calls already inside this object."""
        self._guard.close(self._handle.close)

    def __enter__(self):  # noqa: ANN204 - returns the concrete subclass
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Block(_Handle):
    """A body parsed once, for evaluating many filters over it. It holds a counted
    reference to its schema inside the library."""

    __slots__ = ()
    _what = "Block"


class Filter(_Handle):
    """A compiled boolean SQL expression over a schema's columns, with its query
    parameters bound. It holds a counted reference to its schema, so closing the
    schema first is legal."""

    __slots__ = ()
    _what = "Filter"

    def rows(
        self,
        format: Format | int,
        body: bytes,
        *,
        settings: Settings | None = None,
        session_timezone: str | None = None,
    ) -> FilterResult:
        """Evaluate over a body. `settings` and `session_timezone` are the body's
        PARSE settings only: the filter's own zone was fixed when it compiled."""
        payload = body_bytes(body)
        settings_bytes = settings_json(settings, session_timezone)
        with _hold(self._guard):
            raw = self._library._api.filter_eval_body(
                self._handle, int(format), payload, settings_bytes
            )
        return _decode.decode_filter_result(raw)

    def eval(self, block: Block) -> FilterResult:
        """Evaluate over a parsed block. It takes no settings: the filter brings
        its zone, and the block brought its parse zone when it was parsed."""
        if not isinstance(block, Block):
            raise TypeError(f"block must be a Block, not {type(block).__name__}")
        with _hold(self._guard, block._guard):
            raw = self._library._api.filter_eval_block(self._handle, block._handle)
        return _decode.decode_filter_result(raw)


class Schema(_Handle):
    """A compiled table: exactly one `CREATE TABLE` statement, immutable once
    created. Safe to share across threads; calls run in parallel (ctypes releases
    the GIL), and `close` waits for the calls already inside."""

    __slots__ = ()
    _what = "Schema"

    def describe(self) -> SchemaDescription:
        with _hold(self._guard):
            raw = self._library._api.schema_describe(self._handle)
        return _decode.decode_schema_description(raw)

    def row(
        self,
        format: Format | int,
        body: bytes,
        *,
        settings: Settings | None = None,
        session_timezone: str | None = None,
        columns: Sequence[BytesIn] | None = None,
    ) -> RowResult:
        """One row, as a server's INSERT would validate and coerce it."""
        payload = body_bytes(body)
        settings_bytes = settings_json(settings, session_timezone)
        columns_bytes = columns_json(columns)
        with _hold(self._guard):
            raw = self._library._api.preview_row(
                self._handle, int(format), payload, settings_bytes, columns_bytes
            )
        return _decode.decode_row_document(raw)

    def rows(
        self,
        format: Format | int,
        body: bytes,
        *,
        settings: Settings | None = None,
        session_timezone: str | None = None,
        columns: Sequence[BytesIn] | None = None,
        row_filter: Filter | None = None,
        export: Format | int | None = None,
        doc_flags: DocFlags = DocFlags.ALL,
    ) -> BatchResult:
        """A whole body. `export` asks for the accepted rows serialized in that
        format (`payload`, and `spans`); to insert a body whose rows carry a
        `default_generated` column, insert `payload`, never the original input."""
        payload = body_bytes(body)
        settings_bytes = settings_json(settings, session_timezone)
        columns_bytes = columns_json(columns)
        if row_filter is not None and not isinstance(row_filter, Filter):
            raise TypeError(f"row_filter must be a Filter, not {type(row_filter).__name__}")
        guards = [self._guard] + ([] if row_filter is None else [row_filter._guard])
        with _hold(*guards):
            raw, exported = self._library._api.preview_batch(
                self._handle,
                int(format),
                payload,
                settings_bytes,
                columns_bytes,
                None if row_filter is None else row_filter._handle,
                EXPORT_NONE if export is None else int(export),
                int(doc_flags),
            )
        return _decode.decode_batch(raw, exported if export is not None else None)

    def compile_filter(
        self,
        expr: BytesIn,
        *,
        params: Settings | None = None,
        settings: Settings | None = None,
        session_timezone: str | None = None,
    ) -> Filter:
        """Compile a boolean expression over this schema's columns. The filter's
        zone is its own, fixed here: its WHERE runs in the session this call's
        `session_timezone` (or `settings`) names, on every evaluation."""
        expr_bytes = to_bytes(expr, "expr")
        params_bytes = string_map_json(params, "params")
        settings_bytes = settings_json(settings, session_timezone)
        with _hold(self._guard):
            handle = self._library._api.filter_create(
                self._handle, expr_bytes, params_bytes, settings_bytes
            )
        return Filter(self._library, handle)

    def parse_block(
        self,
        format: Format | int,
        body: bytes,
        *,
        settings: Settings | None = None,
        session_timezone: str | None = None,
        columns: Sequence[BytesIn] | None = None,
    ) -> Block:
        """Parse a body once, for evaluating many filters over it."""
        payload = body_bytes(body)
        settings_bytes = settings_json(settings, session_timezone)
        columns_bytes = columns_json(columns)
        with _hold(self._guard):
            handle = self._library._api.block_create(
                self._handle, int(format), payload, settings_bytes, columns_bytes
            )
        return Block(self._library, handle)


class Library:
    """One loaded ClickHouse build. Never unloaded, never closed; safe for
    concurrent use. Construct one through `Registry.for_version`, or
    `open_unverified` for a local build."""

    def __init__(self, load: _loader.LoadResult, resolved: object | None) -> None:
        self._load = load
        self._api = load.api
        self._resolved = resolved
        self._build_info = _decode.build_info(load.raw, load.build_info, load.path)
        self._path = Path(load.path)
        self._error_codes: ErrorCodeTable | None = None
        self._error_codes_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Library(version={self.version!r}, path={str(self._path)!r})"

    @property
    def build_info(self) -> BuildInfo:
        """What the library is: `chs_build_info`, read once by the loader."""
        return self._build_info

    @property
    def version(self) -> str:
        """The four-part ClickHouse version (`clickhouse_version`), no channel."""
        return self._build_info.clickhouse_version

    @property
    def minor(self) -> str:
        """The release line (`clickhouse_minor`)."""
        return self._build_info.clickhouse_minor

    @property
    def path(self) -> Path:
        return self._path

    @property
    def resolved(self):  # noqa: ANN201 - the fetch layer's own record type
        """The fetch record that first opened this image; None when unverified."""
        return self._resolved

    def validate_type(self, type_expr: BytesIn) -> bytes:
        """Canonicalize a type."""
        return self._api.type_validate(to_bytes(type_expr, "type_expr"))

    def quote_identifier(self, name: BytesIn) -> bytes:
        """Quote an identifier, always."""
        return self._api.back_quote(to_bytes(name, "name"))

    def quote_identifier_if_needed(self, name: BytesIn) -> bytes:
        """Quote an identifier only if it needs it."""
        return self._api.back_quote_if_needed(to_bytes(name, "name"))

    def quote_literal(self, text: BytesIn) -> bytes:
        """Quote a string literal."""
        return self._api.quote_string(to_bytes(text, "text"))

    def error_codes(self) -> ErrorCodeTable:
        """This build's error-code table. Built on the first call and kept for the
        library's life on success only."""
        with self._error_codes_lock:
            if self._error_codes is None:
                self._error_codes = _decode.decode_error_codes(self._api.error_codes())
            return self._error_codes

    def discover_query(self) -> bytes:
        """The query a caller runs against its server to read a table's
        `system.columns` rows (two query parameters, `{database:String}` and
        `{table:String}`). The caller binds them with its own client."""
        return self._api.discover_query()

    def discover_columns(self, rows: bytes) -> Discovery:
        """Read a server's answer to `discover_query()`."""
        return _decode.decode_discovery(self._api.discover_columns(body_bytes(rows, "rows")))

    def live_handles(self) -> dict[str, int]:
        """Live handle counts per kind in this image (a diagnostic)."""
        return _decode.decode_live_handles(self._api.live_handles())

    def compile_table(
        self,
        create_table: BytesIn,
        *,
        settings: Settings | None = None,
        session_timezone: str | None = None,
    ) -> Schema:
        """Compile exactly one `CREATE TABLE` statement. `settings` is the profile;
        a compiled type always takes the image zone, never the profile's."""
        statement = to_bytes(create_table, "create_table")
        settings_bytes = settings_json(settings, session_timezone)
        handle = self._api.schema_create(statement, settings_bytes)
        return Schema(self, handle)
