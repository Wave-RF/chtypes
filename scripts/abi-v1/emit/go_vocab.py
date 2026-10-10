"""The Go public vocabularies, generated from spec/abi-v1/abi.json.

docs/reference/bindings-v1.md section 3 ("The generated vocabularies"): every
vocabulary of the description is generated into every binding, with the
description's numbers and spellings, and no binding keeps a copy of its own.
For Go this emitter owns ONE file, go/chtypes/vocab_gen.go, in package
chtypes:

  Format, ExportNone      chs_format (int32) and CHS_EXPORT_NONE; `ChName()`
                          is the enum's own `ch_name` field
  DocFlags                the CHS_DOC_* constants
  Status                  chs_status (int32); `String()` is the C constant's
                          name, and `statusOfName` reads one back
  Reason                  transform_reason, with its `lossy` fact; a value the
                          table does not list takes the fallback's fact
  Source                  value_src, with its `is_stored` fact
  Outcome                 row_outcome and batch_outcome (one type, the union
                          of both); an unknown value reads as the fallback
  FilterOutcome           filter_outcome, with the same fallback rule
  Verdict                 filter_verdict, with `Answered()` the description's
                          own `answered` fact, and the same fallback rule
  DefaultKind             default_kind
  MergeReason             merge_reason (ABI v2): what a merge would do to a row
                          of the part an INSERT writes, the batch document's
                          at_merge reason
  DeclinedTier            declined_tier (ABI v2): the WHERE-settings tier of a
                          setting this build's filters decline,
                          schema_description's filter_declined_settings
  DeclinedLayer           declined_layer (ABI v2): the settings layer such a
                          setting comes from (defaults, server or schema)

The identifier of a constant is derived from its value's spelling by the one
mechanical rule below (`_pascal`, a hyphen read as a word boundary like an
underscore: `predicate-unflipped` is `PredicateUnflipped`), except for the four verdict letters, whose
identifiers cannot be derived from a single letter and are NAMED in
`_VERDICT_NAMES`: that table is naming, never a vocabulary, and the emitter
refuses a verdict value it has no name for.

ABI V2 (rule r3, spec/abi-v2/docs.md): every vocabulary type above is its raw
representation (an int32 or a string), so a value the description does not
list IS that vocabulary's unknown(n) member: it carries the raw value, and
every type gains `Known()`, false for exactly those values. A reader keeps the
value and goes on decoding; it never fails the document, and a fallback
(outcomes, verdicts, reasons) no longer replaces the value: it only names whose
facts unknown(n) reports, so an unknown outcome is never Accepted and an unknown
verdict is never Answered. Status.String() spells an unlisted status
"unknown(<n>)". discover_query_param gains its type (DiscoverQueryParam), and
`describedVocabulary` (unexported, for the r3 tests) builds a value of any
described enum from a raw spelling, so a test reaches every enum the
description defines without a hand-kept list. ABI v1's file is unchanged.

GOFMT-CLEAN BY CONSTRUCTION, like emit/go.py: every declaration is a separate
single-statement const, no struct and no map literal is written, and a switch's
case labels are never aligned by gofmt, so no tabwriter alignment arises.
"""

from __future__ import annotations

from . import Output, banner

BINDING = "go"  # runs for the ONE major spec/binding-majors.json gives go (emit/__init__.py)
MAJORS = (1, 2)

VOCAB_GEN = "go/chtypes/vocab_gen.go"

TAB = "\t"

# Words whose Go spelling is not the plain title case of the value's word.
_WORDS = {"ok": "OK", "OK": "OK", "uuid": "UUID", "ip": "IP", "ttl": "TTL"}

# The identifiers of filter_verdict's single-letter values (see the module
# docstring: naming, not vocabulary).
_VERDICT_NAMES = {"t": "True", "f": "False", "e": "Error", "d": "Decline"}


class VocabError(Exception):
    pass


def _pascal_word(word: str) -> str:
    if word in _WORDS:
        return _WORDS[word]
    return word[:1].upper() + word[1:].lower() if word else word


def _pascal(text: str, sep: str = "_") -> str:
    return "".join(_pascal_word(w) for w in text.split(sep) if w)


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _doc(text: str) -> list[str]:
    return [f"// {line}" if line else "//" for line in text.splitlines()]


