"""Parsing the `chs_row` / `chs_rows` result documents into the typed results.

Every key is treated as optional-with-a-default. That is not defensive
programming, it is the contract: a rejected row's document omits
`unknown_fields`, `unsupported_settings` and `computed` entirely and carries
`"cols":[]` (docs/reference/c-abi.md "Keys may be absent").
"""

from __future__ import annotations

from dataclasses import dataclass

from ._rawjson import RawArray, RawNumber, RawObject, decode_document
from .errors import ChtypesError
from .results import (
    BatchResult,
    Computed,
    FilterOutcome,
    FilterResult,
    FilterRowError,
    Outcome,
    RowResult,
    Span,
    Substitution,
    Transform,
    Value,
    Verdict,
)
from .transform import classify

__all__ = ["ColDoc", "parse_batch_document", "parse_filter_document", "parse_row_document"]


@dataclass(frozen=True, slots=True)
class ColDoc:
    """One `cols[]` entry, with `stored` / `ref` kept as raw JSON text.

    They are deliberately NOT decoded into Python's str and int: a String column
    holds arbitrary bytes and ClickHouse integers go to 2**256 (see _rawjson).
    """

    name: str
    type: str
    base: str
    src: str
    input: str
    ref_type: str
    stored_raw: str | None
    ref_raw: str | None
    nullable: bool
    poison: bool
    null_input: bool
    dup_dropped: bool
    # The C layer found this leaf numeric/temporal but had no reference-ladder
    # entry (audit F4): the classifier treats a visible change as lossy, never
    # `reformat`. Absent from every artifact whose build gate ran, so the
    # default is the ordinary state.
    ref_unclassified: bool = False

    @property
    def stored(self) -> str:
        # A JSON null here is a real stored value (a Nullable column holding
        # null), not the absence of one. Only the poison case has no renderable
        # value, and that is flagged separately.
        if self.stored_raw is None or self.poison:
            return ""
        return self.stored_raw

    @property
    def ref(self) -> str:
        if self.ref_raw is None or self.ref_raw == "null":
            return ""
        return self.ref_raw


def _obj(doc: object, where: str) -> RawObject:
    # RawObject, not dict: every document here comes from `decode_document`, and
    # the raw spans it carries are the only way a value's text reaches a caller
    # unaltered. Accepting a plain dict would silently re-open defect classes B
    # and C (see _rawjson).
    if not isinstance(doc, RawObject):
        raise ChtypesError(f"chtypes: bad {where} document: not a JSON object")
    return doc


def _text(doc: RawObject, key: str) -> str:
    value = doc.get(key)
    return value if isinstance(value, str) else ""


def _flag(doc: RawObject, key: str) -> bool:
    return doc.get(key) is True


def _count(doc: RawObject, key: str) -> int:
    value = doc.get(key)
    if isinstance(value, RawNumber):
        try:
            return int(value.text)
        except ValueError:
            return 0
    return 0


def _raw(doc: RawObject, key: str) -> str | None:
    """The library's own JSON text for a value, or None when the key is absent.

    The single choke point for every reported value, and it hands back the
    document's bytes rather than a re-rendering of the decoded form. Rendering
    would drop a duplicate `Map` key and respell `\\u000B` as `\\u000b` — a
    change the tenant would read as ClickHouse's, when it is this binding's.
    """
    return doc.raw(key)


def _text_tuple(doc: RawObject, key: str) -> tuple[str, ...]:
    value = doc.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def _col_doc(raw: object) -> ColDoc:
    doc = _obj(raw, "column")
    return ColDoc(
        name=_text(doc, "name"),
        type=_text(doc, "type"),
        base=_text(doc, "base"),
        src=_text(doc, "src"),
        input=_text(doc, "input"),
        ref_type=_text(doc, "ref_type"),
        stored_raw=_raw(doc, "stored"),
        ref_raw=_raw(doc, "ref"),
        nullable=_flag(doc, "nullable"),
        poison=_flag(doc, "poison"),
        null_input=_flag(doc, "null_input"),
        dup_dropped=_flag(doc, "dup_dropped"),
        ref_unclassified=_flag(doc, "ref_unclassified"),
    )


def _row_result(doc: RawObject) -> RowResult:
    outcome = Outcome.of(_text(doc, "outcome"))
    unsupported_settings = _text_tuple(doc, "unsupported_settings")
    # A declined setting must never become a scored answer.
    if unsupported_settings and outcome is not Outcome.REJECTED:
        outcome = Outcome.UNSUPPORTED

    values: list[Value] = []
    transformed: list[Transform] = []
    substituted: list[Substitution] = []
    cols = doc.get("cols")
    for entry in cols if isinstance(cols, list) else ():
        col = _col_doc(entry)
        # MATERIALIZED / ALIAS / EPHEMERAL are never read from an input row and
        # are not part of the stored row a subscriber would SELECT.
        if col.src == "skipped":
            continue
        values.append(
            Value(
                column=col.name,
                text=col.stored,
                null=col.stored_raw == "null" and not col.poison,
                source=col.src,
            )
        )
        if col.src == "default_substituted":
            substituted.append(Substitution(column=col.name, expr=col.input, text=col.stored))
        transformed.extend(classify(col))

    computed: list[Computed] = []
    entries = doc.get("computed")
    for entry in entries if isinstance(entries, list) else ():
        comp = _obj(entry, "computed")
        computed.append(
            Computed(
                column=_text(comp, "name"),
                kind=_text(comp, "kind"),
                text=_raw(comp, "stored") or "",
            )
        )

    return RowResult(
        outcome=outcome,
        err_code=_count(doc, "code"),
        err_msg=_text(doc, "err"),
        values=tuple(values),
        transformed=tuple(transformed),
        unknown_fields=_text_tuple(doc, "unknown_fields"),
        unsupported_settings=unsupported_settings,
        substituted=tuple(substituted),
        computed=tuple(computed),
    )


