"""model.py: the typed ABI model every emitter consumes.

`load(root, major)` reads one ABI major's four inputs, under spec/abi-v<major>/
(spec/abi-v1/ by default; `spec(major)` names every path):

  abi.json          the ABI itself, and the ONLY fingerprinted input
  sdk.json          SDK-side policy: error classes, loader refusals, the
                    build_info cross-check map, D1.3's reuse flag, the
                    provisional-marker legend, per-binding naming overrides
  docs.md           the prose, one `### <symbol>` section per symbol
  v0-symbols.json   every v0 `chs_*` prototype from the released headers

and returns a frozen `Model`, or raises `ModelError` listing EVERY problem it
found (not just the first), so `gen.py --check` reports them all at once.

Validation happens in three layers, each refusing rather than guessing:

  1. jcs.loads: ASCII only, no floats, |n| < 2**53, no duplicate keys
     (what makes the fingerprint exact);
  2. the JSON Schemas under spec/abi-v1/schema/, through `validate()` below,
     a stdlib validator for the subset of draft 2020-12 those schemas use. A
     schema keyword outside that subset is an error, never silently ignored;
  3. cross-references no schema can express: a handle's free function exists
     and has the free shape, every status a call may return is a chs_status
     value, a FIRM function references no provisional handle or enum, every
     provisional marker has a legend entry, docs.md has exactly one section
     per symbol, and (while sdk.json says reuse_v0_names is false) no v0 name
     is reused with a different signature.

From generation 2 on, load() also checks the generation's own rules
(`generation_rule_problems`, below): the description says its stability, a
growable function takes exactly one options document and every input
document closes its objects (r1), a result document's schema never closes an
object to new fields (r2), no enum value takes the name readers reserve for
unknown(n) (r3), every status and error code generation 1 published keeps its
name and number (r4), and docs.md carries the `## Rules` section naming each
rule. Every major checks that the description's `abi` is the major it is
generated as.

Emitters receive the `Model` and never read the JSON themselves. Everything an
emitter needs about a parameter's C shape is precomputed here (`Param.c`,
`Return.c_type`, `Function.prototype()`), so the header and all four bindings
derive from ONE expansion of the parameter kinds.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jcs

# The ABI majors this generator knows. Each has its own description directory
# and its own outputs; generating one never reads or writes another's outputs
# (generation 2's r4 check reads generation 1's description, never its outputs).
MAJORS = (1, 2)


@dataclass(frozen=True)
class Spec:
    """Where one ABI major's inputs live: spec/abi-v<major>/."""

    major: int

    @property
    def dir(self) -> str:
        return f"spec/abi-v{self.major}"

    @property
    def abi_json(self) -> str:
        return f"{self.dir}/abi.json"

    @property
    def abi_schema(self) -> str:
        return f"{self.dir}/schema/abi.schema.json"

    @property
    def sdk_json(self) -> str:
        return f"{self.dir}/sdk.json"

    @property
    def sdk_schema(self) -> str:
        return f"{self.dir}/schema/sdk.schema.json"

    @property
    def docs_md(self) -> str:
        return f"{self.dir}/docs.md"

    @property
    def v0_json(self) -> str:
        return f"{self.dir}/v0-symbols.json"


def spec(major: int) -> Spec:
    if major not in MAJORS:
        raise ValueError(f"ABI v{major} is not one of the majors this generator knows ({MAJORS})")
    return Spec(major)


# ABI v1's paths, as every caller that predates the major parameter names them.
ABI_JSON = spec(1).abi_json
ABI_SCHEMA = spec(1).abi_schema
SDK_JSON = spec(1).sdk_json
SDK_SCHEMA = spec(1).sdk_schema
DOCS_MD = spec(1).docs_md
V0_JSON = spec(1).v0_json

# The stabilities a generation-2+ description declares in its top-level
# `stability` (generation 1 predates the field and is locked by definition).
STABILITIES = ("unstable", "locked")
# The rules every generation-2+ docs.md states in its `## Rules` section, by
# label; load() refuses a Rules section that drops one.
GENERATION_RULES = ("r1", "r2", "r3", "r4", "r5", "r6", "r7")
# The spelling readers reserve for the member an unlisted enum value maps to
# (r3): no described value may take it.
UNKNOWN = "unknown"

# The closed vocabularies. Every emitter must be total over each of them; a
# new entry here is an emitter change in every lane, which is why the schema
# pins the same sets.
SCALARS: dict[str, str] = {
    "void": "void",
    "int": "int",  # C's int: only the frozen v0 tombstone, chs_abi_revision, uses it
    "int32": "int32_t",
    "uint32": "uint32_t",
    "int64": "int64_t",
    "uint64": "uint64_t",
    "size": "size_t",
    "u8ptr_const": "const uint8_t *",
    "cstr_static": "const char *",
}
PARAM_KINDS = ("scalar", "enum", "bytes_in", "handle", "out_handle", "out_error", "out_scalar")
RETURN_KINDS = ("status", "void", "scalar", "enum", "handle")
CLASSES = ("handshake", "tombstone", "api", "tooling")
# The thread classes: what a caller may run at the same time as a call. Every
# function names one; the emitters print these descriptions (no second copy),
# and the schema pins the same set. The rule tying a class to a function's
# shape (decision 1, "concurrent reads on one handle, everywhere it is safe")
# is checked in load(): a call that reads a caller's handle is `shared`, a
# free is `handle_serial`, and nothing else is either.
THREADS: dict[str, str] = {
    "any": "reads no caller handle: safe from any thread, concurrently with any call",
    "shared": "reads one or more caller handles and never changes them: safe concurrently with any call, "
    "on the same handles too, except a free of one of those handles",
    "handle_serial": "a free: releases the caller's reference, so it must not overlap any other call "
    "that uses the same handle",
    "process_once": "process setup, once per image: the first call sets process state, and a repeat with "
    "the same arguments is a no-op that is safe at any time",
    "process_serial": "process-level: must not overlap any other call into the same library image",
}
# What a counted byte string carries. Every one is a byte string, never a C
# string; the description says which, so a binding picks a byte-safe type for
# a name and the stub and the cases know what a valid input looks like. The
# emitters print these descriptions; there is no second copy.
CONTENTS: dict[str, str] = {
    "bytes": "arbitrary bytes: a body, a string value, an export",
    "name": "a ClickHouse column name: NUL and invalid UTF-8 are legal, so never assume UTF-8",
    "sql": "SQL text (a statement, an expression, a type, a quoted name or literal): may carry any byte",
    "message": "a ClickHouse message: may quote input bytes, so may carry any byte",
    "ascii": "guaranteed ASCII",
    "json_object_string_values": "a JSON object whose values are JSON strings (settings, query parameters)",
    "json_array_names": "a JSON array of column names, each an object carrying the name as byte_strings says",
    "timezone": "a time zone name, validated by ClickHouse's own DateLUT; length 0 means UTC",
}
# A bytes_in content `input:<name>` names an entry of the description's
# `inputs` (generation 2 on): a JSON object the library validates and whose
# unknown keys it refuses (r1). Beside CONTENTS, not in it: its meaning is the
# named document's schema and prose, not one line.
INPUT_PREFIX = "input:"
STATUS_ENUM = "chs_status"
ERROR_HANDLE = "chs_error"
BUF_HANDLE = "chs_buf"
TOMBSTONE = "chs_abi_revision"
TOMBSTONE_CONSTANT = "CHS_ABI_REVISION_TOMBSTONE"
TOMBSTONE_VALUE = 1001

