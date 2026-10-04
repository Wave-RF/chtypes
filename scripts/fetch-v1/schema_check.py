#!/usr/bin/env python3
r"""scripts/fetch-v1/schema_check.py — a Python-stdlib-only validator for
the fixtures this repository generates against the frozen schemas lane 0A
published (spec/fetch-v1/schema/*.json). No `jsonschema` dependency: the
`v1-fixtures` job runs this with nothing but a checkout, the same
discipline scripts/fetch-v1/gen-constants.py follows.

    scripts/fetch-v1/schema_check.py            validate everything this script covers
    scripts/fetch-v1/schema_check.py --selftest  prove a deliberately broken fixture is
                                                  refused, on an in-memory document —
                                                  never a file on disk

WHAT IT VALIDATES, and the one deliberate exclusion:

  - tests/fixtures/fetch-v1/cases.json            against cases.schema.json
  - tests/fixtures/fetch-v1/http/*.json           against http-script.schema.json
  - tests/fixtures/fetch-v1/locks/expected/*.json against lock3.schema.json
  - tests/fixtures/fetch-v1/layouts/<name>/unpacked/sha256/*/verified.json
                                                  against verified.schema.json, for every
                                                  layout whose name starts `cache-record-canonical`
                                                  (the foreign, schema-2 and 0.x layouts exist to
                                                  FAIL it and are deliberately not validated)

`locks/inputs/invalid-*.json` is DELIBERATELY NOT validated here. Those
fixtures exist to prove a wrong-schema or wrong-ABI lock is refused
(frozen-lock-abi-2, lock-schema-2-refused) — they are supposed to fail
lock3.schema.json, so running this validator against them would be
asserting the opposite of what they are for. Every other file under
locks/inputs/ (not `invalid-*`) is schema-valid and IS checked, same as
locks/expected/.

SUPPORTED SUBSET of JSON Schema (draft 2020-12), exactly what the five
schema files in spec/fetch-v1/schema/ use — not a general-purpose
validator: type, const, enum, pattern, required, properties,
additionalProperties (bool), items, minItems, maxItems, uniqueItems,
minLength, maxLength, minimum, maximum, exclusiveMinimum, $ref (to a
sibling #/$defs/<name> only), oneOf. `format` is accepted and ignored
(every use in these schemas is "uri", which this script does not verify
beyond "is a string" — the content of a predicateType or media type URL is
not this gate's job).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = ROOT / "spec" / "fetch-v1" / "schema"
FIXTURES_DIR = ROOT / "tests" / "fixtures" / "fetch-v1"


class SchemaError(Exception):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def _resolve_ref(ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaError(ref, "only local #/... refs are supported")
    node: Any = root_schema
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(ref, "does not resolve")
        node = node[part]
    return node


def validate(instance: Any, schema: dict[str, Any], root_schema: dict[str, Any], path: str = "$") -> None:
    if "$ref" in schema:
        validate(instance, _resolve_ref(schema["$ref"], root_schema), root_schema, path)
        return

    if "oneOf" in schema:
        errors: list[str] = []
        matches = 0
        for sub in schema["oneOf"]:
            try:
                validate(instance, sub, root_schema, path)
                matches += 1
            except SchemaError as e:
                errors.append(e.message)
        if matches != 1:
            raise SchemaError(path, f"matched {matches} of {len(schema['oneOf'])} oneOf branches: {errors}")
        return

    if "const" in schema:
        if instance != schema["const"]:
            raise SchemaError(path, f"expected const {schema['const']!r}, got {instance!r}")

    if "enum" in schema:
        if instance not in schema["enum"]:
            raise SchemaError(path, f"{instance!r} is not one of {schema['enum']!r}")

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(instance, one) for one in types):
            raise SchemaError(path, f"expected type {t!r}, got {type(instance).__name__}: {instance!r}")

    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            raise SchemaError(path, f"{instance!r} does not match pattern {schema['pattern']!r}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(path, f"{instance!r} is shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaError(path, f"{instance!r} is longer than maxLength {schema['maxLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(path, f"{instance!r} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(path, f"{instance!r} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            raise SchemaError(path, f"{instance!r} <= exclusiveMinimum {schema['exclusiveMinimum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(path, f"has {len(instance)} items, minItems is {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaError(path, f"has {len(instance)} items, maxItems is {schema['maxItems']}")
        if schema.get("uniqueItems") and len(instance) != len(set(map(_hashable, instance))):
            raise SchemaError(path, "items are not unique")
        if "items" in schema:
            for i, item in enumerate(instance):
                validate(item, schema["items"], root_schema, f"{path}[{i}]")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        missing = [k for k in required if k not in instance]
        if missing:
            raise SchemaError(path, f"missing required propert{'y' if len(missing) == 1 else 'ies'}: {missing}")

        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                validate(value, props[key], root_schema, f"{path}.{key}")
            else:
                ap = schema.get("additionalProperties", True)
                if ap is False:
                    raise SchemaError(path, f"additional property {key!r} is not allowed")
                if isinstance(ap, dict):
                    validate(value, ap, root_schema, f"{path}.{key}")


def _is_type(instance: Any, t: str) -> bool:
    if t == "object":
        return isinstance(instance, dict)
    if t == "array":
        return isinstance(instance, list)
    if t == "string":
        return isinstance(instance, str)
    if t == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if t == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if t == "boolean":
        return isinstance(instance, bool)
    if t == "null":
        return instance is None
    raise SchemaError("$", f"unsupported schema type {t!r}")


def _hashable(v: Any) -> Any:
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    return v


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def validate_file(path: Path, schema: dict[str, Any]) -> list[str]:
    try:
        instance = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"{path}: invalid JSON: {e}"]
    try:
        validate(instance, schema, schema)
    except SchemaError as e:
        return [f"{path}: {e.message} (at {e.path})"]
    return []


def run_checks() -> list[str]:
    problems: list[str] = []

    cases_schema = load_schema("cases.schema.json")
    cases_path = FIXTURES_DIR / "cases.json"
    if cases_path.exists():
        problems += validate_file(cases_path, cases_schema)
    else:
        problems.append(f"{cases_path}: does not exist")

    http_schema = load_schema("http-script.schema.json")
    http_dir = FIXTURES_DIR / "http"
    if http_dir.is_dir():
        for p in sorted(http_dir.glob("*.json")):
            problems += validate_file(p, http_schema)

    lock_schema = load_schema("lock3.schema.json")
    for sub in ("inputs", "expected"):
        lock_dir = FIXTURES_DIR / "locks" / sub
        if not lock_dir.is_dir():
            continue
        for p in sorted(lock_dir.glob("*.json")):
            if sub == "inputs" and p.name.startswith("invalid-"):
                continue  # deliberately not lock3-valid; see module docstring
            problems += validate_file(p, lock_schema)

    verified_schema = load_schema("verified.schema.json")
    layouts_dir = FIXTURES_DIR / "layouts"
    if layouts_dir.is_dir():
        for layout in sorted(layouts_dir.glob("cache-record-canonical*")):
            records = sorted(layout.glob("unpacked/sha256/*/verified.json"))
            if not records:
                problems.append(f"{layout}: a canonical-record layout carries no verified.json")
            for p in records:
                problems += validate_file(p, verified_schema)

    return problems


def selftest() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "id"],
        "properties": {"schema": {"const": 1}, "id": {"type": "string", "pattern": "^[a-z-]+$"}},
    }
    good = {"schema": 1, "id": "ok-case"}
    try:
        validate(good, schema, schema)
    except SchemaError as e:
        print(f"schema_check --selftest: a known-good document was refused: {e}", file=sys.stderr)
        sys.exit(1)

    broken_cases = [
        ({"schema": 2, "id": "ok-case"}, "wrong const"),
        ({"schema": 1, "id": "Not Lowercase"}, "pattern violation"),
        ({"schema": 1}, "missing required property"),
        ({"schema": 1, "id": "ok-case", "extra": True}, "additional property"),
    ]
    for doc, why in broken_cases:
        try:
            validate(doc, schema, schema)
        except SchemaError:
            continue
        print(f"schema_check --selftest: a document broken by [{why}] was NOT refused: {doc}", file=sys.stderr)
        sys.exit(1)

    # oneOf, array and $ref coverage (the http-script response shape and
    # cases.json's $defs/case both exercise these).
    ref_schema = {
        "$defs": {"leaf": {"type": "string", "enum": ["a", "b"]}},
        "type": "array",
        "items": {"$ref": "#/$defs/leaf"},
        "minItems": 1,
        "uniqueItems": True,
    }
    try:
        validate(["a", "b"], ref_schema, ref_schema)
    except SchemaError as e:
        print(f"schema_check --selftest: $ref/array/uniqueItems coverage failed: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        validate(["a", "a"], ref_schema, ref_schema)
        print("schema_check --selftest: a duplicate item was not refused by uniqueItems", file=sys.stderr)
        sys.exit(1)
    except SchemaError:
        pass
    try:
        validate(["c"], ref_schema, ref_schema)
        print("schema_check --selftest: an out-of-enum $ref target was not refused", file=sys.stderr)
        sys.exit(1)
    except SchemaError:
        pass

    one_of_schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    try:
        validate("x", one_of_schema, one_of_schema)
        validate(1, one_of_schema, one_of_schema)
    except SchemaError as e:
        print(f"schema_check --selftest: oneOf coverage failed: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        validate(1.5, {"oneOf": [{"type": "string"}, {"type": "integer"}]}, one_of_schema)
        print("schema_check --selftest: a value matching zero oneOf branches was not refused", file=sys.stderr)
        sys.exit(1)
    except SchemaError:
        pass

    # The canonical record schema: a good record passes, and each rule the
    # readers enforce is refused here too.
    verified_schema = load_schema("verified.schema.json")
    good_record = {
        "schema": 1,
        "platform": "linux-arm64",
        "version": "26.8.15.10",
        "channel": None,
        "build": "20261001.183455",
        "library": "libchtypes.so",
        "library_sha256": "a" * 64,
        "library_bytes": 3,
        "digests": {
            "index": None,
            "manifest": "sha256:" + "b" * 64,
            "layer": "sha256:" + "c" * 64,
            "bundle": None,
            "bundle_manifest": None,
        },
        "signed_by": None,
        "predicate": {},
    }
    try:
        validate(good_record, verified_schema, verified_schema)
    except SchemaError as e:
        print(f"schema_check --selftest: a canonical record was refused: {e}", file=sys.stderr)
        sys.exit(1)

    def mutated(fn: Any) -> dict[str, Any]:
        doc = json.loads(json.dumps(good_record))
        fn(doc)
        return doc

    for why, doc in [
        ("schema 2", mutated(lambda d: d.update(schema=2))),
        ("missing signed_by", mutated(lambda d: d.pop("signed_by"))),
        ("missing digests.bundle_manifest", mutated(lambda d: d["digests"].pop("bundle_manifest"))),
        ("library with a separator", mutated(lambda d: d.update(library="a/b"))),
        ("short library_sha256", mutated(lambda d: d.update(library_sha256="abc"))),
        ("negative library_bytes", mutated(lambda d: d.update(library_bytes=-1))),
        ("unknown platform", mutated(lambda d: d.update(platform="plan9-mips"))),
        ("three-part version", mutated(lambda d: d.update(version="26.8.15"))),
    ]:
        try:
            validate(doc, verified_schema, verified_schema)
        except SchemaError:
            continue
        print(f"schema_check --selftest: a record broken by [{why}] was NOT refused", file=sys.stderr)
        sys.exit(1)

    print("schema_check --selftest: OK")


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        selftest()
        return 0
    if argv:
        print(f"schema_check.py: unknown arguments: {argv}", file=sys.stderr)
        return 2

    problems = run_checks()
    if problems:
        print("schema_check.py: fixtures do not match their schemas:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print("schema_check.py: every checked fixture matches its schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())