def parse_row_document(raw: bytes) -> RowResult:
    return _row_result(_obj(decode_document(raw), "row"))


def parse_batch_document(raw: bytes, payload: bytes | None = None) -> BatchResult:
    doc = _obj(decode_document(raw), "batch")

    rows: list[RowResult] = []
    transformed: list[Transform] = []
    entries = doc.get("rows")
    for index, entry in enumerate(entries if isinstance(entries, list) else ()):
        row = _row_result(_obj(entry, "row"))
        # A batch of ten rows with one bad value is useless to a tenant without
        # the index, so every folded transform carries its row.
        indexed = tuple(
            Transform(column=t.column, input=t.input, stored=t.stored, reason=t.reason, row=index)
            for t in row.transformed
        )
        rows.append(
            RowResult(
                outcome=row.outcome,
                err_code=row.err_code,
                err_msg=row.err_msg,
                values=row.values,
                transformed=indexed,
                unknown_fields=row.unknown_fields,
                unsupported_settings=row.unsupported_settings,
                substituted=row.substituted,
                computed=row.computed,
            )
        )
        transformed.extend(indexed)

    # The storage layer's own verdicts on rows the type layer accepted. A
    # TTL-expired row is `accepted` per row and NOT STORED per batch: dropping
    # these tells a tenant a row was stored that the table deletes at merge time.
    storage = doc.get("storage_transforms")
    for entry in storage if isinstance(storage, list) else ():
        st = _obj(entry, "storage transform")
        transformed.append(
            Transform(
                column=_text(st, "column"),
                input="",
                stored=_raw(st, "stored") or "",
                reason=_text(st, "reason"),
                row=_count(st, "row"),
            )
        )

    engine_rows: tuple[str, ...] | None = None
    preview = doc.get("engine_rows")
    if isinstance(preview, RawArray):
        # When present this — not `rows` — is the stored truth. Kept as the
        # library's own JSON text, one entry per stored row, for the same reason
        # `stored` is.
        engine_rows = tuple(preview.raw(index) for index in range(len(preview)))

    # The export channel's addressing (docs/reference/c-abi.md §Rows): `row_spans` is
    # present exactly when bytes were emitted, `export_declined` exactly when
    # an export was requested and withheld. Both keys absent is both the
    # no-export case and the call-level-verdict case — optional-with-default,
    # like every key in these documents.
    spans: tuple[Span, ...] | None = None
    raw_spans = doc.get("row_spans")
    if isinstance(raw_spans, list):
        span_objs = (_obj(entry, "row span") for entry in raw_spans)
        spans = tuple(Span(off=_count(sp, "off"), len=_count(sp, "len")) for sp in span_objs)

    return BatchResult(
        outcome=Outcome.of(_text(doc, "outcome")),
        err_code=_count(doc, "code"),
        err_msg=_text(doc, "err"),
        rows=tuple(rows),
        rows_read=_count(doc, "rows_read"),
        rows_skipped=_count(doc, "rows_skipped"),
        transformed=tuple(transformed),
        engine_rows=engine_rows,
        payload=payload,
        spans=spans,
        export_declined=_text(doc, "export_declined"),
    )


def parse_filter_document(raw: bytes) -> FilterResult:
    """Parse a `chs_filter_rows` document into a `FilterResult`.

    The call outcome degrades unknown spellings to UNSUPPORTED and unknown
    verdict characters to DECLINE — both the fail-closed arm, never an
    invented answer (docs/reference/bindings.md §Revision 3).
    """
    doc = _obj(decode_document(raw), "filter")
    outcome = FilterOutcome.of(_text(doc, "outcome"))
    verdicts: tuple[Verdict, ...] = ()
    errors: tuple[FilterRowError, ...] = ()
    if outcome is FilterOutcome.OK:
        verdicts = tuple(Verdict.of(char) for char in _text(doc, "verdicts"))
        raw_errors = doc.get("errors")
        if isinstance(raw_errors, list):
            errors = tuple(
                FilterRowError(
                    row=_count(_obj(entry, "filter error"), "row"),
                    code=_count(_obj(entry, "filter error"), "code"),
                    err=_text(_obj(entry, "filter error"), "err"),
                )
                for entry in raw_errors
            )
    return FilterResult(
        outcome=outcome,
        err_code=_count(doc, "code"),
        err_msg=_text(doc, "err"),
        rows_read=_count(doc, "rows_read"),
        unsupported_settings=_text_tuple(doc, "unsupported_settings"),
        verdicts=verdicts,
        errors=errors,
    )