# Parameter names become C (and C++) identifiers in the header, which CI
# compiles as both; a keyword of either language is refused here rather than
# by a compiler error three steps later.
_KEYWORDS = frozenset(
    """auto break case char const continue default do double else enum extern float for goto if inline int
    long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile
    while alignas alignof and and_eq asm bitand bitor bool catch char8_t char16_t char32_t class compl concept
    consteval constexpr constinit const_cast co_await co_return co_yield decltype delete dynamic_cast explicit
    export false friend mutable namespace new noexcept not not_eq nullptr operator or or_eq private protected
    public reinterpret_cast requires static_assert static_cast template this thread_local throw true try typeid
    typename using virtual wchar_t xor xor_eq""".split()
)


class ModelError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


# ------------------------------------------------------------------ JSON Schema


ANNOTATION_KEYWORDS = frozenset({"$schema", "$id", "$comment", "title", "description", "examples", "default"})
SUPPORTED_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "oneOf",
        "anyOf",
        "allOf",
    }
)
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _json_equal(a: Any, b: Any) -> bool:
    """JSON equality: True is not 1, and 1 is not 1.0's float twin here."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def schema_keyword_problems(schema: Any, where: str = "#") -> list[str]:
    """Every keyword in `schema` (recursively) that `validate` does not
    implement. A validator that skipped an unknown keyword would pass
    documents the schema's author meant to refuse."""
    out: list[str] = []
    if isinstance(schema, bool):
        return out
    if not isinstance(schema, dict):
        return [f"{where}: a schema must be an object or a boolean"]
    for key, value in schema.items():
        if key in ANNOTATION_KEYWORDS:
            continue
        if key not in SUPPORTED_KEYWORDS:
            out.append(f"{where}: keyword {key!r} is not implemented by scripts/abi-v1/model.py's validator")
            continue
        if key in ("properties", "$defs"):
            for name, sub in value.items():
                out += schema_keyword_problems(sub, f"{where}/{key}/{name}")
        elif key in ("additionalProperties", "propertyNames", "items"):
            out += schema_keyword_problems(value, f"{where}/{key}")
        elif key in ("oneOf", "anyOf", "allOf"):
            for i, sub in enumerate(value):
                out += schema_keyword_problems(sub, f"{where}/{key}/{i}")
    return out


