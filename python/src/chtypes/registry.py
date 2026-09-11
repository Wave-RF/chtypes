"""The four objects: `Registry` -> `Library` -> `Schema` -> results.

Registry(dir) -> for_version(v) -> Library -> compile_ddl(ddl) -> Schema
                                      |                            |
                                validate_type(expr)        set_engine / set_ttl
                                                           row(...) / rows(...)
"""

from __future__ import annotations

import json
import os
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import Final

from ._document import parse_batch_document, parse_filter_document, parse_row_document
from ._manifest import (
    Manifest,
    cache_registry_dir,
    host_platform,
    minor_of,
    read_manifest,
    verify_library,
)
from ._native import NativeLibrary
from .errors import (
    ArtifactMissingError,
    ChtypesError,
    RegistryError,
    SchemaError,
    UnsupportedError,
    _error_for,
)
from .fetch import ENV_AUTOFETCH, Fetcher, fetch_destination, registry_search_path
from .results import (
    COMPILE_DECLARED,
    DOC_ALL,
    EXPORT_NONE,
    BatchResult,
    Column,
    DefaultKind,
    FilterResult,
    Format,
    RowResult,
)

__all__ = [
    "DEFAULT_TIMEZONE",
    "ENV_REGISTRY",
    "default_registry_dir",
    "Filter",
    "Library",
    "Manifest",
    "Registry",
    "Schema",
    "Settings",
    "encode_settings",
    "host_platform",
    "minor_of",
    "read_manifest",
    "verify_library",
]

# Where a caller points the binding when it does not pass a directory.
ENV_REGISTRY: Final = "CHTYPES_REGISTRY"


def default_registry_dir() -> str:
    """The per-user artifact cache for this host — ``${XDG_CACHE_HOME:-~/.cache}/
    chtypes/artifacts/<os>-<arch>`` with ``<arch>`` spelled the artifact way
    (``amd64``/``arm64``). Where ``chtypes fetch`` installs, where a core-repo
    build lands, and slot 3 of the registry search path every SDK walks
    (docs/fetch.md §1). A path, not a promise: it need not exist yet."""
    return cache_registry_dir()


# The server timezone assumed for bare DateTime / DateTime64 columns. "UTC" is
# what a stock ClickHouse container uses, and the process environment is
# deliberately NOT consulted: letting the host's TZ leak in changes answers.
DEFAULT_TIMEZONE: Final = "UTC"

SettingValue = str | int | bool
Settings = Mapping[str, SettingValue]


# chs_init bookkeeping: resolved artifact path -> the timezone its image was
# initialized with. Process-wide because the C state it guards is.
_INITIALIZED_IMAGES: dict[str, str] = {}

# Live-wrapper refcount per dlopen'd IMAGE, keyed like everything else on the
# resolved path. `dlopen` refcounts one image per file, so two Registries over
# one artifact directory share the C globals `chs_shutdown` tears into:
# closing the first must be a no-op at the C boundary while the second still
# holds the image, and only the LAST close runs `chs_shutdown`
# (docs/reference/bindings.md §Teardown). Guarded by `_IMAGES_MU`, as is
# `_INITIALIZED_IMAGES`, so two threads constructing Libraries over one
# artifact cannot double-init or double-count. (The lock is cheap: load and close
# paths only, never a row call.)
_IMAGE_REFS: dict[str, int] = {}
_IMAGES_MU = threading.Lock()


def _minor_sort_key(minor: str) -> tuple[int, int, str]:
    """Order minor lines numerically.

    25.10 is a LATER minor than 25.8, so string comparison of minor lines is
    meaningless and must not be used for ordering (docs/reference/bindings.md, "Version
    selection", rule 2).
    """
    parts = minor.split(".")
    try:
        return int(parts[0]), int(parts[1]), minor
    except (IndexError, ValueError):
        return 1 << 30, 1 << 30, minor


def encode_settings(settings: Settings | None) -> str:
    """Encode a settings mapping the way the C ABI requires: values as JSON STRINGS.

    This is not cosmetic. `chtypes_now_epoch_nanos` is a 19-digit nanosecond
    epoch, which does not survive an IEEE double: as a JSON *number* through a
    float it arrives as 1.7e+18 and the setting is SILENTLY IGNORED. An `int` is
    therefore stringified exactly with `str()`, and a `float` is refused rather
    than quietly rounded.

    A `bool` encodes as ``"1"`` / ``"0"`` — the spelling the server's own
    settings parser treats as canonical — never as Python's ``"True"`` text
    (which the server would refuse to parse for a numeric-backed bool
    setting). Checked BEFORE `int`, because `bool` is an `int` subclass and
    `str(True)` is exactly the wrong answer. Spec: docs/reference/bindings.md, "Values a
    binding must accept and reject".
    """
    if not settings:
        return "{}"
    out: dict[str, str] = {}
    for key, value in settings.items():
        if isinstance(value, bool):
            out[key] = "1" if value else "0"
        elif isinstance(value, int):
            out[key] = str(value)  # exact for any width; never through a float
        elif isinstance(value, str):
            out[key] = value
        else:
            raise TypeError(
                f"chtypes: setting {key!r} must be a str, int or bool, not "
                f"{type(value).__name__}: a float cannot carry a 19-digit "
                "nanosecond epoch, and the setting would be silently ignored"
            )
    return json.dumps(out)


def _as_bytes(raw: object, what: str) -> bytes:
    """Row bodies are bytes, never text: binary formats contain NUL bytes and
    text rows can carry invalid UTF-8 on purpose."""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, (bytearray, memoryview)):
        return bytes(raw)
    raise TypeError(
        f"chtypes: {what} must be bytes, not {type(raw).__name__} "
        "(encode it yourself: the wire bytes are the input, not text)"
    )


