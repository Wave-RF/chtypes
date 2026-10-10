"""ts/src/abi1/decls.gen.ts and ts/src/abi1/errmap.gen.ts: the TS binding's
generated view of the ABI description.

THE TS TRAP (plan §3.3) decides this emitter's shape. ffi-rs 1.3.7's only way
to CALL a C function is `define({key: {library, funcName, retType,
paramsType}})` / `load(...)`, resolved by (library key, symbol NAME) — it has
no API to call an arbitrary already-resolved function POINTER (there is no
"call this JsExternal as a function" primitive; `DataType.Function` is for the
other direction, passing a JS callback INTO C). So a loader cannot do
"dlsym this symbol, then call what dlsym returned" through ffi-rs alone, which
is exactly what steps 3-4 of the loader need for `chs_abi_version` and
`chs_build_info` BEFORE the library is known to speak ABI v1 at all.

The resolution (ts/src/abi1/libc.gen.ts, this emitter's third output):
declare `dlopen`/`dlsym`/`dlerror`/`gnu_get_libc_version`/`strlen` (needed for
step 1 and for reading owned/borrowed C strings byte-safely) through ffi-rs
AGAINST THE PROCESS'S OWN LIBC — ordinary, always-resolvable symbols, so
ffi-rs's normal by-name resolution is fine for THEM. Steps 2-6 do their OWN
`dlopen(path, RTLD_NOW|RTLD_LOCAL)` through that libc declaration, which —
unlike ffi-rs's `open()`, which goes through libloading with `RTLD_LAZY |
RTLD_LOCAL` (measured in v0 `ts/src/registry.ts`'s header comment) — eagerly
binds every relocation, so the `unbound` stub variant (an unresolved external
symbol) correctly FAILS to load under this path, on both glibc and darwin.
Once that succeeds, the image is fully resident and bound; only THEN does the
loader `open()` the SAME path through ffi-rs (a harmless re-open of an
already-mapped image, confirmed by dlopen's own refcount-by-path contract)
and declare the full typed call table on it. Steps 3-6 beyond chs_abi_version/
chs_build_info are PRESENCE checks only (`dlsym(handle, name) != NULL`), which
need no typed call at all.

**Why the libc declarations are GENERATED, not hand-written** (lead's ruling,
2026-10-02): `scripts/abi-v1/check-no-hand-decls.py`'s ts rules flag a bare
`dlsym(` call anywhere in a hand-written v1 FFI file — the check's INTENT is
that every raw symbol lookup lives in generated code, exempt by construction
(the generated banner), never worked around with a renamed table key or a
call site shaped to dodge the text match. So `libc.gen.ts` declares
`dlopen`/`dlsym`/`dlerror`/`gnu_get_libc_version`/`strlen` by their real C
names (this file needs no disguise: it IS the generated code the check
expects) and exports `resolveSymbol` — THE one symbol-lookup helper
(`dlsym` plus the null check) — alongside the other four as plain
typed-array-argument functions. Hand-written `ts/src/abi1/libc.ts` calls
only these exports, by name, and never declares or looks up a symbol
itself. `flock` is declared here too, by the same rule, for the fetch layer's
in-use hold (docs/guides/fetch-v1.md §1, "In use"): Node has no file lock of
its own, and the other three bindings take the same flock(2) on the same file.

THE PUBLIC LAYER'S VIEW (wave C). Two further outputs sit over that data, so the
public API never spells a described name or a vocabulary value itself:

  * ts/src/abi1/calls.gen.ts: ONE typed, copy-then-free method per described
    call (a class `Calls` over the resolved table), named without the `chs_`
    prefix (`schemaCreate`, `previewBatch`, ...). Each marshals through the
    generic `rawCall` (below), throws the class sdk.json's status table gives
    for a non-OK status, and returns the owned buffers as `Buffer`s and the
    new handles as wrapped handles; no `chs_buf` or `chs_error` ever escapes.
    The accessors and the frees of the buf and error handles are the generic
    engine's own business and get no method.
  * ts/src/abi1/vocab.gen.ts: every vocabulary, with the description's numbers,
    spellings, facts and fallbacks: `Format`, `Status`, `Outcome`,
    `BatchOutcome`, `FilterOutcome`, `Verdict`, `Reason` (with `lossy`),
    `Source` (with `isStored`), `DefaultKind`, `DiscoverQueryParam`,
    `DocFlags`, and from ABI v2 `MergeReason`. A fact is read by a generated function keyed by the value the
    document carries (`reasonLossy`, `sourceIsStored`, `verdictAnswered`),
    and an unlisted value reads as the vocabulary's own fallback; a
    vocabulary with no fallback answers `undefined`, which the decoder turns
    into an internal error.

SO THE CALL ENGINE ITSELF IS NOT GENERATED PER FUNCTION. Given ffi-rs can only call a
symbol it already knows the NAME of, and every chs_* name is known statically
from spec/abi-v1/abi.json, there is no benefit to generating 38 nearly
identical TS wrapper functions (one per chs_* call) the way emit/stub.py
generates 38 C bodies (the STUB has per-function C semantics to special-case;
a TS CALLER never does — it only marshals). Instead this emitter produces
DATA:

  * `FUNCTION_SPECS`: every function's parameter and return shape, exactly
    model.py's own Param/Return fields, as plain JSON — so hand code can
    build the right ffi-rs paramsType/retType and know how to decode a
    result, for ANY function, from one small generic routine
    (ts/src/abi1/raw.ts's `rawCall`, hand-written, which is the "invoke by
    name" dispatcher the plan names: it takes a chs_* name string and a
    resolved argument array and does the marshal/call/decode generically,
    reading NOTHING about any function except what FUNCTION_SPECS says);
  * `SYMBOL`: every described chs_* name, as a named export
    (`SYMBOL.BUF_FREE === "chs_buf_free"`), so hand code NEVER spells a
    `chs_` identifier itself (the rule this whole file exists to satisfy:
    "the emitter is the only author of chs_* declarations" — the name
    strings, not just a typed signature around them);
  * `CAMEL_NAMES`: chs_* name -> the key ffi-rs's `define()` table uses for
    it (chs_buf_data -> bufData), so two call sites can never spell two
    different keys for the same symbol;
  * `HANDLE_INFO`: per chs_* handle, its free function and the handle kinds
    it `holds` (D2's "a child holds its parents alive"), for
    ts/src/abi1/handles.ts's wrapper classes;
  * `STATUS_NAMES`/`STATUS_VALUES`: the chs_status enum, both directions;
  * `DESCRIBED_SYMBOLS`: every exported symbol, sorted — the loader's step 6
    resolve-all sweep.

errmap.gen.ts is the one place sdk.json's status/class and
loader-refusal/class tables become code: `errorForStatus` (a CALL's
chs_status -> one of SchemaError/UnsupportedError/UsageError/InternalError)
and `loaderErrorClassFor` (a loader refusal's reason -> artifact_incompatible
or artifact_corrupt), both over the hand-written error classes in
ts/src/abi1/errors.ts.

ABI v2 (`MAJORS`, run for the ONE major spec/binding-majors.json gives ts).
The same five files under ts/src/abi2: ABI v1's text with every spelling that
names its major moved by `_MAJOR_SPELLINGS` (the directory, the description
paths, the libc key, the handle base class, the error prefixes), each of which
must occur, so a renamed spelling fails generation instead of leaking a v1
name into v2. What is v2's own:

  * decls.gen.ts carries ABI_STABILITY, the description's `stability`: the
    loader's fingerprint refusal is rule r6's exact dev message while it is
    "unstable" (spec/abi-v2/docs.md);
  * vocab.gen.ts follows rule r3: every vocabulary's type is its listed
    values OR its unknown(n) member, the raw integer or the exact string,
    which `<vocabulary>Of` keeps instead of replacing it with a fallback, and
    `<vocabulary>Known` is false for exactly those values. A fallback only
    names whose facts unknown(n) reports, so an unknown outcome is never
    accepted and an unknown verdict is never answered; a boolean fact with no
    fallback (value_src's is_stored) reads false for unknown(n), as every
    dev binding reads it, until the description says otherwise.
    `statusName` spells an unlisted status `unknown(<n>)`, and
    DESCRIBED_VOCABULARIES lists every enum the description defines, so the
    r3 tests reach each one without a hand-kept list;
  * errmap.gen.ts's unlisted status is an InternalError naming unknown(n).

ABI v1's outputs are produced by the untouched v1 path, byte for byte.
"""