def validate(instance: Any, schema: Any, root: Any = None, path: str = "$") -> list[str]:
    """Validate `instance` against `schema` (the subset in SUPPORTED_KEYWORDS);
    return every violation as `path: message`. Patterns are searched, as JSON
    Schema specifies, so every pattern in these schemas anchors itself."""
    if root is None:
        root = schema
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: not allowed here"]
    errs: list[str] = []
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            return [f"{path}: unsupported $ref {ref!r} (only #/$defs/<name>)"]
        target = root.get("$defs", {}).get(ref[len("#/$defs/") :])
        if target is None:
            return [f"{path}: unresolved $ref {ref!r}"]
        errs += validate(instance, target, root, path)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](instance) for t in types):
            return errs + [f"{path}: expected {' or '.join(types)}, got {type(instance).__name__} {instance!r}"]
    if "const" in schema and not _json_equal(instance, schema["const"]):
        errs.append(f"{path}: must be {schema['const']!r}, got {instance!r}")
    if "enum" in schema and not any(_json_equal(instance, e) for e in schema["enum"]):
        errs.append(f"{path}: {instance!r} is not one of {schema['enum']!r}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errs.append(f"{path}: {instance!r} does not match {schema['pattern']!r}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errs.append(f"{path}: {instance} is below {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errs.append(f"{path}: {instance} is above {schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in instance:
                if any(_json_equal(item, s) for s in seen):
                    errs.append(f"{path}: duplicate item {item!r}")
                seen.append(item)
        if "items" in schema:
            for i, item in enumerate(instance):
                errs += validate(item, schema["items"], root, f"{path}[{i}]")
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errs.append(f"{path}: missing required {name!r}")
        for name, value in instance.items():
            if name in props:
                errs += validate(value, props[name], root, f"{path}.{name}")
            elif "additionalProperties" in schema:
                errs += validate(value, schema["additionalProperties"], root, f"{path}.{name}")
        if "propertyNames" in schema:
            for name in instance:
                errs += validate(name, schema["propertyNames"], root, f"{path} key {name!r}")
    for sub in schema.get("allOf", []):
        errs += validate(instance, sub, root, path)
    if "anyOf" in schema and not any(not validate(instance, sub, root, path) for sub in schema["anyOf"]):
        errs.append(f"{path}: matches none of anyOf")
    if "oneOf" in schema:
        results = [validate(instance, sub, root, path) for sub in schema["oneOf"]]
        ok = [i for i, r in enumerate(results) if not r]
        if len(ok) != 1:
            if not ok:
                # Name the closest branch's first complaint, so a typo in one
                # parameter does not read as "matches nothing".
                best = min(results, key=len)
                errs.append(f"{path}: matches no oneOf branch (closest: {best[0]})")
            else:
                errs.append(f"{path}: matches {len(ok)} oneOf branches, exactly one required")
    return errs


# ------------------------------------------------------------------------ C


def norm_c(t: str) -> str:
    """One spelling per C type: collapsed whitespace, `*` written `T *`."""
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*\*\s*", "*", t)
    t = re.sub(r"\*+", lambda m: " " + m.group(0), t)
    return t.strip()


def join_decl(c_type: str, name: str) -> str:
    """`const uint8_t *` + `body` -> `const uint8_t *body`; `size_t` + `n` -> `size_t n`."""
    return f"{c_type}{name}" if c_type.endswith("*") else f"{c_type} {name}"


@dataclass(frozen=True)
class CParam:
    c_type: str
    name: str

    def decl(self) -> str:
        return join_decl(self.c_type, self.name)


@dataclass(frozen=True)
class Param:
    name: str
    kind: str
    type: str | None
    const: bool
    nullable: bool
    content: str | None
    c: tuple[CParam, ...]

    @property
    def is_out(self) -> bool:
        return self.kind in ("out_handle", "out_error", "out_scalar")


@dataclass(frozen=True)
class Return:
    kind: str
    type: str | None
    owned: bool
    nullable: bool
    content: str | None
    borrows: str | None
    constant: str | None  # a return that is always one described constant
    c_type: str


@dataclass(frozen=True)
class Function:
    name: str
    cls: str
    provisional: tuple[str, ...]
    thread: str
    returns: Return
    may_return: tuple[str, ...]
    params: tuple[Param, ...]
    doc: str
    growable: bool = False  # r1, generation 2 on: takes exactly one options document

    @property
    def is_firm(self) -> bool:
        return not self.provisional

    @property
    def c_params(self) -> tuple[CParam, ...]:
        return tuple(cp for p in self.params for cp in p.c)

    def prototype(self, api: str = "CHS_API") -> str:
        args = ", ".join(cp.decl() for cp in self.c_params) or "void"
        return f"{api} {join_decl(self.returns.c_type, self.name)}({args});"

    def signature_key(self) -> tuple[str, tuple[str, ...]]:
        """(return type, parameter types), normalized as v0-symbols.json is."""
        return norm_c(self.returns.c_type), tuple(norm_c(cp.c_type) for cp in self.c_params)


@dataclass(frozen=True)
class Handle:
    name: str
    free: str
    holds: tuple[str, ...]
    provisional: tuple[str, ...]
    doc: str


@dataclass(frozen=True)
class EnumValue:
    name: str | None  # the C constant, for an int32 enum
    value: int | str
    fields: dict[str, Any]


@dataclass(frozen=True)
class Enum:
    name: str
    repr: str  # "int32" (a C enum) or "string" (a document vocabulary)
    closed: bool
    frozen: bool
    values: tuple[EnumValue, ...]
    fields: dict[str, str]
    fallback: str | None
    provisional: tuple[str, ...]
    doc: str

    @property
    def c_type(self) -> str:
        return self.name


@dataclass(frozen=True)
class Constant:
    name: str
    type: str
    value: int
    provisional: tuple[str, ...]
    doc: str

    @property
    def is_firm(self) -> bool:
        return not self.provisional


@dataclass(frozen=True)
class InputDoc:
    """An input document (generation 2 on): a JSON object a caller passes in a
    bytes_in parameter whose content is `input:<name>`. Its schema closes
    every object, because the library refuses a key it does not define (r1).
    `options` marks the one document a growable function takes the inputs it
    gains in."""

    name: str
    options: bool
    schema: Any
    doc: str


def input_name(content: str | None) -> str | None:
    """`server_profile` for the content `input:server_profile`, else None."""
    if content and content.startswith(INPUT_PREFIX):
        return content[len(INPUT_PREFIX) :]
    return None


@dataclass(frozen=True)
class Document:
    name: str
    provisional: tuple[str, ...]
    carries_names: bool
    schema: Any
    doc: str
    byte_fields: tuple[str, ...] = ()
    value_entries: tuple[str, ...] = ()


# ------------------------------------------------------------- byte strings
#
# The byte_strings rule (abi.json): a data-derived string F is the member F
# (valid UTF-8) or F_b64 (standard base64), never both. A document lists every
# such field in byte_fields, as a path; this section checks that the
# document's own JSON Schema says exactly that for each one, so the list a
# binding generates a decoder from and the schema a reviewer reads (and the
# validator enforces) can never disagree.

_B64_PATTERN = "^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"


def byte_rule(field: str, suffix: str, required: bool) -> dict[str, Any]:
    """The canonical subschema for one byte field: exactly one of F and F_b64
    (`required`), or at most one."""
    b = field + suffix
    if required:
        return {"oneOf": [{"required": [field]}, {"required": [b]}]}
    return {
        "oneOf": [
            {"required": [field], "properties": {b: False}},
            {"required": [b], "properties": {field: False}},
            {"properties": {field: False, b: False}},
        ]
    }


def _deref(schema: dict[str, Any], node: Any) -> Any:
    seen = 0
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/$defs/") or seen > 32:
            return None
        node = schema.get("$defs", {}).get(ref[len("#/$defs/") :])
        seen += 1
    return node


def _holder(schema: dict[str, Any], node: Any, name: str) -> Any:
    """`node` (dereferenced), or the one oneOf/anyOf branch of it that
    declares the member `name` (a nullable object is `oneOf: [null, object]`)."""
    node = _deref(schema, node)
    if not isinstance(node, dict):
        return None
    if name in node.get("properties", {}):
        return node
    for key in ("oneOf", "anyOf"):
        hits = [b for b in (_deref(schema, x) for x in node.get(key, ())) if isinstance(b, dict)]
        hits = [b for b in hits if name in b.get("properties", {})]
        if len(hits) == 1:
            return hits[0]
    return None


def resolve_path(schema: dict[str, Any], path: str) -> tuple[Any, str] | None:
    """(the object schema holding the path's last member, that member), or
    None. Every `[]` steps into `items`; every other step into `properties`."""
    parts = path.split(".")
    node: Any = schema
    for part in parts[:-1]:
        name, levels = part.rstrip("[]"), part.count("[]")
        node = _holder(schema, node, name)
        if node is None:
            return None
        node = node["properties"][name]
        for _ in range(levels):
            node = _deref(schema, node)
            if not isinstance(node, dict) or "items" not in node:
                return None
            node = node["items"]
    last = parts[-1]
    if last.endswith("[]"):
        name, levels = last.rstrip("[]"), last.count("[]")
        node = _holder(schema, node, name)
        if node is None:
            return None
        node = node["properties"][name]
        for _ in range(levels):
            node = _deref(schema, node)
            if not isinstance(node, dict) or "items" not in node:
                return None
            node = node["items"]
        return _deref(schema, node), ""
    node = _holder(schema, node, last)
    return None if node is None else (node, last)


def _object_schemas(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every object schema in a document schema, with where it sits."""
    out: list[tuple[str, dict[str, Any]]] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            # A byte rule's branches (no "type") constrain their parent
            # object; only a schema that declares itself an object is one.
            if "properties" in node and node.get("type") == "object":
                out.append((where, node))
            for k, v in node.items():
                if k in ("properties", "$defs"):
                    for name, sub in v.items():
                        walk(sub, f"{where}/{k}/{name}")
                elif k in ("items", "additionalProperties"):
                    walk(v, f"{where}/{k}")
                elif k in ("oneOf", "anyOf", "allOf"):
                    for i, sub in enumerate(v):
                        walk(sub, f"{where}/{k}/{i}")

    walk(schema, "#")
    return out


def byte_field_problems(doc: Document, suffix: str, value_member: str) -> list[str]:
    where = f"document {doc.name}"
    if doc.schema is None:
        if doc.byte_fields or doc.value_entries:
            return [f"{where}: lists byte_fields or value_entries but fixes no schema to check them against"]
        return []
    problems: list[str] = []
    claimed: set[tuple[int, str]] = set()
    for path in doc.byte_fields:
        got = resolve_path(doc.schema, path)
        if got is None or not got[1]:
            problems.append(f"{where}: byte field {path!r} does not resolve to a member in the document's schema")
            continue
        obj, field = got
        props = obj.get("properties", {})
        b = field + suffix
        if props.get(field) != {"type": "string"}:
            problems.append(f"{where}: byte field {path!r}: the member {field!r} must be {{\"type\": \"string\"}}")
        if props.get(b) != {"type": "string", "pattern": _B64_PATTERN}:
            problems.append(f"{where}: byte field {path!r}: the member {b!r} must be a standard-base64 string")
        rules = obj.get("allOf", [])
        if byte_rule(field, suffix, True) not in rules and byte_rule(field, suffix, False) not in rules:
            problems.append(
                f"{where}: byte field {path!r}: the object's allOf has no rule allowing exactly (or at most) one "
                f"of {field!r} and {b!r}"
            )
        claimed.add((id(obj), field))
    valued: set[int] = set()
    for path in doc.value_entries:
        got = resolve_path(doc.schema, path)
        if got is None or got[1]:
            problems.append(f"{where}: value entry {path!r} does not resolve to an array's entries")
            continue
        obj = got[0]
        if obj.get("properties", {}).get(value_member) != {"type": "string", "pattern": _B64_PATTERN}:
            problems.append(f"{where}: value entry {path!r} has no standard-base64 {value_member!r} member")
        if (id(obj), "stored") not in claimed:
            problems.append(f"{where}: value entry {path!r} carries no `stored` byte field beside {value_member!r}")
        valued.add(id(obj))
    # Nothing carries the rule's members unlisted: every *_b64 member is a
    # listed byte field's sibling, or the value member of a listed entry.
    for at, obj in _object_schemas(doc.schema):
        for name in obj.get("properties", {}):
            if name == value_member:
                if id(obj) not in valued:
                    problems.append(f"{where}: {at} has {value_member!r}, but no value_entries path names it")
            elif name.endswith(suffix) and (id(obj), name[: -len(suffix)]) not in claimed:
                problems.append(f"{where}: {at} has {name!r}, but no byte_fields path names {name[: -len(suffix)]!r}")
    return problems


@dataclass(frozen=True)
class Docs:
    title: str
    preamble: str
    sections: dict[str, str]
    rules: str = ""  # the `## Rules` section: generation 2 on; generation 1's docs.md has none


@dataclass(frozen=True)
class Model:
    root: Path
    raw: dict[str, Any]
    fingerprint: str
    description_schema: int
    abi: int
    library_stem: str
    prefix: str
    scalars: tuple[str, ...]
    handles: dict[str, Handle]
    enums: dict[str, Enum]
    constants: dict[str, Constant]
    documents: dict[str, Document]
    byte_strings: dict[str, str]
    build_info_schema: dict[str, Any]
    build_info_provisional: dict[str, tuple[str, ...]]
    functions: tuple[Function, ...]
    sdk: dict[str, Any]
    docs: Docs
    v0: dict[str, list[dict[str, Any]]]
    major: int = 1
    stability: str | None = None  # generation 2 on: "unstable" until the lock, then "locked"
    inputs: dict[str, InputDoc] = field(default_factory=dict)  # generation 2 on: the input documents (r1)

    @property
    def spec(self) -> Spec:
        return spec(self.major)

    @property
    def unstable(self) -> bool:
        return self.stability == "unstable"

    def function(self, name: str) -> Function:
        for f in self.functions:
            if f.name == name:
                return f
        raise KeyError(name)

    def input_of(self, p: Param) -> InputDoc | None:
        """The input document a bytes_in parameter carries, or None."""
        name = input_name(p.content) if p.kind == "bytes_in" else None
        return self.inputs.get(name) if name else None

    def options_param(self, fn: Function) -> Param | None:
        """The one options document a growable function takes (r1), or None."""
        found = [p for p in fn.params if (d := self.input_of(p)) is not None and d.options]
        return found[0] if fn.growable and len(found) == 1 else None

    @property
    def markers(self) -> dict[str, str]:
        return self.sdk["provisional_markers"]

    @property
    def int_enums(self) -> list[Enum]:
        return [e for e in self.enums.values() if e.repr == "int32"]

    @property
    def vocabularies(self) -> list[Enum]:
        return [e for e in self.enums.values() if e.repr == "string"]

    def symbols(self) -> list[str]:
        """Every exported symbol: what a loader resolves at step 6 and what an
        export list names. Sorted."""
        return sorted(f.name for f in self.functions)


# --------------------------------------------------------------------- docs


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def parse_docs(text: str, docs_md: str = DOCS_MD) -> tuple[Docs, list[str]]:
    """docs.md: `# title`, a `## Preamble` section, optionally a `## Rules`
    section (generation 2 on), and `### <symbol>` sections (the symbol in
    backticks or bare). Headings inside fenced code are text. A symbol section
    runs to the next heading of any level, so a section cannot smuggle in a
    heading of its own; the Rules section therefore holds no heading either."""
    problems: list[str] = []
    title = ""
    preamble: list[str] = []
    rules: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            level, heading = len(m.group(1)), m.group(2).strip("`")
            if level == 1:
                title = heading
                current = None
            elif level == 2:
                current = {"Preamble": preamble, "Rules": rules}.get(heading)
            elif level == 3:
                if heading in sections:
                    problems.append(f"{docs_md}:{n}: a second section for {heading!r}")
                sections[heading] = []
                current = sections[heading]
            else:
                problems.append(f"{docs_md}:{n}: heading level {level} inside a symbol section; use prose")
                current = None
            continue
        if current is not None:
            current.append(line)
    if in_fence:
        problems.append(f"{docs_md}: an unterminated code fence")

    def body(lines: list[str]) -> str:
        return "\n".join(lines).strip("\n")

    docs = Docs(
        title=title,
        preamble=body(preamble),
        sections={k: body(v) for k, v in sections.items()},
        rules=body(rules),
    )
    if not docs.preamble:
        problems.append(f"{docs_md}: no `## Preamble` section (the header's opening comment comes from it)")
    for name, prose in docs.sections.items():
        if not prose.strip():
            problems.append(f"{docs_md}: the section for {name!r} is empty")
    return docs, problems


# --------------------------------------------------------------------- load


def _markers(entry: dict[str, Any]) -> tuple[str, ...]:
    return tuple(entry.get("provisional", ()))


def _read_json(root: Path, rel: str, problems: list[str], strict: bool = True) -> Any:
    path = root / rel
    try:
        data = path.read_bytes()
    except OSError as e:
        problems.append(f"{rel}: unreadable ({e.strerror})")
        return None
    try:
        return jcs.loads(data) if strict else json.loads(data)
    except (jcs.JCSError, json.JSONDecodeError) as e:
        problems.append(f"{rel}: {e}")
        return None


def _schema_check(root: Path, instance: Any, schema_rel: str, what: str, problems: list[str]) -> bool:
    schema = _read_json(root, schema_rel, problems)
    if schema is None:
        return False
    kw = schema_keyword_problems(schema)
    if kw:
        problems += [f"{schema_rel}: {p}" for p in kw]
        return False
    errs = validate(instance, schema)
    problems += [f"{what} fails {schema_rel}: {e}" for e in errs]
    return not errs


def _expand_param(p: dict[str, Any], handles: dict[str, Any], enums: dict[str, Any]) -> Param:
    kind, name = p["kind"], p["name"]
    t = p.get("type")
    const = bool(p.get("const", False))
    if kind == "scalar":
        c = (CParam(SCALARS[t], name),)
    elif kind == "enum":
        c = (CParam(t, name),)
    elif kind == "bytes_in":
        c = (CParam("const uint8_t *", name), CParam("size_t", f"{name}_len"))
    elif kind == "handle":
        c = (CParam(f"{'const ' if const else ''}{t} *", name),)
    elif kind == "out_handle":
        c = (CParam(f"{t} **", name),)
    elif kind == "out_error":
        t = ERROR_HANDLE
        c = (CParam(f"{ERROR_HANDLE} **", name),)
    else:  # out_scalar
        c = (CParam(f"{SCALARS[t]} *", name),)
    return Param(
        name=name,
        kind=kind,
        type=t,
        const=const,
        nullable=bool(p.get("nullable", kind == "out_error")),
        content=p.get("content"),
        c=c,
    )


def _expand_return(r: dict[str, Any]) -> Return:
    kind, t = r["kind"], r.get("type")
    c_type = {
        "status": STATUS_ENUM,
        "void": "void",
        "scalar": SCALARS.get(t or "", "?"),
        "enum": t or "?",
        "handle": f"{t} *",
    }[kind]
    return Return(
        kind=kind,
        type=STATUS_ENUM if kind == "status" else t,
        owned=bool(r.get("owned", False)),
        nullable=bool(r.get("nullable", False)),
        content=r.get("content"),
        borrows=r.get("borrows"),
        constant=r.get("constant"),
        c_type=c_type,
    )


# ------------------------------------------------------- generation 2's rules
#
# The rules every generation from 2 on is written under are normative in its
# docs.md `## Rules` section. These are the parts a description can be checked
# against. (r5) and (r6), the cache and the dev channel, are binding behavior.


def _closed_objects(node: Any, where: str) -> list[str]:
    """Every place in a JSON Schema that closes an object to new fields."""
    out: list[str] = []
    if isinstance(node, dict):
        if node.get("additionalProperties") is False:
            out.append(where)
        for k, v in node.items():
            out += _closed_objects(v, f"{where}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _closed_objects(v, f"{where}/{i}")
    return out


def _open_objects(node: Any, where: str) -> list[str]:
    """Every object schema in an input document's schema that does not close
    its object (r1). A record closes with `additionalProperties: false`; a
    map, whose keys are data (setting or macro names) rather than names the
    ABI defines, has an `additionalProperties` schema and no `properties`.
    Anything else would let a key the library refuses look valid."""
    out: list[str] = []
    if isinstance(node, dict):
        is_object = node.get("type") == "object" or "properties" in node
        if is_object:
            ap = node.get("additionalProperties")
            record = ap is False
            mapping = isinstance(ap, dict) and "properties" not in node
            if not (record or mapping):
                out.append(where)
        for k, v in node.items():
            if k in ("properties", "$defs") and isinstance(v, dict):
                for name, sub in v.items():
                    out += _open_objects(sub, f"{where}/{k}/{name}")
            elif k in ("additionalProperties", "items", "propertyNames"):
                out += _open_objects(v, f"{where}/{k}")
            elif k in ("oneOf", "anyOf", "allOf") and isinstance(v, list):
                for i, sub in enumerate(v):
                    out += _open_objects(sub, f"{where}/{k}/{i}")
    return out


def r1_problems(sp: Spec, raw: dict[str, Any]) -> list[str]:
    """(r1) A growable function takes its growable inputs in exactly one
    options document, which the library validates, refusing a key it does not
    know. What a description can be checked for:

      * a function marked `growable` takes exactly one options document: one
        bytes_in parameter whose content names an `inputs` entry with
        `options` true;
      * a function not marked `growable` takes no input document at all;
      * every input document is a JSON object whose schema closes every
        object (`_open_objects`), so its schema refuses what the library does;
      * every input document is taken by some function.

    A content naming no `inputs` entry is load()'s cross-reference problem."""
    problems: list[str] = []
    inputs = raw.get("inputs", {})
    taken: set[str] = set()
    for f in raw["functions"]:
        docs = [(p["name"], input_name(p.get("content"))) for p in f["params"] if p["kind"] == "bytes_in"]
        docs = [(pn, n) for pn, n in docs if n is not None]
        taken |= {n for _, n in docs}
        where = f"{sp.abi_json}: function {f['name']}"
        if not f.get("growable", False):
            for pn, n in docs:
                problems.append(
                    f"{where}: takes the input document {n!r} (parameter {pn!r}) but is not marked `growable`; "
                    "(r1) a function takes an input document only as a growable function does, so mark it "
                    "growable or take the input another way"
                )
            continue
        options = [f"{pn} (input:{n})" for pn, n in docs if inputs.get(n, {}).get("options") is True]
        if len(options) != 1:
            problems.append(
                f"{where}: is marked `growable`, so (r1) it takes exactly one options document (a bytes_in "
                "parameter whose content is input:<name>, naming an `inputs` entry with `options` true); it takes "
                + (str(len(options)) + ": " + ", ".join(options) if options else "none")
            )
    for name, entry in inputs.items():
        schema = entry.get("schema")
        at = f"inputs.{name}.schema"
        if not isinstance(schema, dict) or schema.get("type") != "object":
            problems.append(f"{sp.abi_json}: {at} is not an object schema; (r1) an input document is a JSON object")
        for where in _open_objects(schema, at):
            problems.append(
                f"{sp.abi_json}: {where} does not close its object, but (r1) the library refuses a key an input "
                "document does not define: close a record with `additionalProperties: false`, or give a map whose "
                "keys are data an `additionalProperties` schema and no `properties`"
            )
        if name not in taken:
            problems.append(f"{sp.abi_json}: inputs.{name} is taken by no function's bytes_in parameter")
    return problems


def _reserved_unknown(spelling: Any) -> bool:
    """True when an enum value's spelling is the readers' unknown(n) member:
    a vocabulary value `unknown`, or a C name `CHS_UNKNOWN` or `CHS_<..>_UNKNOWN`."""
    if not isinstance(spelling, str):
        return False
    up = spelling.upper()
    return up == UNKNOWN.upper() or up.endswith("_" + UNKNOWN.upper())


def generation_rule_problems(root: Path, sp: Spec, raw: dict[str, Any], sdk: dict[str, Any], docs: Docs) -> list[str]:
    problems: list[str] = []

    # Stability: generation 2 on says whether its fingerprint may still move.
    stability = raw.get("stability")
    if stability not in STABILITIES:
        problems.append(
            f"{sp.abi_json}: from generation 2 on the description declares `stability`, one of "
            f"{list(STABILITIES)} ('unstable' until the lock); found {stability!r}"
        )

    # (r1) a growable function's one options document, and closed input documents.
    problems += r1_problems(sp, raw)

    # The rules themselves are stated, every one of them.
    if not docs.rules.strip():
        problems.append(
            f"{sp.docs_md}: no `## Rules` section; generation {sp.major}'s rules "
            f"({', '.join(GENERATION_RULES)}) are normative there"
        )
    else:
        for label in GENERATION_RULES:
            if f"({label})" not in docs.rules:
                problems.append(f"{sp.docs_md}: the `## Rules` section does not state ({label})")

    # (r2) readers ignore unknown fields, so no result schema refuses one.
    schemas = [(f"documents.{n}.schema", d.get("schema")) for n, d in raw["documents"].items()]
    schemas.append(("build_info.schema", raw["build_info"]["schema"]))
    for at, schema in schemas:
        for where in _closed_objects(schema, at):
            problems.append(
                f"{sp.abi_json}: {where} closes an object (additionalProperties: false), but (r2) a result "
                "document's readers ignore unknown fields: a field added later must be one an older reader skips"
            )

    # (r3) every enum's readers map an unlisted value to unknown(n), so no
    # described value may take that name.
    for name, e in raw["enums"].items():
        for v in e["values"]:
            spelled = v.get("name") if e["repr"] == "int32" else v["value"]
            if _reserved_unknown(spelled):
                problems.append(
                    f"{sp.abi_json}: enum {name}: the value {spelled!r} takes the name (r3) reserves for the "
                    "member an unlisted value is read as, unknown(n)"
                )

    # (r4) a published error code keeps its name and number. Generation 1's
    # codes are the published ones this generator can see.
    v1 = spec(1)
    read: list[str] = []
    old_abi = _read_json(root, v1.abi_json, read)
    old_sdk = _read_json(root, v1.sdk_json, read)
    if old_abi is None or old_sdk is None:
        problems += [f"(r4) compares against generation 1's published codes, which cannot be read: {p}" for p in read]
        return problems
    now = {v.get("name"): v["value"] for v in raw["enums"].get(STATUS_ENUM, {}).get("values", [])}
    for v in old_abi["enums"][STATUS_ENUM]["values"]:
        found = now.get(v["name"])
        if found != v["value"]:
            what = "is missing" if v["name"] not in now else f"is {found}"
            problems.append(
                f"{sp.abi_json}: {STATUS_ENUM} {v['name']} = {v['value']} is published (generation 1), and (r4) "
                f"keeps its name and number; here it {what}"
            )
    codes = sdk["errors"]["codes"]
    published = old_sdk["errors"]["codes"]
    for key, code in published.items():
        if codes.get(key) != code:
            what = "is missing" if key not in codes else f"is {codes[key]!r}"
            problems.append(
                f"{sp.sdk_json}: errors.codes {key} = {code!r} is published (generation 1), and (r4) keeps it; "
                f"here it {what}"
            )
    for key, code in codes.items():
        if key not in published and code in published.values():
            problems.append(
                f"{sp.sdk_json}: errors.codes {key} reuses the published code {code!r} for another meaning (r4)"
            )
    return problems


def load(root: Path, major: int = 1) -> Model:
    root = Path(root)
    sp = spec(major)
    problems: list[str] = []

    abi_bytes = b""
    try:
        abi_bytes = (root / sp.abi_json).read_bytes()
    except OSError as e:
        raise ModelError([f"{sp.abi_json}: unreadable ({e.strerror})"]) from None
    if not abi_bytes.isascii():
        problems.append(f"{sp.abi_json}: the file is not ASCII")
    raw = _read_json(root, sp.abi_json, problems)
    sdk = _read_json(root, sp.sdk_json, problems)
    v0raw = _read_json(root, sp.v0_json, problems)
    try:
        docs_text = (root / sp.docs_md).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        problems.append(f"{sp.docs_md}: unreadable ({e})")
        docs_text = ""
    if raw is None or sdk is None:
        raise ModelError(problems)

    abi_ok = _schema_check(root, raw, sp.abi_schema, sp.abi_json, problems)
    sdk_ok = _schema_check(root, sdk, sp.sdk_schema, sp.sdk_json, problems)
    if not (abi_ok and sdk_ok):
        raise ModelError(problems)

    docs, doc_problems = parse_docs(docs_text, sp.docs_md)
    problems += doc_problems
    markers: dict[str, str] = sdk["provisional_markers"]

    def marker_check(where: str, entry: dict[str, Any]) -> None:
        for m in _markers(entry):
            if m not in markers:
                problems.append(f"{where}: provisional marker {m!r} has no entry in {sp.sdk_json} provisional_markers")

    def doc(name: str) -> str:
        return docs.sections.get(name, "")

    # ---- handles
    handles: dict[str, Handle] = {}
    for name, h in raw["handles"].items():
        marker_check(f"handle {name}", h)
        handles[name] = Handle(name, h["free"], tuple(h.get("holds", ())), _markers(h), doc(name))
        for held in h.get("holds", ()):
            if held not in raw["handles"]:
                problems.append(f"handle {name}: holds unknown handle {held!r}")
    for required in (BUF_HANDLE, ERROR_HANDLE):
        if required not in handles:
            problems.append(f"handles: {required} is required by D2/D3")

    # ---- enums
    enums: dict[str, Enum] = {}
    for name, e in raw["enums"].items():
        marker_check(f"enum {name}", e)
        fields = e.get("fields", {})
        values = []
        seen_names: set[str] = set()
        seen_values: set[Any] = set()
        for v in e["values"]:
            vname = v.get("name")
            value = v["value"]
            if e["repr"] == "int32":
                if vname is None:
                    problems.append(f"enum {name}: an int32 value needs a C name")
                elif vname in seen_names:
                    problems.append(f"enum {name}: {vname} appears twice")
                if not isinstance(value, int) or isinstance(value, bool):
                    problems.append(f"enum {name}: {vname} must have an integer value")
            elif not isinstance(value, str):
                problems.append(f"enum {name}: a string vocabulary's values are strings, got {value!r}")
            if value in seen_values:
                problems.append(f"enum {name}: the value {value!r} appears twice")
            seen_values.add(value)
            if vname:
                seen_names.add(vname)
            extra = {k: v[k] for k in v if k not in ("name", "value")}
            if set(extra) != set(fields):
                problems.append(
                    f"enum {name}: value {value!r} carries {sorted(extra)}, the enum declares {sorted(fields)}"
                )
            for fk, ftype in fields.items():
                if fk in extra and not _TYPES[ftype](extra[fk]):
                    problems.append(f"enum {name}: value {value!r} field {fk} must be {ftype}")
            values.append(EnumValue(vname, value, extra))
        fallback = e.get("fallback")
        if fallback is not None and fallback not in seen_values:
            problems.append(f"enum {name}: fallback {fallback!r} is not one of its values")
        enums[name] = Enum(
            name=name,
            repr=e["repr"],
            closed=bool(e.get("closed", False)),
            frozen=bool(e.get("frozen", False)),
            values=tuple(values),
            fields=dict(fields),
            fallback=fallback,
            provisional=_markers(e),
            doc=doc(name),
        )
    if STATUS_ENUM not in enums or enums[STATUS_ENUM].repr != "int32":
        problems.append(f"enums: {STATUS_ENUM} (int32) is required by D3")
    status_names = {v.name for v in enums[STATUS_ENUM].values} if STATUS_ENUM in enums else set()

    # ---- constants
    constants: dict[str, Constant] = {}
    enum_value_names = {v.name for e in enums.values() for v in e.values if v.name}
    for name, c in raw["constants"].items():
        marker_check(f"constant {name}", c)
        if name in enum_value_names:
            problems.append(f"constant {name}: also an enum value's name")
        lo, hi = {"int32": (-(2**31), 2**31 - 1), "uint32": (0, 2**32 - 1)}.get(c["type"], (-(2**53), 2**53))
        if not lo <= c["value"] <= hi:
            problems.append(f"constant {name}: {c['value']} does not fit {c['type']}")
        constants[name] = Constant(name, c["type"], c["value"], _markers(c), doc(name))

    # ---- documents
    documents: dict[str, Document] = {}
    for name, d in raw["documents"].items():
        marker_check(f"document {name}", d)
        schema = d.get("schema")
        if schema is not None:
            problems += [f"document {name}: {p}" for p in schema_keyword_problems(schema)]
        documents[name] = Document(
            name,
            _markers(d),
            bool(d["carries_names"]),
            schema,
            doc(f"document:{name}"),
            tuple(d.get("byte_fields", ())),
            tuple(d.get("value_entries", ())),
        )
    bs = raw["byte_strings"]
    for document in documents.values():
        problems += byte_field_problems(document, bs["suffix"], bs["value"])
        if document.carries_names and not any(
            f == "name" or f.endswith(".name") or f.endswith("].name") for f in document.byte_fields
        ):
            problems.append(f"document {document.name}: carries names, but no byte field is a `name`")

    # ---- input documents (generation 2 on; the v1 schema admits none)
    inputs: dict[str, InputDoc] = {}
    for name, d in raw.get("inputs", {}).items():
        schema = d["schema"]
        problems += [f"input {name}: {p}" for p in schema_keyword_problems(schema)]
        inputs[name] = InputDoc(name, bool(d["options"]), schema, doc(f"{INPUT_PREFIX}{name}"))

    def content_check(where: str, content: str | None) -> None:
        if content and content.startswith("document:") and content[len("document:") :] not in documents:
            problems.append(f"{where}: content {content!r} names no entry in documents")
        named = input_name(content)
        if named is not None and named not in inputs:
            problems.append(f"{where}: content {content!r} names no entry in inputs")

    # ---- build_info
    bi = raw["build_info"]
    problems += [f"build_info: {p}" for p in schema_keyword_problems(bi["schema"])]
    bi_props = bi["schema"].get("properties", {})
    bi_prov = {k: tuple(v) for k, v in bi.get("provisional_fields", {}).items()}
    for field, ms in bi_prov.items():
        if field not in bi_props:
            problems.append(f"build_info.provisional_fields: {field!r} is not a build_info property")
        for m in ms:
            if m not in markers:
                problems.append(f"build_info.provisional_fields.{field}: marker {m!r} has no legend entry")

    # ---- scalars
    declared_scalars = tuple(raw["scalars"])
    if set(declared_scalars) != set(SCALARS):
        problems.append(
            f"scalars: {sorted(declared_scalars)} differs from the vocabulary model.py implements "
            f"{sorted(SCALARS)}; a scalar is an emitter change in every lane"
        )

    # ---- the thread vocabulary, which the schema pins too
    schema_threads = (_read_json(root, sp.abi_schema, problems) or {}).get("$defs", {}).get("function", {})
    schema_threads = schema_threads.get("properties", {}).get("thread", {}).get("enum", [])
    if set(schema_threads) != set(THREADS):
        problems.append(
            f"{sp.abi_schema}: the thread enum {sorted(schema_threads)} differs from the classes model.py "
            f"describes {sorted(THREADS)}"
        )

    # ---- functions
    functions: list[Function] = []
    fnames: set[str] = set()
    prefix = raw["library"]["prefix"]
    for f in raw["functions"]:
        name = f["name"]
        where = f"function {name}"
        if name in fnames:
            problems.append(f"{where}: declared twice")
        fnames.add(name)
        if not name.startswith(prefix):
            problems.append(f"{where}: does not start with the frozen prefix {prefix!r}")
        marker_check(where, f)
        params = tuple(_expand_param(p, raw["handles"], raw["enums"]) for p in f["params"])
        ret = _expand_return(f["returns"])
        fn = Function(
            name=name,
            cls=f["class"],
            provisional=_markers(f),
            thread=f["thread"],
            returns=ret,
            may_return=tuple(f.get("may_return", ())),
            params=params,
            doc=doc(name),
            growable=bool(f.get("growable", False)),
        )
        functions.append(fn)

        # Parameters: names, order, references.
        c_names = [cp.name for cp in fn.c_params]
        if len(set(c_names)) != len(c_names):
            problems.append(f"{where}: two C parameters share a name ({c_names})")
        for cn in c_names:
            if cn in _KEYWORDS:
                problems.append(f"{where}: parameter {cn!r} is a C or C++ keyword")
        seen_out = False
        for i, p in enumerate(params):
            pw = f"{where} parameter {p.name}"
            if p.is_out:
                seen_out = True
            elif seen_out:
                problems.append(f"{pw}: an input after an output; outputs come last (D3)")
            if p.kind == "out_error" and i != len(params) - 1:
                problems.append(f"{pw}: the error out-parameter must be last (D3)")
            if p.kind in ("handle", "out_handle") and p.type not in handles:
                problems.append(f"{pw}: unknown handle {p.type!r}")
            if p.kind == "enum" and (p.type not in enums or enums[p.type].repr != "int32"):
                problems.append(f"{pw}: {p.type!r} is not an int32 enum")
            if p.kind in ("scalar", "out_scalar") and p.type in ("void", "u8ptr_const", "cstr_static"):
                problems.append(f"{pw}: {p.type} is a return-only scalar")
            if p.kind in ("scalar", "out_scalar") and p.type == "int":
                problems.append(f"{pw}: `int` is reserved for the frozen tombstone's return")
            if p.kind == "bytes_in" and not p.content:
                problems.append(f"{pw}: a bytes_in parameter must say its content")
            if p.kind == "out_handle" and (p.type == BUF_HANDLE) != bool(p.content):
                problems.append(f"{pw}: an out chs_buf says its content, and only an out chs_buf does")
            if p.kind == "bytes_in" and p.content and p.content.startswith("document:"):
                problems.append(
                    f"{pw}: documents are outputs; an input's content is one of {sorted(CONTENTS)} or "
                    f"{INPUT_PREFIX}<name>"
                )
            if p.kind != "bytes_in" and input_name(p.content) is not None:
                problems.append(f"{pw}: an input document is an input; only a bytes_in parameter carries one")
            content_check(pw, p.content)

        # Returns.
        r = fn.returns
        if r.kind == "status":
            if not fn.may_return:
                problems.append(f"{where}: returns a status, so it must list may_return")
            for s in fn.may_return:
                if s not in status_names:
                    problems.append(f"{where}: may_return names {s!r}, not a {STATUS_ENUM} value")
            if "CHS_OK" not in fn.may_return:
                problems.append(f"{where}: may_return must include CHS_OK")
            if not params or params[-1].kind != "out_error":
                problems.append(f"{where}: a status-returning call ends with a chs_error ** out-parameter (D3)")
        else:
            if fn.may_return:
                problems.append(f"{where}: may_return applies only to a status-returning call")
            if any(p.kind == "out_error" for p in params):
                problems.append(f"{where}: only a status-returning call takes a chs_error ** (D3)")
        if r.kind == "scalar" and r.type not in SCALARS:
            problems.append(f"{where}: unknown scalar return {r.type!r}")
        if r.kind == "scalar" and r.type == "int" and fn.cls != "tombstone":
            problems.append(f"{where}: `int` is reserved for the frozen tombstone's return")
        if r.kind == "enum" and (r.type not in enums or enums[r.type].repr != "int32"):
            problems.append(f"{where}: returns {r.type!r}, not an int32 enum")
        if r.kind == "handle":
            if r.type not in handles:
                problems.append(f"{where}: returns unknown handle {r.type!r}")
            if not r.owned:
                problems.append(
                    f"{where}: a returned handle is a new reference the caller owns (D2: no borrowed handles)"
                )
            if (r.type == BUF_HANDLE) != bool(r.content):
                problems.append(f"{where}: a returned chs_buf says its content, and only a chs_buf does")
            if input_name(r.content) is not None:
                problems.append(f"{where}: an input document is an input; a return never carries one")
        if r.borrows is not None:
            if r.kind != "scalar" or r.type != "u8ptr_const":
                problems.append(f"{where}: only a const uint8_t * return borrows from a parameter")
            if r.borrows not in {p.name for p in params}:
                problems.append(f"{where}: borrows from unknown parameter {r.borrows!r}")
        elif r.kind == "scalar" and r.type == "u8ptr_const":
            problems.append(f"{where}: a const uint8_t * return must say which parameter it borrows from")
        content_check(where, r.content)

        # Classes.
        if fn.cls in ("handshake", "tombstone"):
            if params:
                problems.append(f"{where}: a {fn.cls} function takes no parameters (D1.2)")
            if not fn.is_firm:
                problems.append(f"{where}: the {fn.cls} set is frozen forever (D1.2) and cannot be provisional")

        # FIRM must not lean on anything provisional.
        if fn.is_firm:
            refs = [(p.type, "handle") for p in params if p.kind in ("handle", "out_handle")]
            refs += [(p.type, "enum") for p in params if p.kind == "enum"]
            if r.kind == "handle":
                refs.append((r.type, "handle"))
            if r.kind == "enum":
                refs.append((r.type, "enum"))
            for t, what in refs:
                entity = handles.get(t) if what == "handle" else enums.get(t)
                if entity is not None and entity.provisional:
                    problems.append(
                        f"{where}: a FIRM function references the provisional {what} {t} "
                        f"({', '.join(entity.provisional)}); mark the function provisional too"
                    )

    # ---- handles' free functions, once every function is known
    fmap = {f.name: f for f in functions}
    for h in handles.values():
        fr = fmap.get(h.free)
        if fr is None:
            problems.append(f"handle {h.name}: free function {h.free!r} is not described")
            continue
        ok = (
            fr.returns.kind == "void"
            and len(fr.params) == 1
            and fr.params[0].kind == "handle"
            and fr.params[0].type == h.name
            and not fr.params[0].const
            and fr.params[0].nullable
        )
        if not ok:
            problems.append(
                f"handle {h.name}: {h.free} must be `void {h.free}({h.name} *)`, nullable (D2: free(NULL) is a no-op)"
            )
        if set(fr.provisional) != set(h.provisional):
            problems.append(f"handle {h.name}: {h.free}'s provisional markers must equal the handle's")
    # ---- thread classes follow the shape (decision 1: concurrent reads on one
    # handle, everywhere it is safe). A handle is immutable once made (decision 4),
    # so every call that reads a caller's handle is `shared`; only a free, which
    # releases the caller's reference, is `handle_serial`.
    frees = {h.free for h in handles.values()}
    for fn in functions:
        reads = [p.name for p in fn.params if p.kind == "handle"]
        where = f"function {fn.name}"
        if fn.name in frees:
            if fn.thread != "handle_serial":
                problems.append(f"{where}: a handle's free is thread class `handle_serial`, not `{fn.thread}`")
        elif reads and fn.thread != "shared":
            problems.append(
                f"{where}: reads the caller's handle(s) {reads}, so its thread class must be `shared` "
                f"(concurrent reads on one handle), not `{fn.thread}`"
            )
        elif not reads and fn.thread in ("shared", "handle_serial"):
            problems.append(f"{where}: thread class `{fn.thread}` applies only to a call that takes a handle")

    # ---- docs coverage: exactly one section per symbol
    required = set(handles) | set(enums) | set(fmap) | {f"{INPUT_PREFIX}{n}" for n in inputs}
    optional = set(constants) | {f"document:{d}" for d in documents}
    for name in sorted(required):
        if name not in docs.sections:
            problems.append(
                f"{sp.docs_md}: no `### {name}` section; every handle, enum, function and input document has one"
            )
    for name in sorted(docs.sections):
        if name not in required | optional:
            problems.append(f"{sp.docs_md}: a section for {name!r}, which the description does not define")

    # ---- sdk.json cross-references
    for field in sdk["cross_check"]:
        if field["build_info"] not in bi_props:
            problems.append(f"{sp.sdk_json} cross_check: {field['build_info']!r} is not a build_info property")
    status_map = sdk["errors"]["status"]
    for s in sorted(status_names):
        if s not in status_map:
            problems.append(f"{sp.sdk_json} errors.status: no entry for {s}")
    for s, cls in status_map.items():
        if s != "unknown" and s not in status_names:
            problems.append(f"{sp.sdk_json} errors.status: {s} is not a {STATUS_ENUM} value")
        if cls is not None and cls not in sdk["errors"]["classes"]:
            problems.append(f"{sp.sdk_json} errors.status.{s}: unknown class {cls!r}")
    for r in sdk["loader"]["refusals"]:
        if r["error"] not in sdk["errors"]["classes"]:
            problems.append(f"{sp.sdk_json} loader refusal {r['reason']}: unknown class {r['error']!r}")

    # ---- the v0 tombstone (D1.2), and why D1.3 depends on it.
    #
    # Measured by the tombstone sweep of every released binding (0.1-0.5, all
    # four): a v0 binding treats a MISSING chs_abi_revision as "revision 0,
    # predates the probe" and goes on to call chs_init; it calls the symbol
    # whenever it exists, and refuses cleanly only when it returns an
    # out-of-range revision (1001 measured). So the tombstone is what turns a
    # v0 binding away before it reaches any reused name. It is mandatory, its
    # shape and value are fixed, and no description may reuse a v0 name
    # without it, whatever reuse_v0_names says.
    tomb = fmap.get(TOMBSTONE)
    tomb_problem = None
    if tomb is None:
        tomb_problem = f"function {TOMBSTONE}: missing; it is a mandatory v1 export (D1.2)"
    elif not (
        tomb.cls == "tombstone"
        and not tomb.params
        and tomb.returns.kind == "scalar"
        and tomb.returns.type == "int"
        and tomb.returns.constant == TOMBSTONE_CONSTANT
        and TOMBSTONE_CONSTANT in constants
        and constants[TOMBSTONE_CONSTANT].value == TOMBSTONE_VALUE
        and constants[TOMBSTONE_CONSTANT].is_firm
    ):
        tomb_problem = (
            f"function {TOMBSTONE}: must be class tombstone, `int {TOMBSTONE}(void)`, returning the FIRM constant "
            f"{TOMBSTONE_CONSTANT} = {TOMBSTONE_VALUE} (D1.2)"
        )
    if tomb_problem:
        problems.append(tomb_problem)
        reused = sorted(f.name for f in functions if f.name in (v0raw or {}).get("symbols", {}))
        if reused:
            problems.append(
                f"the description reuses v0 names ({', '.join(reused)}) without the {TOMBSTONE} tombstone: every "
                f"released binding treats a missing {TOMBSTONE} as revision 0 and goes on to call chs_init "
                "(measured by the tombstone sweep), so a reused name would be called through a v0 declaration"
            )
    for fn in functions:
        r = fn.returns
        if r.constant is not None:
            c = constants.get(r.constant)
            if c is None:
                problems.append(f"function {fn.name}: returns the undescribed constant {r.constant!r}")
            elif r.kind != "scalar" or r.type not in ("int", "int32"):
                problems.append(f"function {fn.name}: only an integer return can be a fixed constant")

    # ---- v0 names (D1.3)
    v0: dict[str, list[dict[str, Any]]] = {}
    if v0raw is not None:
        v0 = v0raw.get("symbols", {})
        if not sdk["reuse_v0_names"]:
            for fn in functions:
                old = v0.get(fn.name)
                if not old:
                    continue
                mine = fn.signature_key()
                bad = [s for s in old if (norm_c(s["returns"]), tuple(norm_c(t) for t in s["params"])) != mine]
                if bad:
                    want = f"{mine[0]} ({', '.join(mine[1]) or 'void'})"
                    was = "; ".join(f"{s['returns']} ({', '.join(s['params']) or 'void'})" for s in bad)
                    problems.append(
                        f"function {fn.name}: reuses a v0 name with a different signature (v{major}: {want}; v0: {was}). "
                        f"{sp.sdk_json} reuse_v0_names is false until the tombstone sweep of the released bindings "
                        f"passes (D1.3): rename it in {sp.abi_json}"
                    )

    # ---- the generation: a description states the major it is generated as.
    if raw["abi"] != major:
        problems.append(
            f"{sp.abi_json}: abi is {raw['abi']}, but this is the description of ABI v{major}; the generation "
            "(CHS_ABI_VERSION) and the major are one number"
        )
    if major >= 2:
        problems += generation_rule_problems(root, sp, raw, sdk, docs)

    if problems:
        raise ModelError(problems)

    return Model(
        root=root,
        raw=raw,
        fingerprint=jcs.fingerprint(abi_bytes),
        description_schema=raw["description_schema"],
        abi=raw["abi"],
        library_stem=raw["library"]["stem"],
        prefix=prefix,
        scalars=declared_scalars,
        handles=handles,
        enums=enums,
        constants=constants,
        documents=documents,
        byte_strings=dict(raw["byte_strings"]),
        build_info_schema=bi["schema"],
        build_info_provisional=bi_prov,
        functions=tuple(functions),
        sdk=sdk,
        docs=docs,
        v0=v0,
        major=major,
        stability=raw.get("stability"),
        inputs=inputs,
    )