def _known_method(type_name: str, idents: list[str], what: str, recv: str = "x") -> list[str]:
    """ABI v2: `func (x T) Known() bool`, true for exactly the described values (r3)."""
    out = _doc(
        f"Known reports whether the description lists the {what}. A value it does not list is the\n"
        "vocabulary's unknown(n) member, carrying the raw value (rule r3): a reader keeps it and decodes on."
    )
    out.append(f"func ({recv} {type_name}) Known() bool {{")
    out.append(f"{TAB}switch {recv} {{")
    out.append(f"{TAB}case " + ", ".join(idents) + ":")
    out.append(f"{TAB}{TAB}return true")
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return false")
    out.append("}")
    out.append("")
    return out


def _status(model) -> list[str]:
    enum = model.enums["chs_status"]
    out: list[str] = []
    out += _doc(
        "Status is chs_status: the call status every error carries (D3), with the description's numbers.\n"
        "Compare with the constants below, never with a literal."
    )
    out.append("type Status int32")
    out.append("")
    for v in enum.values:
        ident = "Status" + _pascal(v.name[len("CHS_") :])
        out += _doc(f"{ident} is {v.name}.")
        out.append(f"const {ident} Status = {v.value}")
        out.append("")
    if model.major >= 2:
        out += _doc("String is the chs_status constant's own name (CHS_REJECTED, ...), or unknown(<n>) for a value the description does not list (rule r3).")
    else:
        out += _doc("String is the chs_status constant's own name (CHS_REJECTED, ...), or CHS_STATUS_<n> for a value the description does not list.")
    out.append("func (s Status) String() string {")
    out.append(f"{TAB}switch s {{")
    for v in enum.values:
        out.append(f"{TAB}case Status{_pascal(v.name[len('CHS_'):])}:")
        out.append(f"{TAB}{TAB}return {_q(v.name)}")
    out.append(f"{TAB}}}")
    if model.major >= 2:
        out.append(f'{TAB}return "unknown(" + strconv.Itoa(int(s)) + ")"')
    else:
        out.append(f'{TAB}return "CHS_STATUS_" + strconv.Itoa(int(s))')
    out.append("}")
    out.append("")
    if model.major >= 2:
        out += _known_method("Status", [f"Status{_pascal(v.name[len('CHS_'):])}" for v in enum.values], "status", "s")
    out += _doc("statusOfName reads a chs_status constant's name back into a Status.")
    out.append("func statusOfName(name string) (Status, bool) {")
    out.append(f"{TAB}switch name {{")
    for v in enum.values:
        out.append(f"{TAB}case {_q(v.name)}:")
        out.append(f"{TAB}{TAB}return Status{_pascal(v.name[len('CHS_'):])}, true")
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return 0, false")
    out.append("}")
    out.append("")
    return out


def _format(model) -> list[str]:
    enum = model.enums["chs_format"]
    out: list[str] = []
    out += _doc(
        "Format is chs_format: a body or export format, with the description's numbers. Each carries\n"
        "ClickHouse's own name (ChName), which is how a build's capabilities list a format."
    )
    out.append("type Format int32")
    out.append("")
    for v in enum.values:
        ch_name = v.fields["ch_name"]
        out += _doc(f"{ch_name} is {v.name}.")
        out.append(f"const {ch_name} Format = {v.value}")
        out.append("")
    for name, const in model.constants.items():
        if name == "CHS_EXPORT_NONE":
            out += _doc("ExportNone is CHS_EXPORT_NONE: no export was asked for.")
            out.append(f"const ExportNone Format = {const.value}")
            out.append("")
    if model.major >= 2:
        out += _known_method("Format", [v.fields["ch_name"] for v in enum.values], "format", "f")
    out += _doc(
        "ChName is ClickHouse's own name for the format, the enum's ch_name field (bindings-v1.md section 3,\n"
        "spelled as section 4 spells CallError.ChName)."
    )
    out.append("func (f Format) ChName() string {")
    out.append(f"{TAB}switch f {{")
    for v in enum.values:
        ch_name = v.fields["ch_name"]
        out.append(f"{TAB}case {ch_name}:")
        out.append(f"{TAB}{TAB}return {_q(ch_name)}")
    out.append(f"{TAB}}}")
    out.append(f'{TAB}return ""')
    out.append("}")
    out.append("")
    return out