class Schema:
    """A column list compiled inside one library's ClickHouse.

    Not a `CREATE TABLE`: a ClickHouse column-declaration list, e.g.
    `"a UInt8, b Nullable(String) DEFAULT 'x', c DateTime MATERIALIZED now()"`,
    parsed by ClickHouse's own `ParserColumnDeclarationList` so DEFAULT
    expressions are validated as real SQL. Column-level TTLs belong in this
    string; `set_ttl` is only for the table-level rows TTL.
    """

    __slots__ = (
        "__weakref__",
        "_blocks",
        "_filters",
        "_handle",
        "_library",
        "_mu",
        "columns",
        "ddl",
    )

    def __init__(self, library: Library, handle: int, ddl: str) -> None:
        self._library = library
        self._handle: int | None = handle
        # Every open Filter compiled from this handle, so close() can free
        # them FIRST — the C layer does not refcount, and freeing the schema
        # under a live filter is use-after-free (docs/reference/c-abi.md §Filters,
        # handle lifetime). A WeakSet: an abandoned Filter's own __del__
        # frees its handle, and this set never keeps one alive.
        self._filters: weakref.WeakSet[Filter] = weakref.WeakSet()
        # Every open Block parsed from this handle — the same non-owning
        # rule, the same free-before-schema order (docs/reference/c-abi.md §Blocks).
        self._blocks: weakref.WeakSet[Block] = weakref.WeakSet()
        # ONE handle is single-threaded, by the ABI: "a single chs_schema *
        # MUST NOT be used from two threads at once" (docs/reference/c-abi.md
        # §Thread-safety). The library's readers-writer lock deliberately lets
        # row calls on DISTINCT handles run together — that is the whole point
        # of it being an RWLock — so the per-handle exclusion has to live here,
        # exactly as Go's `LoadedSchema.mu` sits under `Library.mu.RLock()`.
        # Always the OUTER lock of the two: a writer never takes a schema lock,
        # so there is no order to invert.
        self._mu = threading.Lock()
        self.ddl = ddl
        self.columns: tuple[Column, ...] = tuple(
            Column(
                name=name,
                type=type_,
                default_kind=DefaultKind.of(kind),
                default_expr=expr,
                default_is_literal=literal,
            )
            for name, type_, kind, expr, literal in library._native.schema_columns(handle)
        )

    # -- lifetime ----------------------------------------------------------

    def close(self) -> None:
        """Free the underlying `chs_schema` handle. Idempotent.

        Any `Filter` or `Block` still open on this schema is closed FIRST, in
        the same call — the handles-before-schema free order the C layer
        requires, enforced here so no caller ordering can get it backwards.

        Also run by the context manager and (as a safety net, not the
        contract) by `__del__`. Every later call on this schema raises
        `ChtypesError`. Close every `Schema` before closing its `Library`.
        """
        with self._mu:
            for f in list(self._filters):
                f._close_locked()
            for b in list(self._blocks):
                b._close_locked()
            if self._handle is not None:
                self._library._native.schema_free(self._handle)
                self._handle = None

    def __enter__(self) -> Schema:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - a safety net, not the contract
        try:
            self.close()
        except Exception:
            pass

    def _live(self) -> int:
        if self._handle is None:
            raise ChtypesError("chtypes: schema is closed")
        return self._handle

    # -- table-level declarations -------------------------------------------

    def set_engine(
        self, engine: str, order_by: str, *, merge_tree_settings: Settings | None = None
    ) -> None:
        """Declare the engine, so `rows` applies its insert-time semantics.

        `engine` is the SHOW CREATE spelling ("CollapsingMergeTree(sign)") and
        `order_by` the sorting key ("tuple()", "id", "(day, key)"). Raises
        `UnsupportedError` for an engine or sorting key this build does not
        model — never a guess. An artifact built before `chs_schema_engine`
        (engine support was always optional) raises `UnsupportedError` naming
        the missing symbol — never a crash, never a silent fallback.

        `merge_tree_settings` declares the table's MergeTree-namespace settings
        (the `SETTINGS` clause after the engine — `allow_nullable_key` and
        friends, a namespace `DB::Settings` cannot carry). None or `{}` is
        IDENTICAL to declaring no settings, byte for byte. Names are validated
        by the server's own `MergeTreeSettings` object: an unknown name raises
        `SchemaError` with the server's own code 115 (kept visible so a caller
        can tell "bad name" from "not modelled"); a known name declared at a
        NON-default value raises `UnsupportedError` — no MergeTree setting's
        behaviour is modelled yet, and silently ignoring a declared value would
        mean the declared profile is not the profile; declared AT the default
        is inert and accepted.
        """
        with self._mu:
            rc, err = self._library._native.schema_engine(
                self._live(), engine, order_by, encode_settings(merge_tree_settings)
            )
        if rc == 0:
            return
        # The SIGN of rc is the rule. Positive is a real ClickHouse code: the
        # server's own engine validation refused this DDL, it can never exist,
        # and the tenant has to be told — so it is a `SchemaError` carrying the
        # server's code and message (which holds the "Maybe you meant ..."
        # hint). Negative is this library declining to model something (-2), or
        # a guarded exception (-1) — an `UnsupportedError`, which a caller must
        # treat as "validate this cautiously", never as the tenant's fault.
        # `rc > 0` rather than `rc == 115` so a code a future era returns here
        # cannot silently be demoted to a decline.
        if rc > 0:
            raise SchemaError(rc, err)
        raise UnsupportedError(err or "engine not modelled by this build")

    def set_ttl(self, ttl_sql: str) -> None:
        """Declare the table's rows TTL, e.g. "ts + INTERVAL 30 DAY".

        Raises `UnsupportedError` for the forms this build refuses (WHERE /
        GROUP BY TTLs, TO DISK/VOLUME moves, RECOMPRESS, and any clock-reading
        TTL expression, which a merge would evaluate against the server's clock
        at merge time — an instant no preview owns).
        """
        with self._mu:
            rc, err = self._library._native.schema_ttl(self._live(), ttl_sql)
        if rc != 0:
            raise UnsupportedError(err or "TTL form not modelled by this build")

    # -- rows ---------------------------------------------------------------

    def row(self, fmt: Format, raw: bytes, settings: Settings | None = None) -> RowResult:
        """Validate and coerce one row: what would this INSERT do to this value?

        `fmt` is a `Format` code, `raw` the row's wire bytes (bytes, never
        text: binary formats contain NULs and text rows may carry invalid
        UTF-8 on purpose), `settings` an optional per-call settings mapping
        (values cross as strings; see `encode_settings`).

        Returns a `RowResult` — a verdict, never an exception: a row the
        server would refuse comes back `Outcome.REJECTED` with the server's
        own code and message, a row this build declines comes back
        `Outcome.UNSUPPORTED`. Raises only for caller faults: `ChtypesError`
        on a closed schema, `TypeError` for a non-bytes body or a float
        setting value, `UnsupportedError` if the artifact predates `chs_row`.
        """
        with self._mu:
            doc = self._library._native.row(
                self._live(), int(fmt), _as_bytes(raw, "row"), encode_settings(settings)
            )
        return parse_row_document(doc)

    def rows(
        self,
        fmt: Format,
        body: bytes,
        settings: Settings | None = None,
        *,
        export: Format | int | None = None,
        doc_flags: int | None = None,
    ) -> BatchResult:
        """Validate and coerce a whole request body, which may hold many rows.

        This is not `row` in a loop, and must not be implemented as one: row
        separation is format-specific (a quoted CSV field can contain a newline)
        and `input_format_allow_errors_num` / `_ratio` decide whether a bad row
        is skipped or aborts the batch. It is also the unit of the
        volatile-DEFAULT clock guarantee: one batch is one clock instant.

        Same parameters and exception posture as `row`; returns a
        `BatchResult`, whose `transformed` folds in the storage layer's
        verdicts and whose `engine_rows` — when present — is the stored truth.
        A row skipped under `input_format_allow_errors_*` keeps its place in
        `BatchResult.rows` as `Outcome.SKIPPED`, carrying the server's own
        caught error verbatim (2026-08-27).

        **The revision-3 export channel** (docs/reference/c-abi.md §Rows;
        docs/proposals/rows-export.md) — still ONE `chs_rows` call, never a
        second, never re-parsing:

        `export` is None (no bytes — today's path, byte-identical documents)
        or a `Format` this artifact can SERIALIZE — this revision exactly
        `Format.JSON_COMPACT_EACH_ROW`. Any other value answers the whole
        call `Outcome.UNSUPPORTED` and processes nothing — loud, never
        silent, and deliberately not pre-validated here. The bytes come back
        in `BatchResult.payload` with `BatchResult.spans` index-aligned to
        `rows` (`payload[s.off:s.off+s.len]` IS row i's line); emitted-empty
        versus declined is `b""` versus `None` + `export_declined`.

        `doc_flags` is the `DOC_*` bitmask choosing which document GROUPS the
        per-row documents carry (verdicts are always present and not a flag).
        None means `DOC_ALL` when no export is requested — today's full
        document — and 0 (LEAN: verdicts only; `values`, `transformed`,
        `computed`, `substituted`, `unknown_fields` all come back empty) when
        `export` is given, the proposal's export-spelling default. An
        explicit int always wins, over both.
        """
        export_format = EXPORT_NONE if export is None else int(export)
        if doc_flags is None:
            flags = DOC_ALL if export is None else 0
        else:
            flags = int(doc_flags)
        with self._mu:
            doc, payload = self._library._native.rows(
                self._live(),
                int(fmt),
                _as_bytes(body, "body"),
                encode_settings(settings),
                export_format,
                flags,
            )
        return parse_batch_document(doc, payload=payload)

    # -- filters ------------------------------------------------------------

    def compile_filter(self, expr: str, *, params: Settings | None = None) -> Filter:
        """Compile one boolean SQL expression over this schema's PHYSICAL
        columns (ordinary + MATERIALIZED) — `chs_filter_compile`, the same
        TreeRewriter + ExpressionAnalyzer pipeline the CONSTRAINT CHECK path
        runs, so comparison semantics are WHERE-side by construction:
        `x = 256` over UInt8 promotes (false for every row), it never wraps.

        `params` binds `{name:Type}` query parameters (revision 4): a mapping
        of name -> value STRING (the settings convention — ints/bools are
        stringified exactly, floats refused loudly). NEVER hand-escape a
        value into the expression text: substitution is the server's own
        `ReplaceQueryParameterVisitor`, each value is deserialized by the
        DECLARED type's own reader and injected as a typed literal AFTER SQL
        parsing, so injection safety is by construction — a hostile value
        compares as exactly that literal. The compiled handle bakes the
        values in (identity is per (schema, expr, params)); a caller
        compiling filters from tenant-influenced values MUST bound its cache
        (LRU) and its compile rate per principal (docs/reference/c-abi.md §Filters).

        CHOOSE THE BRACE TYPE FOR THE VALUE'S DOMAIN: the declared type's
        own reader WRAPS an out-of-domain integer — `{p:UInt8}` given "256"
        binds 0 and matches every genuine zero (measured, uniform
        24.8-26.7) — while the same constant as a literal PROMOTES
        (`x = 256` is never true). Size the brace type for the
        tenant-supplied domain or validate the value first; malformed
        spellings refuse loudly (457 for "-1"/"+7"/"007" as UInt8, 32 for
        ""). A name bound twice at the C boundary takes the LAST binding —
        the server's own insert_or_assign rule (unreachable through a dict,
        stated for completeness).

        Errors follow rule 12's split: `SchemaError` when ClickHouse itself
        refuses the expression (unknown identifier 47, unknown function —
        and the server's own parameter refusals: an UNBOUND `{name:Type}` is
        456 UNKNOWN_QUERY_PARAMETER "Substitution `name` is not set", a value
        the declared type cannot parse completely is 457 BAD_QUERY_PARAMETER
        — the server's own code and message, verbatim); `UnsupportedError`
        when this build declines — a non-deterministic expression (clock
        reads: `now() > ts`; `rand()`; server-constants; the scan runs AFTER
        substitution, so a value can never smuggle one in). A bound name the
        expression never uses is ignored, as a live server ignores an unused
        `param_*`.

        The Filter references this schema's handle — keep the Schema open for
        the Filter's whole life. The ordering is enforced structurally: the
        Filter holds its Schema, and `Schema.close()` closes open filters
        first. A filter answers for THIS compiled handle: recompile filters
        when the schema is recompiled.
        """
        with self._mu:
            fhandle, code, err = self._library._native.filter_compile(
                self._live(), expr, encode_settings(params)
            )
            if fhandle is None:
                raise _error_for(code, err or f"filter expression refused: {expr!r}")
            f = Filter(self, fhandle, expr)
            self._filters.add(f)
        return f

    def parse_block(self, fmt: Format, body: bytes, settings: Settings | None = None) -> Block:
        """Parse a body ONCE into a `Block` (`chs_block_parse`) — the parse
        half of `Filter.rows`, exported so K filters can evaluate one event
        with no re-parse (`Filter.eval`; docs/reference/c-abi.md §Blocks). Same formats
        and settings contract as `rows` (`settings` is the PARSE-side map:
        format settings, clock keys; evaluation takes none). Volatile
        DEFAULTs resolve against THIS call's clock instant, so
        `filter.eval(schema.parse_block(body))` ≡ `filter.rows(body)` exactly
        when the clock is pinned (`chtypes_now_epoch_nanos`) or the schema
        has no volatile DEFAULT.

        A call-level failure — an unknown setting's 115, an unsplittable
        body, a binary decode fault, the deferred JSONEachRow framing verdict
        — raises (`SchemaError` with ClickHouse's own code, or
        `UnsupportedError` for a decline): a malformed body yields no block
        and no partial answers. Per-row parse failures do NOT raise — they
        are recorded IN the block and answer `Verdict.DECLINE` from every
        filter, with the recorded error.

        The Block references this schema's handle exactly as a Filter does;
        `Schema.close()` closes open blocks first, and the Block holds its
        Schema so the schema cannot be collected under it.
        """
        with self._mu:
            bhandle, code, err = self._library._native.block_parse(
                self._live(), int(fmt), _as_bytes(body, "body"), encode_settings(settings)
            )
            if bhandle is None:
                raise _error_for(code, err or "block parse refused")
            b = Block(self, bhandle)
            self._blocks.add(b)
        return b