from __future__ import annotations

import json
import re

from model import BUF_HANDLE

from . import Output, banner

BINDING = "ts"  # runs for the ONE major spec/binding-majors.json gives ts (emit/__init__.py)
MAJORS = (1, 2)

DECLS_PATH = "ts/src/abi1/decls.gen.ts"
ERRMAP_PATH = "ts/src/abi1/errmap.gen.ts"
LIBC_PATH = "ts/src/abi1/libc.gen.ts"
VOCAB_PATH = "ts/src/abi1/vocab.gen.ts"
CALLS_PATH = "ts/src/abi1/calls.gen.ts"


def paths(major: int) -> tuple[str, str, str, str, str]:
    """(decls, errmap, libc, vocab, calls) for one major: ts/src/abi<major>/."""
    v1 = (DECLS_PATH, ERRMAP_PATH, LIBC_PATH, VOCAB_PATH, CALLS_PATH)
    if major == 1:
        return v1
    return tuple(p.replace("ts/src/abi1/", f"ts/src/abi{major}/") for p in v1)  # type: ignore[return-value]


# Every spelling of ABI v1's TS layer that names its major, in the order they
# are applied. Each must occur in the rendered text at least once (a spelling
# that moved in the v1 path would otherwise silently stop being moved). The
# banner (each file's first line) is the model's own and already names the
# major; none of these touches it ("scripts/abi-v1/gen.py --major 2" carries
# no "spec/abi-v1/").
_MAJOR_SPELLINGS = (
    ("ts/src/abi1/", "ts/src/abi{n}/"),
    ("spec/abi-v1/", "spec/abi-v{n}/"),
    ("chtypes_abi1_", "chtypes_abi{n}_"),
    ("chtypes abi1:", "chtypes abi{n}:"),
    ("are v1 platforms", "are ABI v{n} platforms"),
    ("Abi1Handle", "Abi{n}Handle"),
)


def _respell(text: str, major: int) -> str:
    for old, new in _MAJOR_SPELLINGS:
        text = text.replace(old, new.replace("{n}", str(major)))
    return text


def _respell_all(texts: list[str], major: int) -> list[str]:
    joined = "\0".join(texts)
    missing = [old for old, _ in _MAJOR_SPELLINGS if old not in joined]
    if missing:
        raise ValueError(f"emit/ts.py: ABI v1's TS layer no longer spells {missing}; update _MAJOR_SPELLINGS")
    return [_respell(x, major) for x in texts]


_WORD = re.compile(r"[A-Za-z0-9]+")


def _strip_prefix(name: str, prefix: str) -> str:
    if not name.startswith(prefix):
        raise ValueError(f"{name!r} does not start with {prefix!r}")
    return name[len(prefix) :]


def camel_name(prefix: str, name: str) -> str:
    """chs_buf_data -> bufData (the ffi-rs define() table key)."""
    words = _WORD.findall(_strip_prefix(name, prefix))
    if not words:
        raise ValueError(f"{name!r}: nothing left after stripping {prefix!r}")
    return words[0].lower() + "".join(w.capitalize() for w in words[1:])


def const_name(prefix: str, name: str) -> str:
    """chs_buf_data -> BUF_DATA (a SYMBOL.* key)."""
    words = _WORD.findall(_strip_prefix(name, prefix))
    return "_".join(w.upper() for w in words)


def pascal_name(prefix: str, name: str) -> str:
    """chs_buf -> Buf (a handle wrapper class's base name)."""
    c = camel_name(prefix, name)
    return c[0].upper() + c[1:]


def _param_spec(p) -> dict:
    return {
        "name": p.name,
        "kind": p.kind,
        "type": p.type,
        "nullable": p.nullable,
        "content": p.content,
    }


def _return_spec(r) -> dict:
    return {
        "kind": r.kind,
        "type": r.type,
        "owned": r.owned,
        "nullable": r.nullable,
        "content": r.content,
        "borrows": r.borrows,
        "constant": r.constant,
    }


def function_specs(model) -> dict:
    out: dict[str, dict] = {}
    for fn in model.functions:
        out[fn.name] = {
            "cls": fn.cls,
            "thread": fn.thread,
            "params": [_param_spec(p) for p in fn.params],
            "returns": _return_spec(fn.returns),
            "mayReturn": list(fn.may_return),
        }
    return out


def symbol_table(model) -> dict:
    return {const_name(model.prefix, fn.name): fn.name for fn in model.functions}