def _doc_flags(model) -> list[str]:
    out: list[str] = []
    out += _doc("DocFlags is the set of document groups a rows call asks for: the CHS_DOC_* constants.")
    out.append("type DocFlags uint32")
    out.append("")
    for name, const in model.constants.items():
        if name.startswith("CHS_DOC_"):
            ident = "Doc" + _pascal(name[len("CHS_DOC_") :])
            out += _doc(f"{ident} is {name}.")
            out.append(f"const {ident} DocFlags = {const.value}")
            out.append("")
        elif name not in ("CHS_EXPORT_NONE", "CHS_ABI_REVISION_TOMBSTONE"):
            raise VocabError(f"go_vocab.py: constant {name} has no Go spelling; teach emit/go_vocab.py about it")
    return out


def _string_type(type_name: str, doc: str, values: list[tuple[str, str]]) -> list[str]:
    """`type T string` plus one const per (identifier, value)."""
    out: list[str] = []
    out += _doc(doc)
    out.append(f"type {type_name} string")
    out.append("")
    for ident, value in values:
        out += _doc(f"{ident} is the {type_name} value {_q(value)}.")
        out.append(f"const {ident} {type_name} = {_q(value)}")
        out.append("")
    return out


def _bool_method(type_name: str, method: str, doc: str, enum, field: str, fallback_value: str | None) -> list[str]:
    """`func (x T) Method() bool`: a switch over the values whose fact is true
    when the fallback's is not (and vice versa), so the default arm is the
    fallback's fact."""
    ident_of = lambda v: f"{_PREFIX[enum.name]}{_value_ident(enum.name, v.value)}"  # noqa: E731
    fb = None
    if fallback_value is not None:
        fb = next(v for v in enum.values if v.value == fallback_value).fields[field]
    default = bool(fb) if fb is not None else False
    out: list[str] = []
    out += _doc(doc)
    out.append(f"func (x {type_name}) {method}() bool {{")
    out.append(f"{TAB}switch x {{")
    for v in enum.values:
        if bool(v.fields[field]) != default:
            out.append(f"{TAB}case {ident_of(v)}:")
            out.append(f"{TAB}{TAB}return {'true' if v.fields[field] else 'false'}")
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return {'true' if default else 'false'}")
    out.append("}")
    out.append("")
    return out


# Each string vocabulary's Go identifier prefix.
_PREFIX = {
    "transform_reason": "Reason",
    "value_src": "Source",
    "row_outcome": "",
    "batch_outcome": "",
    "filter_outcome": "Filter",
    "filter_verdict": "Verdict",
    "default_kind": "Kind",
    "merge_reason": "Merge",
    "declined_tier": "Tier",
    "declined_layer": "Layer",
}


def _value_ident(vocab: str, value: str) -> str:
    if vocab == "filter_verdict":
        if value not in _VERDICT_NAMES:
            raise VocabError(f"go_vocab.py: filter_verdict value {value!r} has no name in _VERDICT_NAMES")
        return _VERDICT_NAMES[value]
    if vocab == "default_kind" and value == "":
        return "None"
    # A hyphen (declined_tier's `predicate-unflipped`) is a word boundary, as
    # an underscore is; no ABI v1 value carries one, so its file is unchanged.
    return _pascal((value.lower() if vocab == "default_kind" else value).replace("-", "_"))