class Filter:
    """One boolean SQL expression compiled against a `Schema`'s columns.

    Obtained from `Schema.compile_filter`. Evaluates WHERE-side semantics by
    construction (the CONSTRAINT CHECK pipeline over the vendored comparison
    functions); the verdicts are the four-state `Verdict`, two of which are
    fail-closed non-answers.

    LIFETIME: a `chs_filter` REFERENCES its schema handle — the C layer does
    not refcount. This binding enforces the free order structurally, both
    ways: the Filter holds its `Schema` (a strong reference, so the schema
    cannot be garbage-collected first), and `Schema.close()` closes every
    open Filter before freeing the schema. `close()` is idempotent; the
    context manager and (as a safety net) `__del__` run it too.

    THREADS — the header's rule, verbatim: "one chs_filter must not be used
    from two threads at once, and a chs_filter call is ALSO a use of its
    schema handle" — two filters over ONE schema must not run concurrently
    either. This binding enforces both by taking the schema's own per-handle
    lock for every filter call; distinct schemas remain fully concurrent.

    ENFORCEMENT GATE: nothing may enforce read-side security on this surface
    until the WHERE-truth rig gates green (zero over-admit, zero over-hide);
    until then it is a shadow/replay surface (docs/reference/c-abi.md §Filters).
    """

    __slots__ = ("__weakref__", "_handle", "_schema", "expr")

    def __init__(self, schema: Schema, handle: int, expr: str) -> None:
        self._schema = schema
        self._handle: int | None = handle
        #: The expression text as compiled, for logging and cache keys.
        self.expr = expr

    def rows(self, fmt: Format, body: bytes, settings: Settings | None = None) -> FilterResult:
        """Evaluate the filter over a body of rows (`chs_filter_rows`) — the
        same formats and settings contract as `Schema.rows`, one C call.

        Rows are evaluated INDEPENDENTLY (there is no INSERT to abort):
        `input_format_allow_errors_*` does not apply, a bad text row declines
        (`Verdict.DECLINE`, itemized in `errors`) and the tail resyncs so
        verdict indexes keep matching input rows, and volatile DEFAULTs
        resolve against one clock instant per call. The call-level verdict is
        `FilterResult.outcome`; raises only for a closed filter, a non-bytes
        body, or an unreadable document.
        """
        with self._schema._mu:
            if self._handle is None:
                raise ChtypesError("chtypes: filter is closed")
            doc = self._schema._library._native.filter_rows(
                self._handle, int(fmt), _as_bytes(body, "body"), encode_settings(settings)
            )
        return parse_filter_document(doc)

    def eval(self, block: Block) -> FilterResult:
        """Evaluate this filter over an already-parsed `Block`
        (`chs_filter_eval`) — the SAME result document `rows` returns: same
        `FilterResult` fields, same verdicts, same `errors` rule (a row the
        parse recorded as unparseable answers `Verdict.DECLINE` with the
        recorded error). Evaluation is a pure function of (filter, block): no
        settings, and the block is neither consumed nor mutated, so one block
        can be evaluated by K filters sequentially with no re-parse.

        Filter and block MUST come from the SAME schema handle: a mismatched
        pair answers a REJECTED result (code 1002) — the C layer's loud
        refusal, never undefined behaviour. A pair from two different
        `Library` objects raises `ChtypesError`: no handle ever crosses a
        dlopen'd image boundary. Raises only for that, a closed filter or
        block, or an unreadable document.
        """
        fs, bs = self._schema, block._schema
        if fs._library is not bs._library:
            raise ChtypesError(
                "chtypes: filter and block come from different libraries "
                f"(ClickHouse {fs._library.version} vs {bs._library.version})"
            )
        # An eval is a use of BOTH handles. Same schema: one lock. Two schemas
        # (same library — the C layer answers its rejected-1002 document):
        # both locks, in a fixed global order so crossed evals cannot
        # deadlock.
        locks = [fs._mu] if fs is bs else sorted((fs._mu, bs._mu), key=id)
        with ExitStack() as stack:
            for mu in locks:
                stack.enter_context(mu)
            if self._handle is None:
                raise ChtypesError("chtypes: filter is closed")
            if block._handle is None:
                raise ChtypesError("chtypes: block is closed")
            doc = fs._library._native.filter_eval(self._handle, block._handle)
        return parse_filter_document(doc)

    def close(self) -> None:
        """Free the underlying `chs_filter` handle. Idempotent — including
        after `Schema.close()` already freed it (filters first, then the
        schema: the C-required order)."""
        with self._schema._mu:
            self._close_locked()

    def _close_locked(self) -> None:
        """Free the filter handle. Caller holds the schema's lock."""
        if self._handle is not None:
            self._schema._library._native.filter_free(self._handle)
            self._handle = None

    def __enter__(self) -> Filter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - a safety net, not the contract
        try:
            self.close()
        except Exception:
            pass