def camel_table(model) -> dict:
    return {fn.name: camel_name(model.prefix, fn.name) for fn in model.functions}


def handle_info(model) -> dict:
    return {
        name: {"free": h.free, "holds": list(h.holds), "class": pascal_name(model.prefix, name)}
        for name, h in model.handles.items()
    }


def cross_check_table(model) -> list[dict]:
    return [
        {"buildInfo": f["build_info"], "predicate": f["predicate"], "compare": f["compare"]}
        for f in model.sdk["cross_check"]
    ]


def status_tables(model) -> tuple[dict, dict]:
    status = model.enums["chs_status"]
    names: dict[int, str] = {}
    values: dict[str, int] = {}
    for v in status.values:
        assert v.name is not None
        names[v.value] = v.name
        values[v.name] = v.value
    return names, values


def _json_block(obj) -> str:
    """Deterministic JSON, embeddable directly as a TS object/array literal
    (valid JSON is valid TS). Sorted keys: FUNCTION_SPECS/SYMBOL/etc. are
    looked up by name, never iterated for order, except DESCRIBED_SYMBOLS,
    which is built pre-sorted as a JSON array (order survives)."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True)


def render_decls(model) -> str:
    specs = function_specs(model)
    symbols = symbol_table(model)
    camel = camel_table(model)
    handles = handle_info(model)
    status_names, status_values = status_tables(model)
    described = model.symbols()  # sorted, per model.Model.symbols()
    cross_check = cross_check_table(model)

    parts: list[str] = [
        f"/* {banner(model)} */",
        "/*",
        " * The ABI description as DATA: every described symbol's name, its parameter and return",
        " * shape, and the chs_status enum, both directions. See this emitter's module docstring",
        " * (scripts/abi-v1/emit/ts.py) for why this file carries no ffi-rs declare() calls: those",
        " * live in the hand-written ts/src/abi1/raw.ts, which is driven entirely by this data and",
        " * therefore never hand-spells a chs_* name of its own.",
        " *",
        " * SYMBOL.<NAME> is the canonical way hand code refers to a described function by name",
        " * without ever writing the literal `chs_` prefix itself: `rawCall(raw, SYMBOL.BUF_FREE, ...)`.",
        " * CAMEL_NAMES maps the same chs_* name to the key ts/src/abi1/raw.ts's define() table uses.",
        " */",
        "",
        "export type ParamKind = 'scalar' | 'enum' | 'bytes_in' | 'handle' | 'out_handle' | 'out_error' | 'out_scalar';",
        "export type ReturnKind = 'status' | 'void' | 'scalar' | 'enum' | 'handle';",
        "",
        "export interface ParamSpec {",
        "  readonly name: string;",
        "  readonly kind: ParamKind;",
        "  readonly type: string | null;",
        "  readonly nullable: boolean;",
        "  readonly content: string | null;",
        "}",
        "",
        "export interface ReturnSpec {",
        "  readonly kind: ReturnKind;",
        "  readonly type: string | null;",
        "  readonly owned: boolean;",
        "  readonly nullable: boolean;",
        "  readonly content: string | null;",
        "  readonly borrows: string | null;",
        "  readonly constant: string | null;",
        "}",
        "",
        "export interface FunctionSpec {",
        "  readonly cls: 'handshake' | 'tombstone' | 'api' | 'tooling';",
        "  readonly thread: string;",
        "  readonly params: readonly ParamSpec[];",
        "  readonly returns: ReturnSpec;",
        "  readonly mayReturn: readonly string[];",
        "}",
        "",
        "/** Every described function's shape, keyed by its chs_* name. */",
        f"export const FUNCTION_SPECS: Readonly<Record<string, FunctionSpec>> = {_json_block(specs)} as const;",
        "",
        "/** chs_* name -> the key ts/src/abi1/raw.ts's ffi-rs define() table uses for it. */",
        f"export const CAMEL_NAMES: Readonly<Record<string, string>> = {_json_block(camel)} as const;",
        "",
        "/**",
        " * Every described chs_* name, reachable WITHOUT spelling it: hand code writes",
        " * `SYMBOL.BUF_FREE`, never the string `\"chs_buf_free\"`. Generated so the one place that",
        " * spells a chs_* name is this file, machine-checked against spec/abi-v1/abi.json.",
        " *",
        " * Deliberately NOT typed `Record<string, string>`: each key's value is its own string",
        " * LITERAL type, so a known key (`SYMBOL.BUF_FREE`) is never widened to `string | undefined`",
        " * by `noUncheckedIndexedAccess` the way an index signature would be. Nothing here is ever",
        " * looked up with a variable key — `CAMEL_NAMES` and `FUNCTION_SPECS` are, and keep their",
        " * `Record` types for that reason.",
        " */",
        f"export const SYMBOL = {_json_block(symbols)} as const;",
        "",
        "export interface HandleInfo {",
        "  readonly free: string;",
        "  readonly holds: readonly string[];",
        "  readonly className: string;",
        "}",
        "",
        "/** Every described handle kind: its free function (a SYMBOL.* value) and what it holds. */",
        "export const HANDLE_INFO: Readonly<Record<string, HandleInfo>> = "
        + _json_block({name: {"free": h["free"], "holds": h["holds"], "className": h["class"]} for name, h in handles.items()})
        + " as const;",
        "",
        "/** chs_status, both directions (the wire int32 and its C name). */",
        f"export const STATUS_NAMES: Readonly<Record<number, string>> = {_json_block(status_names)} as const;",
        f"export const STATUS_VALUES: Readonly<Record<string, number>> = {_json_block(status_values)} as const;",
        "",
        "/** Every exported symbol, sorted — the loader's step 6 resolve-all sweep. */",
        f"export const DESCRIBED_SYMBOLS: readonly string[] = {_json_block(described)};",
        "",
        "/**",
        " * The buf handle's own chs_* name (D2/D3's universal owned-output type), named so hand",
        " * code can compare `ParamSpec.type`/`ReturnSpec.type` against it without ever spelling",
        " * the literal itself (a literal would read as a hand-written chs_ reference to",
        " * scripts/abi-v1/check-no-hand-decls.py once this file's own `define(`-adjacent code also",
        " * names it — see ts/src/abi1/raw.ts's module comment).",
        " */",
        f"export const BUF_HANDLE: {BUF_HANDLE!r} = {BUF_HANDLE!r};",
        "",
        "/**",
        " * This binding's own compiled-in ABI identity (D1.1/§1.7's \"one computation, copied",
        " * everywhere else\" — never recomputed, byte-compared against a loaded library's",
        " * chs_build_info().abi_fingerprint at loader step 4).",
        " */",
        f"export const ABI_VERSION = {json.dumps(model.abi)};",
        f"export const ABI_FINGERPRINT = {json.dumps(model.fingerprint)};",
        *(
            [
                "/**",
                " * The description's stability: \"unstable\" while this generation is being designed, so",
                " * ABI_FINGERPRINT moves with every change, and \"locked\" after (spec/abi-v1/docs.md, rule",
                " * r6). A dev SDK refuses any other fingerprint with the dev message.",
                " */",
                f"export const ABI_STABILITY: string = {json.dumps(model.stability or '')};",
            ]
            if model.major >= 2
            else []
        ),
        "",
        "export interface CrossCheckField {",
        "  readonly buildInfo: string;",
        "  readonly predicate: string;",
        "  readonly compare: 'bytes' | 'int';",
        "}",
        "",
        "/** spec/abi-v1/sdk.json's cross_check table: loader step 5's nine fields. */",
        f"export const CROSS_CHECK: readonly CrossCheckField[] = {_json_block(cross_check)};",
        "",
    ]
    return "\n".join(parts)


def render_errmap(model) -> str:
    status_map: dict[str, str | None] = model.sdk["errors"]["status"]
    class_names: dict[str, dict] = model.sdk["errors"]["classes"]
    loader_refusals: list[dict] = model.sdk["loader"]["refusals"]
    _, status_values = status_tables(model)

    ts_class_of = {cls: info["ts"] for cls, info in class_names.items()}

    lines: list[str] = [
        f"/* {banner(model)} */",
        "/*",
        " * spec/abi-v1/sdk.json's two error tables, as code: a CALL's chs_status -> one of",
        " * SchemaError/UnsupportedError/UsageError/InternalError (errors.status), and a LOADER",
        " * refusal's reason -> artifact_incompatible or artifact_corrupt (loader.refusals). Both",
        " * read spec/abi-v1/sdk.json; regenerate rather than hand-editing either table.",
        " */",
        "",
        "import {",
    ]
    # One sorted-by-name list (value and type imports interleaved): biome's
    # import-sort assist orders named specifiers alphabetically regardless of
    # the `type` modifier, and this is a generated, banner-checked file, so
    # it must already satisfy that sort rather than relying on `--write`
    # (biome has no opinion on a generated file's CONTENT, only its own
    # formatting of whatever text is there).
    classes_used = {ts_class_of[c] for c in status_map.values() if c is not None} | {"InternalError"}
    specifiers = sorted([(c, False) for c in classes_used] + [("CallErrorFields", True), ("ChtypesError", True)])
    for name, is_type in specifiers:
        lines.append(f"  {'type ' if is_type else ''}{name},")
    lines += [
        "} from './errors.js';",
        "",
        "/**",
        " * A call's raw chs_status value -> the mapped error instance (spec/abi-v1/sdk.json's",
        " * errors.status table). A caller checks for the OK value before ever calling this. A",
        " * status outside the closed set (impossible under a matching fingerprint; see",
        " * spec/abi-v1/docs.md's chs_status section) maps to InternalError, naming the raw",
        " * value, exactly like every other binding's generated table.",
        " */",
        "export function errorForStatus(status: number, fields: CallErrorFields): ChtypesError {",
        "  switch (status) {",
    ]
    for status, cls in sorted(status_map.items(), key=lambda kv: status_values.get(kv[0], -1)):
        if status in ("CHS_OK", "unknown") or cls is None:
            continue
        lines.append(f"    case {status_values[status]}: // {status}")
        lines.append(f"      return new {ts_class_of[cls]}(fields);")
    if model.major >= 2:
        # Rule r3: a status outside the closed set is its unknown(n), and the
        # call still fails, as an internal error naming n (sdk.json's
        # errors.status "unknown" entry).
        unknown_message = "`chtypes: call status unknown(${status}) is outside the closed set: ${fields.messageBytes.toString('utf8')}`,"
    else:
        unknown_message = "`chtypes: unrecognized call status ${status}: ${fields.messageBytes.toString('utf8')}`,"
    lines += [
        "    default:",
        "      return new InternalError({",
        "        ...fields,",
        "        messageBytes: Buffer.from(",
        f"          {unknown_message}",
        "          'utf8',",
        "        ),",
        "      });",
        "  }",
        "}",
        "",
        "export type LoaderErrorClass = 'artifact_incompatible' | 'artifact_corrupt';",
        "",
        "/**",
        " * A loader refusal reason (spec/abi-v1/sdk.json's loader.refusals vocabulary, a",
        " * `missing_symbol:<name>` or `build_info_mismatch:<field>` reason included, via its",
        " * prefix before the first ':') -> which artifact error class the C ABI contract §Loading",
        " * error map says it is.",
        " */",
        "export function loaderErrorClassFor(reason: string): LoaderErrorClass {",
        "  const prefix = reason.split(':', 1)[0];",
        "  switch (prefix) {",
    ]
    seen_reasons: set[str] = set()
    for refusal in loader_refusals:
        reason = refusal["reason"]
        if reason in seen_reasons:
            continue
        seen_reasons.add(reason)
        lines.append(f"    case {reason!r}:")
        lines.append(f"      return {refusal['error']!r};")
    lines += [
        "    default:",
        "      throw new Error(`chtypes abi1: no sdk.json loader refusal class for reason ${reason}`);",
        "  }",
        "}",
        "",
    ]
    return "\n".join(lines)


def render_libc(model) -> str:
    """ts/src/abi1/libc.gen.ts: the libc declarations plus the one
    symbol-lookup helper, per the lead's 2026-10-02 ruling — see this
    module's docstring for why these live here rather than in the
    hand-written loader. Static content: nothing here reads `model` beyond
    the banner/fingerprint, but it is generated, not hand-written, by the
    same rule check-no-hand-decls.py enforces everywhere else.
    """
    lines = [
        f"/* {banner(model)} */",
        "/*",
        " * The libc primitives ts/src/abi1/libc.ts needs for plan §3.3's TS-trap fix: dlopen,",
        " * dlsym, dlerror, gnu_get_libc_version and strlen, declared through ffi-rs's define()",
        " * against the PROCESS'S OWN libc, by their real C names. This file carries the generated",
        " * banner, so scripts/abi-v1/check-no-hand-decls.py exempts it outright — the check's own",
        " * intent (every raw symbol lookup lives in generated code) is satisfied for real here,",
        " * never worked around with a renamed key or a disguised call site in a hand file. See",
        " * scripts/abi-v1/emit/ts.py's module docstring.",
        " *",
        " * `resolveSymbol` is THE ONE symbol-lookup helper hand code uses (dlsym plus the null",
        " * check); the other four exports are the raw primitives dlopen/dlerror/gnu_get_libc_version/",
        " * strlen need, each with its own null handling where the C function can return NULL.",
        " * `flock` is the fetch layer's in-use hold (docs/guides/fetch-v1.md §1, \"In use\"): Node has",
        " * no file lock of its own, and every binding takes the same flock(2) on the same file.",
        " */",
        "",
        "import { DataType, define, isNullPointer, type JsExternal, open } from 'ffi-rs';",
        "",
        "const { External, I32, U64, String: Str } = DataType;",
        "",
        "const LIBC_KEY = 'chtypes_abi1_libc';",
        "",
        "function libcPath(): string {",
        "  if (process.platform === 'darwin') return '/usr/lib/libSystem.B.dylib';",
        "  if (process.platform === 'linux') return 'libc.so.6';",
        "  throw new Error(`chtypes abi1: unsupported platform ${process.platform}; only linux and darwin are v1 platforms`);",
        "}",
        "",
        "let opened = false;",
        "function ensureOpen(): void {",
        "  if (opened) return;",
        "  open({ library: LIBC_KEY, path: libcPath() });",
        "  opened = true;",
        "}",
        "",
        "interface LibcFns {",
        "  dlopen(args: [string, number]): JsExternal;",
        "  dlsym(args: [JsExternal, string]): JsExternal;",
        "  dlerror(args: []): JsExternal;",
        "  gnu_get_libc_version(args: []): JsExternal;",
        "  strlen(args: [JsExternal]): bigint;",
        "  flock(args: [number, number]): { value: number; errnoCode: number; errnoMessage: string };",
        "}",
        "",
        "let fns: LibcFns | null = null;",
        "function libc(): LibcFns {",
        "  ensureOpen();",
        "  if (fns === null) {",
        "    fns = define({",
        "      dlopen: { library: LIBC_KEY, retType: External, paramsType: [Str, I32] },",
        "      dlsym: { library: LIBC_KEY, retType: External, paramsType: [External, Str] },",
        "      dlerror: { library: LIBC_KEY, retType: External, paramsType: [] },",
        "      gnu_get_libc_version: { library: LIBC_KEY, retType: External, paramsType: [] },",
        "      strlen: { library: LIBC_KEY, retType: U64, paramsType: [External] },",
        "      flock: { library: LIBC_KEY, retType: I32, paramsType: [I32, I32], errno: true },",
        "    }) as unknown as LibcFns;",
        "  }",
        "  return fns;",
        "}",
        "",
        "/** dlopen(path, flags) against the process's own libc. Null on failure. */",
        "export function dlopen(path: string, flags: number): JsExternal | null {",
        "  const h = libc().dlopen([path, flags]);",
        "  return isNullPointer(h) ? null : h;",
        "}",
        "",
        "/** THE one symbol-lookup helper: dlsym(handle, name). Null if `name` is not exported by `handle`'s image. */",
        "export function resolveSymbol(handle: JsExternal, name: string): JsExternal | null {",
        "  const p = libc().dlsym([handle, name]);",
        "  return isNullPointer(p) ? null : p;",
        "}",
        "",
        "/** dlerror(). Null if there is no pending error (POSIX clears it on read, so call this ONCE, right after a failure). */",
        "export function dlerror(): JsExternal | null {",
        "  const p = libc().dlerror([]);",
        "  return isNullPointer(p) ? null : p;",
        "}",
        "",
        "/** gnu_get_libc_version(). Null if the symbol does not resolve at all (musl) or the call itself returns null. */",
        "export function gnuGetLibcVersion(): JsExternal | null {",
        "  try {",
        "    const p = libc().gnu_get_libc_version([]);",
        "    return isNullPointer(p) ? null : p;",
        "  } catch {",
        "    return null;",
        "  }",
        "}",
        "",
        "/** strlen(ptr): the length of a NUL-terminated C string, for a byte-safe read via createExternalBuffer. */",
        "export function cStringLength(ptr: JsExternal): number {",
        "  return Number(libc().strlen([ptr]));",
        "}",
        "",
        "/** flock(fd, operation) against the process's own libc: 0 when it succeeded, else the errno it failed with. */",
        "export function flock(fd: number, operation: number): number {",
        "  const r = libc().flock([fd, operation]);",
        "  return r.value === 0 ? 0 : r.errnoCode;",
        "}",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------- vocabularies

# The TS spelling of each vocabulary's type name, keyed by the description's
# own enum name. An enum this table does not name falls back to the PascalCase
# of its name (prefix stripped), so a new vocabulary is generated rather than
# dropped; only a value's NAME ever needs a table, and only where the value
# itself is not a word (a one-letter verdict, the empty default kind).
TS_VOCAB_NAMES = {
    "chs_status": "Status",
    "chs_format": "Format",
    "row_outcome": "Outcome",
    "batch_outcome": "BatchOutcome",
    "filter_outcome": "FilterOutcome",
    "filter_verdict": "Verdict",
    "transform_reason": "Reason",
    "value_src": "Source",
    "default_kind": "DefaultKind",
    "discover_query_param": "DiscoverQueryParam",
    "merge_reason": "MergeReason",
}
TS_VALUE_NAMES = {
    "filter_verdict": {"t": "True", "f": "False", "e": "Error", "d": "Decline"},
    "default_kind": {"": "None"},
}
DOC_PREFIX = "CHS_DOC_"


def _pascal_words(text: str) -> str:
    return "".join(w[:1].upper() + w[1:].lower() for w in _WORD.findall(text))


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def ts_vocab_type(enum_name: str) -> str:
    return TS_VOCAB_NAMES.get(enum_name) or _pascal_words(enum_name.removeprefix("chs_"))


def ts_value_name(enum_name: str, value) -> str:
    override = TS_VALUE_NAMES.get(enum_name, {})
    if value in override:
        return override[value]
    name = _pascal_words(str(value))
    if not name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name):
        raise ValueError(f"emit/ts.py: no TS name for value {value!r} of {enum_name}; add one to TS_VALUE_NAMES")
    return name


def _ts_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _render_int_enum(enum, type_name: str) -> list[str]:
    keys: dict[str, int] = {}
    for v in enum.values:
        if enum.name == "chs_format":
            key = v.fields["ch_name"]
        else:
            assert v.name is not None
            key = _pascal_words(v.name.removeprefix("CHS_"))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) or key in keys:
            raise ValueError(f"emit/ts.py: {enum.name}: cannot spell {v.name!r} as a TS key ({key!r})")
        keys[key] = int(v.value)
    out = [
        f"/** {enum.name}: the description's own numbers. */",
        f"export const {type_name} = {{",
        *[f"  {k}: {v}," for k, v in keys.items()],
        "} as const;",
        f"export type {type_name} = (typeof {type_name})[keyof typeof {type_name}];",
        "",
    ]
    if enum.name == "chs_format":
        names = {int(v.value): v.fields["ch_name"] for v in enum.values}
        out += [
            "/** Each format's ClickHouse name, which is how `capabilities` lists one. */",
            "export const FORMAT_CH_NAME: Readonly<Record<number, string>> = {",
            *[f"  {n}: {_ts_str(c)}," for n, c in names.items()],
            "};",
            "",
            "export function formatChName(format: number): string | undefined {",
            "  return FORMAT_CH_NAME[format];",
            "}",
            "",
        ]
    return out


def _render_string_vocab(enum) -> list[str]:
    type_name = ts_vocab_type(enum.name)
    upper = re.sub(r"(?<!^)(?=[A-Z])", "_", type_name).upper()
    entries = {}
    names: dict[str, str] = {}
    for v in enum.values:
        key = ts_value_name(enum.name, v.value)
        if key in names:
            raise ValueError(f"emit/ts.py: {enum.name}: {v.value!r} and {names[key]!r} both spell {key}")
        names[key] = v.value
        entries[v.value] = dict(v.fields)
    out = [
        f"/** {enum.name}: the description's own values. */",
        f"export const {type_name} = {{",
        *[f"  {k}: {_ts_str(v)}," for k, v in names.items()],
        "} as const;",
        f"export type {type_name} = (typeof {type_name})[keyof typeof {type_name}];",
        "",
        f"const {upper}_ENTRIES: Readonly<Record<string, Readonly<Record<string, boolean | string>>>> = {{",
        *[f"  {_ts_str(v)}: {json.dumps(f, sort_keys=True)}," for v, f in entries.items()],
        "};",
        f"/** The description's fallback for a value it does not list, or null when it names none. */",
        f"export const {upper}_FALLBACK: {type_name} | null = {_ts_str(enum.fallback) if enum.fallback is not None else 'null'};",
        "",
        f"/** The value when listed, else the description's fallback, else undefined (the decoder's internal error). */",
        f"export function {_lower_first(type_name)}Of(value: string): {type_name} | undefined {{",
        f"  if (Object.hasOwn({upper}_ENTRIES, value)) return value as {type_name};",
        f"  return {upper}_FALLBACK ?? undefined;",
        "}",
        "",
    ]
    for field, kind in enum.fields.items():
        ts_t = "boolean" if kind == "boolean" else "string"
        fn = f"{_lower_first(type_name)}{_pascal_words(field)}"
        out += [
            f"/** The `{field}` fact for a value, read from the description; an unlisted value reads as the fallback's. */",
            f"export function {fn}(value: string): {ts_t} | undefined {{",
            f"  const key = Object.hasOwn({upper}_ENTRIES, value) ? value : {upper}_FALLBACK;",
            "  if (key === null) return undefined;",
            f"  return {upper}_ENTRIES[key]?.[{_ts_str(field)}] as {ts_t} | undefined;",
            "}",
            "",
        ]
    return out


def _render_int_enum_v2(enum, type_name: str) -> list[str]:
    """ABI v2 (rule r3): the listed numbers, plus unknown(n), any other integer."""
    out = _render_int_enum(enum, type_name)
    fn = _lower_first(type_name)
    upper = re.sub(r"(?<!^)(?=[A-Z])", "_", type_name).upper()
    values = [int(v.value) for v in enum.values]
    # The v1 type line names the listed values only; v2's adds unknown(n).
    old = f"export type {type_name} = (typeof {type_name})[keyof typeof {type_name}];"
    new = (
        f"/** {enum.name}: a listed value, or its unknown(n) member, any other integer (rule r3); `{fn}Known` tells them apart. */\n"
        f"export type {type_name} = (typeof {type_name})[keyof typeof {type_name}] | Unknown<number>;"
    )
    if old not in out:
        raise ValueError(f"emit/ts.py: {enum.name}: the type line moved; update _render_int_enum_v2")
    out[out.index(old)] = new
    out += [
        f"const {upper}_LISTED: ReadonlySet<number> = new Set([{', '.join(str(v) for v in values)}]);",
        "",
        "/** Whether the description lists `value`: false for exactly the vocabulary's unknown(n) members (rule r3). */",
        f"export function {fn}Known(value: number): boolean {{",
        f"  return {upper}_LISTED.has(value);",
        "}",
        "",
        "/** The value as this vocabulary's: a listed value, or its unknown(n) member carrying the raw integer (rule r3). */",
        f"export function {fn}Of(value: number): {type_name} {{",
        "  return value;",
        "}",
        "",
    ]
    if enum.name == "chs_status":
        names = {int(v.value): v.name for v in enum.values}
        out += [
            "const STATUS_NAME: Readonly<Record<number, string>> = {",
            *[f"  {n}: {_ts_str(s)}," for n, s in names.items()],
            "};",
            "",
            "/** The chs_status constant's own name (CHS_REJECTED, ...), or `unknown(<n>)` for a value the description does not list (rule r3). */",
            "export function statusName(value: number): string {",
            "  return STATUS_NAME[value] ?? `unknown(${value})`;",
            "}",
            "",
        ]
    return out


def _render_string_vocab_v2(enum) -> list[str]:
    """ABI v2 (rule r3): the listed spellings, plus unknown(n), any other
    string, kept verbatim; a fallback names only whose facts it reports."""
    type_name = ts_vocab_type(enum.name)
    upper = re.sub(r"(?<!^)(?=[A-Z])", "_", type_name).upper()
    fn = _lower_first(type_name)
    entries = {}
    names: dict[str, str] = {}
    for v in enum.values:
        key = ts_value_name(enum.name, v.value)
        if key in names:
            raise ValueError(f"emit/ts.py: {enum.name}: {v.value!r} and {names[key]!r} both spell {key}")
        names[key] = v.value
        entries[v.value] = dict(v.fields)
    out = [
        f"/** {enum.name}: the description's own values. */",
        f"export const {type_name} = {{",
        *[f"  {k}: {_ts_str(v)}," for k, v in names.items()],
        "} as const;",
        f"/** {enum.name}: a listed value, or its unknown(n) member, any other string, kept verbatim (rule r3); `{fn}Known` tells them apart. */",
        f"export type {type_name} = (typeof {type_name})[keyof typeof {type_name}] | Unknown<string>;",
        "",
        f"const {upper}_ENTRIES: Readonly<Record<string, Readonly<Record<string, boolean | string>>>> = {{",
        *[f"  {_ts_str(v)}: {json.dumps(f, sort_keys=True)}," for v, f in entries.items()],
        "};",
        "/** Whose per-value facts an unknown(n) value reports (rule r3), or null when the description names none. Never the value an unlisted one is read as. */",
        f"export const {upper}_FALLBACK: {type_name} | null = {_ts_str(enum.fallback) if enum.fallback is not None else 'null'};",
        "",
        "/** Whether the description lists `value`: false for exactly the vocabulary's unknown(n) members (rule r3). */",
        f"export function {fn}Known(value: string): boolean {{",
        f"  return Object.hasOwn({upper}_ENTRIES, value);",
        "}",
        "",
        "/** The value as this vocabulary's: a listed value, or its unknown(n) member carrying the raw string (rule r3). Never a substitute. */",
        f"export function {fn}Of(value: string): {type_name} {{",
        "  return value;",
        "}",
        "",
    ]
    for field, kind in enum.fields.items():
        fact = f"{fn}{_pascal_words(field)}"
        if kind == "boolean":
            if enum.fallback is not None:
                unknown = f"an unknown(n) value reports the fallback's ({_ts_str(enum.fallback)})"
            else:
                unknown = "an unknown(n) value reads false: the description names no fallback, and every dev binding reads it so until it does (rule r3)"
            out += [
                f"/** The `{field}` fact for a value, read from the description; {unknown}. */",
                f"export function {fact}(value: string): boolean {{",
                f"  const key = Object.hasOwn({upper}_ENTRIES, value) ? value : {upper}_FALLBACK;",
                "  if (key === null) return false;",
                f"  return {upper}_ENTRIES[key]?.[{_ts_str(field)}] === true;",
                "}",
                "",
            ]
        else:
            out += [
                f"/** The `{field}` fact for a value, read from the description; an unknown(n) value reports the fallback's, or undefined when there is none (rule r3). */",
                f"export function {fact}(value: string): string | undefined {{",
                f"  const key = Object.hasOwn({upper}_ENTRIES, value) ? value : {upper}_FALLBACK;",
                "  if (key === null) return undefined;",
                f"  return {upper}_ENTRIES[key]?.[{_ts_str(field)}] as string | undefined;",
                "}",
                "",
            ]
    return out


def _render_described(model) -> list[str]:
    """ABI v2: every enum the description defines, for the r3 tests."""
    rows = []
    for enum in model.enums.values():
        type_name = ts_vocab_type(enum.name)
        fn = _lower_first(type_name)
        if enum.repr == "int32":
            listed = ", ".join(str(int(v.value)) for v in enum.values)
            rows.append(f"  {_ts_str(enum.name)}: {{ repr: 'int32', listed: [{listed}], known: {fn}Known, of: {fn}Of }},")
        elif enum.repr == "string":
            listed = ", ".join(_ts_str(v.value) for v in enum.values)
            rows.append(f"  {_ts_str(enum.name)}: {{ repr: 'string', listed: [{listed}], known: {fn}Known, of: {fn}Of }},")
        else:
            raise ValueError(f"emit/ts.py: {enum.name}: no TS shape for repr {enum.repr!r}")
    return [
        "/** One described enum, as the rule r3 tests reach it: its listed values, `known` and `of`. */",
        "export type DescribedVocabulary =",
        "  | { readonly repr: 'int32'; readonly listed: readonly number[]; readonly known: (value: number) => boolean; readonly of: (value: number) => number }",
        "  | { readonly repr: 'string'; readonly listed: readonly string[]; readonly known: (value: string) => boolean; readonly of: (value: string) => string };",
        "",
        "/** Every enum the description defines, keyed by its own name, generated so no list is kept by hand. */",
        "export const DESCRIBED_VOCABULARIES: Readonly<Record<string, DescribedVocabulary>> = {",
        *rows,
        "};",
        "",
    ]


def render_vocab(model) -> str:
    if model.major >= 2:
        return render_vocab_v2(model)
    lines = [
        f"/* {banner(model)} */",
        "/*",
        " * Every vocabulary the description defines, with its numbers, spellings, facts and",
        " * fallbacks. A fact (a reason's `lossy`, a source's `isStored`, a verdict's `answered`)",
        " * is read by the generated function keyed by the value a document carries; no binding",
        " * keeps a list of its own.",
        " */",
        "",
    ]
    for enum in model.int_enums:
        lines += _render_int_enum(enum, ts_vocab_type(enum.name))
    for enum in model.vocabularies:
        lines += _render_string_vocab(enum)
    docs = {k: c for k, c in model.constants.items() if k.startswith(DOC_PREFIX)}
    lines += [
        "/** The document groups a batch call asks for (`CHS_DOC_*`), as bit flags. */",
        "export const DocFlags = {",
        *[f"  {_pascal_words(k.removeprefix(DOC_PREFIX))}: {c.value}," for k, c in docs.items()],
        "} as const;",
        "export type DocFlags = number;",
        "",
    ]
    export_none = model.constants.get("CHS_EXPORT_NONE")
    if export_none is not None:
        lines += [
            "/** The `export_format` of a batch preview that exports nothing. */",
            f"export const EXPORT_NONE = {export_none.value};",
            "",
        ]
    return "\n".join(lines)


def render_vocab_v2(model) -> str:
    """ABI v2's vocab.gen.ts: rule r3 (this module's docstring)."""
    lines = [
        f"/* {banner(model)} */",
        "/*",
        " * Every vocabulary the description defines, with its numbers, spellings, facts and",
        " * fallbacks. A fact (a reason's `lossy`, a source's `isStored`, a verdict's `answered`)",
        " * is read by the generated function keyed by the value a document carries; no binding",
        " * keeps a list of its own.",
        " *",
        " * Rule r3 (spec/abi-v2/docs.md): every vocabulary has an unknown(n) member. A value the",
        " * description does not list IS that member, the raw integer or the exact string, kept for",
        " * that field alone: `<vocabulary>Of` never replaces it, and `<vocabulary>Known` is false for",
        " * it. A reader never fails a document, a row or a batch over one. A fallback names only",
        " * whose facts unknown(n) reports, so an unknown outcome is never accepted and an unknown",
        " * verdict is never answered.",
        " */",
        "",
        "/** Rule r3's unknown(n) member of a vocabulary: a raw value the description does not list, carried verbatim. */",
        "export type Unknown<T extends number | string> = T & Record<never, never>;",
        "",
    ]
    for enum in model.int_enums:
        lines += _render_int_enum_v2(enum, ts_vocab_type(enum.name))
    for enum in model.vocabularies:
        lines += _render_string_vocab_v2(enum)
    docs = {k: c for k, c in model.constants.items() if k.startswith(DOC_PREFIX)}
    lines += [
        "/** The document groups a batch call asks for (`CHS_DOC_*`), as bit flags. */",
        "export const DocFlags = {",
        *[f"  {_pascal_words(k.removeprefix(DOC_PREFIX))}: {c.value}," for k, c in docs.items()],
        "} as const;",
        "export type DocFlags = number;",
        "",
    ]
    export_none = model.constants.get("CHS_EXPORT_NONE")
    if export_none is not None:
        lines += [
            "/** The `export_format` of a batch preview that exports nothing. */",
            f"export const EXPORT_NONE = {export_none.value};",
            "",
        ]
    lines += _render_described(model)
    return "\n".join(lines)


# ----------------------------------------------------------- typed call wrappers


def _ts_param_name(name: str) -> str:
    words = [w for w in name.split("_") if w]
    return words[0] + "".join(w[:1].upper() + w[1:] for w in words[1:])


def _handle_type_name(model, kind: str) -> str:
    return f"{pascal_name(model.prefix, kind)}Handle"


def _wrappable(model, fn) -> bool:
    if fn.cls in ("handshake", "tombstone", "tooling"):
        return False
    if fn.returns.kind != "status":
        return False
    hidden = {model.handles[k].name for k in ("chs_buf", "chs_error") if k in model.handles}
    return not any(p.kind == "handle" and p.type in hidden for p in fn.params)


def render_calls(model) -> str:
    owned_handles = [k for k in model.handles if k not in (BUF_HANDLE, "chs_error")]
    lines = [
        f"/* {banner(model)} */",
        "/*",
        " * One typed, copy-then-free method per described call, named without the `chs_` prefix.",
        " * Each marshals through the generic `rawCall`, throws the error class sdk.json's status",
        " * table gives for a non-OK status, returns every owned buffer as a Buffer (already copied",
        " * and freed) and every new handle wrapped. No raw buffer or error handle ever escapes.",
        " * The accessors and frees of those two kinds, and the handshake, tombstone and tooling",
        " * exports, get no method: the generic engine and the loader own them.",
        " */",
        "",
        "import { SYMBOL } from './decls.gen.js';",
        "import { type Abi1Handle, wrapHandle } from './handles.js';",
        "import { asBuffer, checkStatus, type HandleRef, NULL_EXTERNAL, type RawApi, rawCall } from './raw.js';",
        "",
    ]
    for kind in owned_handles:
        lines.append(f"export type {_handle_type_name(model, kind)} = Abi1Handle & {{ readonly kind: {_ts_str(kind)} }};")
    lines += ["", "export class Calls {", "  readonly raw: RawApi;", "", "  constructor(raw: RawApi) {", "    this.raw = raw;", "  }", ""]
    for fn in model.functions:
        if not _wrappable(model, fn):
            continue
        method = camel_name(model.prefix, fn.name)
        sym = const_name(model.prefix, fn.name)
        ins = [p for p in fn.params if not p.is_out]
        outs = [p for p in fn.params if p.kind == "out_handle"]
        sig, args = [], []
        for p in ins:
            n = _ts_param_name(p.name)
            if p.kind in ("scalar", "enum"):
                sig.append(f"{n}: number")
                args.append(n)
            elif p.kind == "bytes_in":
                sig.append(f"{n}: Uint8Array")
                args.append(f"asBuffer({n})")
            elif p.kind == "handle":
                t = _handle_type_name(model, p.type)
                if p.nullable:
                    sig.append(f"{n}: {t} | null")
                    args.append(f"{n} === null ? NULL_EXTERNAL : {n}.ptr")
                else:
                    sig.append(f"{n}: {t}")
                    args.append(f"{n}.ptr")
            else:
                raise ValueError(f"emit/ts.py: unexpected input param kind {p.kind!r} on {fn.name}")

        def out_expr(p):
            key = _ts_str(p.name)
            if p.type == BUF_HANDLE:
                return f"outs[{key}] as Buffer"
            return f"wrapHandle(this.raw, outs[{key}] as HandleRef) as {_handle_type_name(model, p.type)}"

        def out_type(p):
            return "Buffer" if p.type == BUF_HANDLE else _handle_type_name(model, p.type)

        if len(outs) == 0:
            ret_type = "void"
        elif len(outs) == 1:
            ret_type = out_type(outs[0])
        else:
            ret_type = "{ " + "; ".join(f"readonly {_ts_param_name(p.name)}: {out_type(p)}" for p in outs) + " }"
        lines.append(f"  /** The generated call wrapper for {fn.name}. */")
        lines.append(f"  {method}({', '.join(sig)}): {ret_type} {{")
        call = f"rawCall(this.raw, SYMBOL.{sym}, [{', '.join(args)}])"
        if not outs:
            lines.append(f"    checkStatus({call});")
        else:
            lines.append(f"    const outs = checkStatus({call});")
            if len(outs) == 1:
                lines.append(f"    return {out_expr(outs[0])};")
            else:
                lines.append("    return {")
                for p in outs:
                    lines.append(f"      {_ts_param_name(p.name)}: {out_expr(p)},")
                lines.append("    };")
        lines += ["  }", ""]
    lines += ["}", ""]
    return "\n".join(lines)


def outputs(model) -> list[Output]:
    texts = [
        render_decls(model),
        render_errmap(model),
        render_libc(model),
        render_vocab(model),
        render_calls(model),
    ]
    if model.major != 1:
        texts = _respell_all(texts, model.major)
    return [Output(path, content=text) for path, text in zip(paths(model.major), texts, strict=True)]