def _reason(model) -> list[str]:
    enum = model.enums["transform_reason"]
    values = [(f"Reason{_value_ident(enum.name, v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "Reason",
        "Reason is transform_reason: why a stored value differs from its input. The spelling is the description's.",
        values,
    )
    out += _bool_method(
        "Reason",
        "Lossy",
        "Lossy is the description's own lossy fact for the reason; a reason the table does not list takes the\n"
        "fallback's fact (the description names value_changed).",
        enum,
        "lossy",
        enum.fallback,
    )
    if model.major >= 2:
        out += _known_method("Reason", [i for i, _ in values], "reason")
    return out


def _source(model) -> list[str]:
    enum = model.enums["value_src"]
    values = [(f"Source{_value_ident(enum.name, v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "Source",
        "Source is value_src: where a column's value came from. The spelling is the description's.",
        values,
    )
    out += _bool_method(
        "Source",
        "IsStored",
        "IsStored is the description's own is_stored fact for the source: whether the value is stored."
        + (
            "\nThe description does not yet say what an unknown(n) source reports (rule r3, \"Facts without a\n"
            "fallback\"); until it does, IsStored is false for one."
            if model.major >= 2
            else ""
        ),
        enum,
        "is_stored",
        None,
    )
    if model.major >= 2:
        out += _known_method("Source", [i for i, _ in values], "source")
        return out
    out += _doc("known reports whether the description lists the source; the vocabulary has no fallback.")
    out.append("func (x Source) known() bool {")
    out.append(f"{TAB}switch x {{")
    out.append(f"{TAB}case " + ", ".join(i for i, _ in values) + ":")
    out.append(f"{TAB}{TAB}return true")
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return false")
    out.append("}")
    out.append("")
    return out


def _outcome(model) -> list[str]:
    row, batch = model.enums["row_outcome"], model.enums["batch_outcome"]
    union: list[str] = []
    for enum in (row, batch):
        for v in enum.values:
            if v.value not in union:
                union.append(v.value)
    values = [(_pascal(v), v) for v in union]
    out = _string_type(
        "Outcome",
        "Outcome is row_outcome and batch_outcome: the final verdict the library gives a row or a batch.\n"
        "A batch never carries Skipped. The spelling is the description's.",
        values,
    )
    if model.major >= 2:
        out += _known_method("Outcome", [i for i, _ in values], "outcome")
        for enum, fn in ((row, "parseRowOutcome"), (batch, "parseBatchOutcome")):
            out += _doc(
                f"{fn} reads a {enum.name} value. One the description does not list is kept as its unknown(n)\n"
                f"(rule r3); the fallback, {_q(enum.fallback)}, only says how it is answered: never as accepted."
            )
            out.append(f"func {fn}(s string) Outcome {{ return Outcome(s) }}")
            out.append("")
        return out
    for enum, fn in ((row, "parseRowOutcome"), (batch, "parseBatchOutcome")):
        out += _doc(f"{fn} reads a {enum.name} value; one the description does not list reads as its fallback, {_q(enum.fallback)}.")
        out.append(f"func {fn}(s string) Outcome {{")
        out.append(f"{TAB}switch Outcome(s) {{")
        out.append(f"{TAB}case " + ", ".join(_pascal(v.value) for v in enum.values) + ":")
        out.append(f"{TAB}{TAB}return Outcome(s)")
        out.append(f"{TAB}}}")
        out.append(f"{TAB}return {_pascal(enum.fallback)}")
        out.append("}")
        out.append("")
    return out


def _filter_outcome(model) -> list[str]:
    enum = model.enums["filter_outcome"]
    values = [(f"Filter{_value_ident(enum.name, v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "FilterOutcome",
        "FilterOutcome is filter_outcome: the outcome of a filter evaluation. The spelling is the description's.",
        values,
    )
    if model.major >= 2:
        out += _known_method("FilterOutcome", [i for i, _ in values], "filter outcome")
        out += _doc(
            "parseFilterOutcome reads a filter_outcome value. One the description does not list is kept as its\n"
            f"unknown(n) (rule r3); the fallback, {_q(enum.fallback)}, only says how it is answered: never as ok."
        )
        out.append("func parseFilterOutcome(s string) FilterOutcome { return FilterOutcome(s) }")
        out.append("")
        return out
    out += _doc(f"parseFilterOutcome reads a filter_outcome value; one the description does not list reads as its fallback, {_q(enum.fallback)}.")
    out.append("func parseFilterOutcome(s string) FilterOutcome {")
    out.append(f"{TAB}switch FilterOutcome(s) {{")
    out.append(f"{TAB}case " + ", ".join(i for i, _ in values) + ":")
    out.append(f"{TAB}{TAB}return FilterOutcome(s)")
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return Filter{_pascal(enum.fallback)}")
    out.append("}")
    out.append("")
    return out


def _verdict(model) -> list[str]:
    enum = model.enums["filter_verdict"]
    values = [(f"Verdict{_value_ident(enum.name, v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "Verdict",
        "Verdict is filter_verdict: one row's answer to a filter. The spelling is the description's.",
        values,
    )
    out += _bool_method(
        "Verdict",
        "Answered",
        "Answered is the description's own answered fact for the verdict: whether it is an answer at all.\n"
        "A caller enforcing visibility fails closed on a verdict that is not.",
        enum,
        "answered",
        enum.fallback,
    )
    if model.major >= 2:
        out += _known_method("Verdict", [i for i, _ in values], "verdict")
        out += _doc(
            "parseVerdict reads one verdict character. One the description does not list is kept as its\n"
            f"unknown(n) (rule r3), and Answered reports its fallback's fact ({_q(enum.fallback)}): never an answer."
        )
        out.append("func parseVerdict(s string) Verdict { return Verdict(s) }")
        out.append("")
        return out
    out += _doc(f"parseVerdict reads one verdict character; one the description does not list reads as its fallback, {_q(enum.fallback)}.")
    out.append("func parseVerdict(s string) Verdict {")
    out.append(f"{TAB}switch Verdict(s) {{")
    out.append(f"{TAB}case " + ", ".join(i for i, _ in values) + ":")
    out.append(f"{TAB}{TAB}return Verdict(s)")
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return Verdict{_VERDICT_NAMES[enum.fallback]}")
    out.append("}")
    out.append("")
    return out


def _default_kind(model) -> list[str]:
    enum = model.enums["default_kind"]
    values = [(f"Kind{_value_ident(enum.name, v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "DefaultKind",
        "DefaultKind is default_kind: how a column's default expression applies. The spelling is the description's;\n"
        "KindNone is the empty value.",
        values,
    )
    if model.major >= 2:
        out += _known_method("DefaultKind", [i for i, _ in values], "kind")
        return out
    out += _doc("known reports whether the description lists the kind; the vocabulary has no fallback.")
    out.append("func (x DefaultKind) known() bool {")
    out.append(f"{TAB}switch x {{")
    out.append(f"{TAB}case " + ", ".join(i for i, _ in values) + ":")
    out.append(f"{TAB}{TAB}return true")
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return false")
    out.append("}")
    out.append("")
    return out


def _merge_reason(model) -> list[str]:
    """ABI v2: merge_reason, the batch document's at_merge reason (r3: its unknown(n) too)."""
    enum = model.enums["merge_reason"]
    values = [(f"Merge{_value_ident(enum.name, v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "MergeReason",
        "MergeReason is merge_reason: what a merge would do to one row of the part an INSERT writes, as a\n"
        "batch's AtMerge reports it. The spelling is the description's.",
        values,
    )
    out += _known_method("MergeReason", [i for i, _ in values], "reason")
    return out


def _declined_tier(model) -> list[str]:
    """ABI v2: declined_tier, the tier of a filter_declined_settings entry (r3: its unknown(n) too)."""
    enum = model.enums["declined_tier"]
    values = [(f"Tier{_value_ident(enum.name, v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "DeclinedTier",
        "DeclinedTier is declined_tier: the WHERE-settings tier of a setting a settings layer sets and this\n"
        "build's filters do not honor in a WHERE, as a DeclinedSetting reports it. The spelling is the\n"
        "description's, which is the WHERE-settings lists' own.",
        values,
    )
    out += _known_method("DeclinedTier", [i for i, _ in values], "tier")
    return out


def _declined_layer(model) -> list[str]:
    """ABI v2: declined_layer, the settings layer of a filter_declined_settings entry (r3: its unknown(n) too)."""
    enum = model.enums["declined_layer"]
    values = [(f"Layer{_value_ident(enum.name, v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "DeclinedLayer",
        "DeclinedLayer is declined_layer: the settings layer a DeclinedSetting comes from, the highest one\n"
        "that sets the name (defaults, the server profile, or the schema's own settings). The spelling is\n"
        "the description's.",
        values,
    )
    out += _known_method("DeclinedLayer", [i for i, _ in values], "layer")
    return out


def _discover_query_param(model) -> list[str]:
    """ABI v2: discover_query_param's type (r3: every enum has its unknown(n))."""
    enum = model.enums["discover_query_param"]
    values = [(f"QueryParam{_pascal(v.value)}", v.value) for v in enum.values]
    out = _string_type(
        "DiscoverQueryParam",
        "DiscoverQueryParam is discover_query_param: a query parameter of the SQL DiscoverQuery returns,\n"
        "which the caller binds when it runs the query. The spelling is the description's.",
        values,
    )
    out += _known_method("DiscoverQueryParam", [i for i, _ in values], "parameter")
    return out


# Each described enum's Go type, and how a raw spelling becomes one, for
# describedVocabulary (ABI v2). A described enum missing here fails generation.
_GO_VOCAB = {
    "chs_status": ("Status", "int"),
    "chs_format": ("Format", "int"),
    "transform_reason": ("Reason", "string"),
    "value_src": ("Source", "string"),
    "row_outcome": ("Outcome", "string"),
    "batch_outcome": ("Outcome", "string"),
    "filter_outcome": ("FilterOutcome", "string"),
    "filter_verdict": ("Verdict", "string"),
    "discover_query_param": ("DiscoverQueryParam", "string"),
    "default_kind": ("DefaultKind", "string"),
    "merge_reason": ("MergeReason", "string"),
    "declined_tier": ("DeclinedTier", "string"),
    "declined_layer": ("DeclinedLayer", "string"),
}


def _described(model) -> list[str]:
    """ABI v2: describedVocabulary, the r3 tests' way to reach every enum the
    description defines, generated from it so no list is kept by hand."""
    names = list(model.enums)
    missing = [n for n in names if n not in _GO_VOCAB]
    if missing:
        raise VocabError(f"go_vocab.py: enum(s) {missing} have no Go type; teach emit/go_vocab.py about them")
    out: list[str] = []
    out += _doc("describedVocabularyNames is every enum the description defines, in its own order.")
    out.append("func describedVocabularyNames() []string {")
    out.append(f"{TAB}return []string{{" + ", ".join(_q(n) for n in names) + "}")
    out.append("}")
    out.append("")
    out += _doc(
        "describedVocabulary builds the value of the enum `name` whose raw spelling is raw (an integer for an\n"
        "int32 enum), and reports its Known() and its raw spelling read back. ok is false for a name the\n"
        "description does not define."
    )
    out.append("func describedVocabulary(name, raw string) (known bool, back string, ok bool) {")
    out.append(f"{TAB}switch name {{")
    for n in names:
        go_type, kind = _GO_VOCAB[n]
        out.append(f"{TAB}case {_q(n)}:")
        if kind == "int":
            out.append(f"{TAB}{TAB}i, err := strconv.ParseInt(raw, 10, 32)")
            out.append(f"{TAB}{TAB}if err != nil {{")
            out.append(f'{TAB}{TAB}{TAB}return false, "", false')
            out.append(f"{TAB}{TAB}}}")
            out.append(f"{TAB}{TAB}v := {go_type}(i)")
            out.append(f"{TAB}{TAB}return v.Known(), strconv.FormatInt(int64(v), 10), true")
        else:
            out.append(f"{TAB}{TAB}v := {go_type}(raw)")
            out.append(f"{TAB}{TAB}return v.Known(), string(v), true")
    out.append(f"{TAB}}}")
    out.append(f'{TAB}return false, "", false')
    out.append("}")
    out.append("")
    return out


def render_vocab_gen(model) -> str:
    out = [f"// {banner(model)}", "", "package chtypes", "", 'import "strconv"', ""]
    parts = [
        _status,
        _format,
        _doc_flags,
        _reason,
        _source,
        _outcome,
        _filter_outcome,
        _verdict,
        _default_kind,
    ]
    if model.major >= 2:
        parts += [_discover_query_param, _merge_reason, _declined_tier, _declined_layer, _described]
    for part in parts:
        out += part(model)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def outputs(model) -> list[Output]:
    return [Output(VOCAB_GEN, content=render_vocab_gen(model))]