class Block:
    """One body, parsed ONCE under one schema handle and one clock instant.

    Obtained from `Schema.parse_block`; evaluated by `Filter.eval`. The
    parse-once/eval-many twin of `Filter.rows` (docs/reference/c-abi.md §Blocks): the
    live-SSE call shape is K filters x 1 event, and the block sheds the
    re-parse. Per-row parse failures are recorded IN the block (those rows
    answer `Verdict.DECLINE` from every filter); a call-level failure raised
    in `parse_block` instead — a malformed body yields no Block at all.

    LIFETIME: a `chs_block` REFERENCES its schema handle exactly as a filter
    does — no copy, no refcount. Enforced structurally, both ways: the Block
    holds its `Schema` (a strong reference), and `Schema.close()` closes
    every open Block first. A block may be evaluated by MANY filters,
    sequentially; evaluation does not consume or mutate it. `close()` is
    idempotent; the context manager and (as a safety net) `__del__` run it.

    THREADS: one block must not be used from two threads at once, and an
    eval is a use of BOTH handles — `Filter.eval` takes the schema's own
    per-handle lock, which also serialises K filters over one block.
    """

    __slots__ = ("__weakref__", "_handle", "_schema")

    def __init__(self, schema: Schema, handle: int) -> None:
        self._schema = schema
        self._handle: int | None = handle

    def close(self) -> None:
        """Free the underlying `chs_block` handle. Idempotent — including
        after `Schema.close()` already freed it (blocks first, then the
        schema: the C-required order)."""
        with self._schema._mu:
            self._close_locked()

    def _close_locked(self) -> None:
        """Free the block handle. Caller holds the schema's lock."""
        if self._handle is not None:
            self._schema._library._native.block_free(self._handle)
            self._handle = None

    def __enter__(self) -> Block:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - a safety net, not the contract
        try:
            self.close()
        except Exception:
            pass


class Library:
    """One dlopen'd vendored ClickHouse build, which names itself."""

    __slots__ = (
        "_closed",
        "_image_key",
        "_native",
        "abi_revision",
        "manifest",
        "minor",
        "path",
        "version",
    )

    def __init__(self, path: str, manifest: Manifest, timezone: str = DEFAULT_TIMEZONE) -> None:
        self.path = path
        self.manifest = manifest
        self._closed = False
        self._native = NativeLibrary(path)
        # The library names itself; nothing is inferred from the directory or
        # the file name.
        self.version = self._native.clickhouse_version()
        self.minor = minor_of(self.version)
        #: The chs_* ABI revision this ARTIFACT was built from, or 0 when it
        #: predates `chs_abi_revision`. A mismatching nonzero revision already
        #: raised in `NativeLibrary`, so a `Library` that exists carries either
        #: `chtypes.ABI_REVISION` or 0.
        self.abi_revision = self._native.abi_revision
        if not self.version:
            raise RegistryError(f"chtypes: {path} reported an empty ClickHouse version")
        # The one class of corruption a hash cannot catch: the right bytes in the
        # wrong directory.
        if manifest.clickhouse_version and manifest.clickhouse_version != self.version:
            raise RegistryError(
                f"chtypes: {path} reports ClickHouse {self.version} but its manifest says "
                f"{manifest.clickhouse_version}"
            )
        # Each library keeps its own DateLUT and its own refuse-list. An absent
        # or empty unsafe_families.txt is a valid EMPTY LIST, not a missing file:
        # every artifact in the current matrix ships one.
        try:
            unsafe = (Path(path).parent / "unsafe_families.txt").read_text().strip()
        except OSError:
            unsafe = manifest.unsafe_families.strip()
        # chs_init AT MOST ONCE per artifact image. ctypes.CDLL and dlopen
        # refcount one image per file, so a second Registry over the same
        # artifact shares its C globals — re-running chs_init would rebuild
        # the refuse-list and re-set DateLUT under the first instance's live
        # readers (the Go binding's loadedLibs map and the TS binding's
        # realpath-keyed map guard the same hazard). Keyed on the resolved
        # path; a second init asking for a DIFFERENT timezone is refused
        # rather than silently re-timezoning live libraries.
        key = str(Path(path).resolve())
        self._image_key = key
        with _IMAGES_MU:
            prev_tz = _INITIALIZED_IMAGES.get(key)
            if prev_tz is None:
                rc, err = self._native.init(timezone, unsafe)
                if rc != 0:
                    # err carries ClickHouse's own message — the one reachable
                    # failure is an unknown timezone, and a bare code cannot say
                    # which name was rejected.
                    detail = f": {err}" if err else ""
                    raise RegistryError(f"chtypes: chs_init failed for {path}: [{rc}]{detail}")
                _INITIALIZED_IMAGES[key] = timezone
            elif prev_tz != timezone:
                raise RegistryError(
                    f"chtypes: {path} is already initialized with timezone {prev_tz!r}; "
                    f"cannot re-initialize with {timezone!r} (one image per path — "
                    f"chs_init runs at most once)"
                )
            # Fully constructed: this wrapper now holds a reference on the
            # image, and `close()` releases exactly one (see close).
            _IMAGE_REFS[key] = _IMAGE_REFS.get(key, 0) + 1

    def __repr__(self) -> str:
        return f"<chtypes.Library {self.version} at {self.path}>"

    # -- types --------------------------------------------------------------

    def validate_type(self, type_expr: str) -> str:
        """Parse and canonicalise one type expression.

        The canonical spelling is the library's own (`Decimal(18, 4)`, a space
        after each comma; `Variant` members sorted) and must be passed through
        verbatim — never whitespace-normalised by the caller.

        Returns the canonical type expression. Raises `SchemaError` with
        ClickHouse's own code and message when the expression is refused
        (e.g. code 50, `Unknown data type family`). Note what this alone
        cannot answer: a DEFAULT can rewrite the declared type
        (`x Int64 DEFAULT NULL` compiles to `Nullable(Int64)`), which is why
        the schema-aware `compile_ddl` exists.
        """
        canonical, code, err = self._native.validate_type(type_expr)
        if not canonical:
            # The one construction funnel (`_error_for`) keys on the SIGN:
            # a positive code is the server's own refusal (`SchemaError`), a
            # negative one — `-2` for an unsafe family this build refuses to
            # construct, `-1` for a guarded exception — is this library
            # declining (`UnsupportedError`), never a rejection the product
            # invented (docs/reference/bindings.md rule 12).
            raise _error_for(code, err or f"invalid type expression: {type_expr!r}")
        return canonical

    def reference_type(self, type_expr: str) -> str:
        """Diagnostic: the widened reference type this build compares against.

        `UInt8 -> Int256`, `DateTime -> DateTime64(0, 'UTC')`, `UUID -> String`.
        The empty string for types with no wider type (`String`, `Float64`). Not
        needed to function — the reference parse already happens inside
        `chs_row` — but it makes transformation findings explainable.
        Raises `UnsupportedError` when the artifact predates
        `chs_reference_type`.
        """
        return self._native.reference_type(type_expr)

    def registered_families(self) -> list[str]:
        """Every type family in this build's own runtime registry (139 entries
        on the 25.8 artifact) — the answer to "does this build track upstream
        type families without a table to maintain?" (it does; rebasing onto a
        new release picks up new families automatically).

        Part of the three-question introspection surface every SDK exposes
        (docs/reference/bindings.md §Introspection). Raises `UnsupportedError` when the
        artifact predates `chs_registered_families`.
        """
        return [line for line in self._native.registered_families().split("\n") if line]

    def function_flags(self) -> str:
        """TSV audit of every registered function's volatility, VERBATIM: one
        function per line, six tab-separated fields (`name`, `deterministic`,
        `deterministic_in_query`, `server_constant`, `stateful`,
        `resolver_error_code`) — ClickHouse's own answers off this build's own
        registry, the input to the statelessness gate
        (`lib/tools/gen_function_flags.py`).

        Part of the three-question introspection surface every SDK exposes
        (docs/reference/bindings.md §Introspection). Raises `UnsupportedError` when the
        artifact predates `chs_function_flags`.
        """
        return self._native.function_flags()

    # -- schemas ------------------------------------------------------------

    @property
    def has_compile_settings(self) -> bool:
        """Whether this artifact exports the consolidated `chs_schema_compile`.

        True for every artifact this repo builds — `chs_schema_compile` is a
        mandatory export, so a `Library` cannot exist without it. The probe
        stays as a defensive check for the rare third-party-built artifact a
        `Registry` might someday load; the conformance driver's
        `caps.compile_settings` handshake keys on it.
        """
        return self._native.has("chs_schema_compile")

    def compile_ddl(
        self,
        ddl: str,
        *,
        settings: Settings | None = None,
        mode: int = COMPILE_DECLARED,
    ) -> Schema:
        """Compile a column-declaration list against this library's ClickHouse.

        With `settings`, compile under a DECLARED settings profile — the
        settings the deployment's server runs, fixed into the handle at compile
        exactly as a real CREATE TABLE fixes them into the table (docs/reference/c-abi.md
        §Compile-time vs per-call settings). Values cross the boundary as
        strings, like every settings map on this ABI.

        None or `{}` is IDENTICAL to declaring no profile, byte for byte.
        `mode` defaults to `COMPILE_DECLARED` (0), the only defined mode: every
        declared name takes the caller's value, every undeclared setting keeps
        the library's permissive compile base — a partial profile can admit a
        schema the server might refuse, and can never fabricate a rejection;
        any other mode raises `UnsupportedError` (refused loudly, `-2`,
        reserved for a future mode — the underlying library still sees it, it
        just always declines). An unknown setting name raises `SchemaError`
        with the server's own code 115 (`chtypes_*` keys are per-call keys, not
        ClickHouse settings, and land on the same 115). Per-call settings still
        govern row parsing, and only row parsing.

        The one compile-shape setting modelled today is `flatten_nested`: at
        "0" a `Nested(a,b)` column compiles to ONE `Array(Tuple(...))` column
        named as declared, exactly as the server's CREATE does under that
        setting; every downstream shape (`columns`, name lookup, positional
        arity, the RowBinary wire) follows the compiled shape.
        """
        handle, code, err = self._native.schema_compile(ddl, encode_settings(settings), mode)
        if handle is None:
            # A DEFAULT this build refuses to evaluate, one that exceeded an
            # admission budget, or a mode this build does not define, is
            # refused HERE — and it is a decline, not a rejection a real server
            # would have made. `_error_for` keys on the SIGN of the code, so a
            # future negative sentinel can never become "a SchemaError with a
            # negative code" (docs/reference/bindings.md rule 12).
            raise _error_for(code, err or f"invalid column list: {ddl!r}")
        return Schema(self, handle, ddl)

    # -- process-wide settings ---------------------------------------------

    def set_default_settings(self, settings: Settings | None) -> None:
        """Seed the settings every later call on THIS library starts from.

        Several type families are gated at column-creation time by settings that
        are properties of the server the table lives on, not of the row
        (`allow_suspicious_low_cardinality_types`, `allow_experimental_json_type`,
        ...). A gateway knows those once. The admission budgets
        (`chtypes_default_eval_memory_bytes`, `chtypes_default_eval_wall_nanos`)
        are settable ONLY here.
        Anything a per-call settings map sets still wins.

        Prefer ``compile_ddl(..., settings=...)`` for a gate that belongs to a
        particular tenant's table: a gate declared in the compile profile binds
        where a real server binds it — once, at CREATE — and then outranks the
        per-call map for that handle (measured on live 25.10.7.6 and 26.7.3.19;
        docs/reference/c-abi.md, "Server-level type gates"). This process-wide seed stays
        the right channel only for gateway-uniform policy.

        Raises `ChtypesError` on refusal, with ClickHouse's own message —
        including its did-you-mean hint naming the setting on an unknown name
        (code 115), previously unreachable from this call. A name in the
        library's reserved namespace that is not one of the six documented
        `chtypes_*` keys is an unknown name like any other and lands on the
        same 115; the whole payload is then refused and nothing is committed.

        **This is the one genuinely dangerous call on the ABI**, and it is
        serialised: it takes the loaded image's lock EXCLUSIVELY, so no thread
        can be inside `row` / `rows` / `compile_ddl` / `validate_type` while it
        runs. `chs_set_default_settings` replaces a process-global that the row
        path reads by reference, and ctypes releases the GIL for the whole
        duration of a foreign call — the GIL is not the exclusion, the lock is.
        See `_native._RWLock`.
        """
        rc, err = self._native.set_default_settings(encode_settings(settings))
        if rc != 0:
            detail = f": {err}" if err else ""
            raise ChtypesError(f"chtypes: chs_set_default_settings failed: [{rc}]{detail}")

    def close(self) -> None:
        """Release this wrapper's hold on the loaded image; the LAST close
        joins the DEFAULT evaluator's background threads (`chs_shutdown`).

        `dlopen` refcounts ONE image per file, so two `Registry` instances
        over one artifact share the C globals `chs_shutdown` tears into. This
        call is therefore refcounted on the resolved path (docs/reference/bindings.md
        §Teardown, 2026-08-26): closing one wrapper while another still holds
        the image is a no-op at the C boundary — the survivor's evaluator
        threads keep running — and only the final close runs `chs_shutdown`.
        Before this, closing one Registry joined the evaluator threads under
        the other's live libraries.

        `chs_init` registers `chs_shutdown` with `atexit`, so an ordinary
        process needs no call. An explicit close is required before `dlclose`,
        when the host controls its own teardown order, or from a test that
        must not depend on `atexit` — the thread it joins is still looping
        otherwise, and the process hangs after printing everything, which
        reads as a harness bug rather than a teardown bug. Idempotent per
        wrapper (a second `close()` on the same `Library` releases nothing
        more). Close every `Schema` from this library first.

        When it does reach the C boundary it takes the loaded image's lock
        EXCLUSIVELY, like `set_default_settings` — `chs_shutdown` is the
        other call `docs/reference/c-abi.md` §Thread-safety requires be serialised
        against everything else.
        """
        with _IMAGES_MU:
            if self._closed:
                return
            self._closed = True
            live = _IMAGE_REFS.get(self._image_key, 0) - 1
            _IMAGE_REFS[self._image_key] = max(live, 0)
            if live > 0:
                return
        self._native.shutdown()


class Registry:
    """Every artifact on the search path, dispatched by ClickHouse version.

    The search path (docs/fetch.md §1) is walked in order — the explicit
    ``directory``, ``$CHTYPES_REGISTRY``, the per-user cache, the system
    locations — and a line is served from the **first** directory that holds
    it, so a stale directory can never shadow a good one and an empty one
    is simply skipped. Each directory's layout is one subdirectory per
    ClickHouse minor line holding a `manifest.json` whose `library` field
    names the shared object; that field is the loader's only source of truth
    for the file name (the Linux artifacts in this very tree still ship the
    historical `libchtypes_s1.so`).

    Loading is lazy and per line: constructing a `Registry` reads manifests
    (cheap) and `dlopen`s nothing; `for_version` loads the one line asked
    for, once. A line missing from every directory is `ArtifactMissingError`
    (§7) — unless lazy fetch is on (``autofetch=True`` or
    ``CHTYPES_AUTOFETCH=1``), in which case `ensure` runs first, under one
    process-wide lock per line so concurrent opens fetch once. Off by
    default: a production process must not begin a 250 MB download inside a
    request.

    `directory` is where fetch writes — the first of the explicit path,
    ``$CHTYPES_REGISTRY`` and the cache; `search_path` is every directory
    consulted, in order.
    """

    __slots__ = (
        "_autofetch",
        "_by_id",
        "_closed",
        "_index",
        "_mu",
        "_timezone",
        "_verify_hashes",
        "directory",
        "search_path",
    )

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        timezone: str = DEFAULT_TIMEZONE,
        verify_hashes: bool = False,
        autofetch: bool | None = None,
    ) -> None:
        self.search_path: tuple[Path, ...] = registry_search_path(directory)
        self.directory: Path = fetch_destination(directory)
        self._timezone = timezone
        self._verify_hashes = verify_hashes
        self._autofetch = (
            autofetch
            if autofetch is not None
            else os.environ.get(ENV_AUTOFETCH, "").strip().lower() in ("1", "true", "yes", "on")
        )
        self._by_id: dict[str, Library] = {}
        self._index: dict[str, Path] = {}
        self._closed = False
        self._mu = threading.RLock()
        if directory is not None:
            # An explicit directory that exists but cannot be read is a
            # configuration mistake, named now. One that does not exist yet
            # is fine: fetch creates it.
            try:
                Path(directory).iterdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RegistryError(f"chtypes: cannot read registry {directory}: {exc}") from exc
        self._scan()

    def __repr__(self) -> str:
        return f"<chtypes.Registry {self.directory} versions={self.versions()}>"

    # ----------------------------------------------------------- discovery

    def _scan(self) -> None:
        """Index every line on the search path: minor -> the FIRST directory
        holding it. Reads manifests only; loads nothing."""
        index: dict[str, Path] = {}
        for root in self.search_path:
            try:
                entries = sorted(root.iterdir())
            except OSError:
                continue  # absent, or unreadable: not a registry
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                manifest = read_manifest(entry)
                if manifest is None:
                    continue  # not a version directory; scratch dirs are allowed
                # The manifest's own claim, verified against the library at
                # load; the directory name is only the last resort.
                minor = manifest.clickhouse_minor or minor_of(manifest.clickhouse_version)
                index.setdefault(minor or entry.name, entry)
        self._index = index

    def _load(self, minor: str, entry: Path) -> Library:
        manifest = read_manifest(entry)
        if manifest is None:
            raise RegistryError(f"chtypes: {entry} no longer holds a usable manifest.json")
        if self._verify_hashes:
            verify_library(entry)
        # A directory that has a manifest and does not load is broken, not
        # absent: this is an error, naming the path.
        library = Library(str(entry / manifest.library), manifest, self._timezone)
        if library.minor != minor:
            library.close()
            raise RegistryError(
                f"chtypes: {entry} is indexed as ClickHouse {minor} but its library reports "
                f"{library.version}"
            )
        # Indexed under BOTH its exact version and its minor line: docker
        # tags drift, and an exact-match-only lookup silently loses a whole
        # version column.
        self._by_id[library.version] = library
        self._by_id[library.minor] = library
        return library

    def _autofetch_line(self, version: str) -> None:
        key = (str(self.directory), minor_of(version))
        with _autofetch_lock(key):
            if key in _AUTOFETCHED:
                return
            Fetcher(dest=self.directory).ensure(version)
            _AUTOFETCHED.add(key)

    # ----------------------------------------------------------- resolution

    def versions(self) -> tuple[str, ...]:
        """The ClickHouse minor lines this registry can answer for, in release order."""
        minors = {lib.minor for lib in self._by_id.values()} | set(self._index)
        return tuple(sorted(minors, key=_minor_sort_key))

    def libraries(self) -> tuple[Library, ...]:
        """One entry per line, loaded, in release order."""
        with self._mu:
            for minor in list(self._index):
                if minor not in self._by_id:
                    self._load(minor, self._index[minor])
        unique = {id(lib): lib for lib in self._by_id.values()}
        return tuple(sorted(unique.values(), key=lambda lib: _minor_sort_key(lib.minor)))

    def for_version(self, version: str) -> Library:
        """Resolve a minor line ("25.8") or an exact patch ("25.8.28.1-lts").

        A drifted patch resolves to its minor line, deliberately. Failure is
        `ArtifactMissingError` naming every directory searched, never a
        fallback to the nearest line: answering 26.7 semantics from a 25.8
        artifact is a lie, and the rigs score silent wrongness hardest.
        """
        if self._closed:
            raise ChtypesError("chtypes: registry is closed")
        if not version:
            raise RegistryError("chtypes: an empty version does not mean 'pick one'")
        minor = minor_of(version)
        with self._mu:
            library = self._by_id.get(version) or self._by_id.get(minor)
            if library is not None:
                return library
            if minor not in self._index:
                self._scan()  # installed since construction, by fetch or by hand
            if minor not in self._index and self._autofetch:
                self._autofetch_line(version)
                self._scan()
            if minor not in self._index:
                raise ArtifactMissingError(minor, host_platform(), self.search_path)
            return self._load(minor, self._index[minor])

    def __getitem__(self, version: str) -> Library:
        """`registry["25.8"]` — sugar for `for_version`, same resolution rules."""
        return self.for_version(version)

    def __contains__(self, version: str) -> bool:
        """Whether `for_version(version)` would resolve without fetching."""
        return (
            version in self._by_id
            or minor_of(version) in self._by_id
            or (minor_of(version) in self._index)
        )

    def __iter__(self) -> Iterator[Library]:
        return iter(self.libraries())

    def __len__(self) -> int:
        return len(self.versions())

    def close(self) -> None:
        """`chs_shutdown` every loaded library. Close every `Schema` first."""
        with self._mu:
            unique = {id(lib): lib for lib in self._by_id.values()}
            for library in unique.values():
                library.close()
            self._closed = True

    def __enter__(self) -> Registry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# Lazy fetch runs ONCE per process per (destination, line), whatever the
# number of Registry instances or threads that open it concurrently
# (docs/fetch.md §6): the lock serializes the opens, the memo keeps a line
# whose fetch succeeded but whose load then failed from being re-downloaded
# on every open.
_AUTOFETCHED: set[tuple[str, str]] = set()
_AUTOFETCH_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_AUTOFETCH_MU = threading.Lock()


def _autofetch_lock(key: tuple[str, str]) -> threading.Lock:
    with _AUTOFETCH_MU:
        return _AUTOFETCH_LOCKS.setdefault(key, threading.Lock())
