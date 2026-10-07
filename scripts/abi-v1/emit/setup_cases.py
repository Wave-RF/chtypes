"""tests/fixtures/abi-v1/setup-cases.json: the process setup's public-API
cases (docs/reference/bindings-v1.md section 6), defined once and run by every
binding's public-API suite through its PUBLIC API, against fresh copies of the
stub libraries that scripts/abi-v1/build-stubs.sh builds.

WHY A SEPARATE FILE. tests/fixtures/abi-v1/cases.json is call-level: each of
its cases is one C call through a binding's generated layer, and its runners
never see the process setup. A setup case is a sequence of public calls whose
subject is the binding's own setup guard, its image cache and its registry
memo, so it lives here. Only the public-API suites read it, and it never
reaches the call-level runners or their reports (scripts/abi-v1/parity.py).

HOW A RUNNER RUNS A CASE. A case names a stub `variant`, and an open step may
name another one. The runner:

  1. copies each variant the case uses to a fresh path, once per case, so the
     case gets fresh images (the stub keeps its image zone per image, as the
     library does: _stubshared.IMAGE_ZONE);
  2. starts with no setup recorded and no image ever set up (a fresh process,
     or the binding's own test-only reset);
  3. sets CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1;
  4. runs the steps in order, and fails the case at the first step whose
     outcome differs from its `expect`:
       * {"op": "setup", "timezone": "<name>"}: the binding's `setup` with that
         image zone and no defaults;
       * {"op": "open"} or {"op": "open", "variant": "<name>"}: the binding's
         unverified open with both opt-ins (Go `OpenUnverified(path, true)`,
         Python `open_unverified(path, allow=True)`, TypeScript
         `openUnverified(path, { allow: true })`, Rust
         `Library::open_unverified(path, true)`) of the case's ONE copy of that
         variant (the case's own `variant` when the step names none), the same
         file for every open step that names it. With "allow": false the
         caller's own opt-in is withheld (the environment's stays set);
       * {"op": "open", "request": "<spelling>"}: a registry open of that
         request, over an empty cache, offline, with autofetch off (Go
         `NewRegistry(...).For`, Python `Registry(...).for_version`,
         TypeScript `(await Registry.open(...)).for`, Rust
         `Registry::new(...).for_version`).

`expect` is {"ok": true}, or the error the step must raise: `class` is a key of
spec/abi-v1/sdk.json's errors.classes (each binding's own class for it). An
error the library answered also names the `status`, `ch_code` and `ch_name` it
must carry, verbatim; a loader refusal names its `reason`, sdk.json's word. A
successful open may also name `image_zone`: the zone the opened image was set
up with at step 7, read back through the library with the document's
`image_zone_probe` (_stubshared.ZONE_PROBE: `validate_type` of exactly that
text, in each binding's own spelling).

THE RULE THE CASES PIN. Setup latches only once an image completes load step 7
(chs_initialize, then chs_set_defaults when there are defaults). Before that:

  * a second, different setup before any open is attempted is a UsageError,
    never a silent last-wins;
  * the binding's own input checks unlock nothing: a refused version spelling,
    or an unverified open without the caller's opt-in, fails before any load is
    attempted, and a different setup is still a UsageError afterwards;
  * an open that attempted a load and failed, whatever failed (here a load the
    loader refuses at step 4, _stubshared.plan's "fingerprint-other" variant,
    and a zone the library refuses at step 7), KEEPS the setup record but makes
    it replaceable: the next open with no new setup runs under the recorded
    setup, never the empty one, and a different setup replaces the record;
  * a replaced record is locked again, so a second, different setup before the
    next open is a UsageError;
  * a failed open is never remembered, so the same open runs again.

Once an image has completed step 7, a different setup is a UsageError. The bad
zone, its refusal and the stub rule behind both come from _stubshared.IMAGE_ZONE,
and the probe from _stubshared.ZONE_PROBE, which the stub is generated from too;
the refused load and its reason come from _stubshared.plan, which
build-stubs.sh builds from.
"""

from __future__ import annotations

import json

from . import Output, _stubshared, banner

# Every major some binding speaks (emit/__init__.py, SHARED): each major's
# conformance runners need their own cases and stubs.
MAJORS = (1, 2)
SHARED = True

PATH = "tests/fixtures/abi-v1/setup-cases.json"


def path(major: int) -> str:
    """Each major's own file: tests/fixtures/abi-v<major>/setup-cases.json."""
    return f"tests/fixtures/abi-v{major}/setup-cases.json"

# A good zone, and a second, different one: any spelling the stub accepts.
GOOD_ZONE = "Europe/Berlin"
OTHER_ZONE = "Asia/Tokyo"

OK = {"ok": True}


def _class_of(model, status: str) -> str:
    """The sdk.json error class a status maps to, read from sdk.json itself."""
    cls = model.sdk["errors"]["status"].get(status)
    if cls is None or cls not in model.sdk["errors"]["classes"]:
        raise ValueError(f"spec/abi-v1/sdk.json maps {status} to no error class")
    return cls


# The stub variant a loader refuses before step 7, through the unverified open:
# an incompatible artifact, the failure the issue was reported on.
REFUSED_VARIANT = "fingerprint-other"


def _refused_load(model) -> tuple[str, dict]:
    """(the variant, the expectation for opening it): its reason from
    _stubshared.plan, its class and step from sdk.json's loader.refusals. The
    step must come before step 7, and the unverified open must reach it (it
    skips steps 1 and 5)."""
    variant = next((v for v in _stubshared.plan(model) if v.name == REFUSED_VARIANT), None)
    if variant is None:
        raise ValueError(f"_stubshared.plan has no {REFUSED_VARIANT!r} variant")
    base = variant.reason.split(":", 1)[0]
    rows = [r for r in model.sdk["loader"]["refusals"] if r["reason"] == base]
    if not rows:
        raise ValueError(f"spec/abi-v1/sdk.json's loader.refusals has no reason {base!r}")
    steps = {r["step"] for r in rows}
    if steps & {1, 5} or max(steps) >= 7:
        raise ValueError(f"{REFUSED_VARIANT}: reason {base!r} is refused at step(s) {sorted(steps)}, "
                         "which the unverified open skips or which is not before step 7")
    classes = {r["error"] for r in rows}
    if len(classes) != 1 or not classes <= set(model.sdk["errors"]["classes"]):
        raise ValueError(f"{REFUSED_VARIANT}: reason {base!r} maps to error classes {sorted(classes)}")
    return variant.name, {"class": classes.pop(), "reason": variant.reason}


# A refused version spelling: a "v" prefix, refused before any resolve.
REFUSED_SPELLING = "v26.8"


def _latch_case(model) -> dict:
    rule = _stubshared.IMAGE_ZONE
    fn = model.function(rule["fn"])
    if rule["status"] not in fn.may_return:
        raise ValueError(f"{fn.name}: {rule['status']} is not in its may_return")
    refused = {
        "class": _class_of(model, rule["status"]),
        "status": rule["status"],
        "ch_code": rule["ch_code"],
        "ch_name": rule["ch_name"],
    }
    # A different setup is misuse, the class of CHS_INVALID_ARGUMENT, which is
    # also what the library answers a different spelling with.
    misuse = {"class": _class_of(model, rule["conflict_status"])}
    refused_variant, refused_load = _refused_load(model)
    return {
        "id": "setup.latches_only_on_success",
        "variant": "ok",
        "steps": [
            # Before any open is attempted, a second, different setup is
            # misuse: the first stands, never a silent last-wins.
            {"op": "setup", "timezone": OTHER_ZONE, "expect": OK},
            {"op": "setup", "timezone": GOOD_ZONE, "expect": misuse},
            # The binding's own input checks fail before any load is attempted
            # and unlock nothing: a refused version spelling, and an unverified
            # open without the caller's opt-in.
            {"op": "open", "request": REFUSED_SPELLING, "expect": misuse},
            {"op": "setup", "timezone": GOOD_ZONE, "expect": misuse},
            {"op": "open", "allow": False, "expect": misuse},
            {"op": "setup", "timezone": GOOD_ZONE, "expect": misuse},
            # A load the loader refuses before step 7. No image has completed
            # step 7, so the failed open makes the record replaceable.
            {"op": "open", "variant": refused_variant, "expect": refused_load},
            # So a different setup replaces it: a zone the library refuses.
            # The replaced record is locked again until the next open.
            {"op": "setup", "timezone": rule["bad_zone"], "expect": OK},
            {"op": "setup", "timezone": GOOD_ZONE, "expect": misuse},
            # The open that reaches step 7 raises the library's own refusal,
            # and makes the record replaceable again; a plain retry runs step
            # 7 again under the RECORDED zone, never the empty one, and is
            # refused again.
            {"op": "open", "expect": refused},
            {"op": "open", "expect": refused},
            # A corrected setup replaces it, and the same image now opens
            # under it.
            {"op": "setup", "timezone": GOOD_ZONE, "expect": OK},
            {"op": "open", "expect": {**OK, "image_zone": GOOD_ZONE}},
            # Step 7 has completed: the setup is latched, and a different one
            # is misuse.
            {"op": "setup", "timezone": OTHER_ZONE, "expect": misuse},
        ],
    }


def _retry_case(model) -> dict:
    """A transient failure, then a plain retry with no new setup: the retry
    loads under the zone the caller recorded, read back from the image."""
    refused_variant, refused_load = _refused_load(model)
    misuse = {"class": _class_of(model, _stubshared.IMAGE_ZONE["conflict_status"])}
    return {
        "id": "setup.a_retry_after_a_failed_open_keeps_the_recorded_setup",
        "variant": "ok",
        "steps": [
            {"op": "setup", "timezone": OTHER_ZONE, "expect": OK},
            # The failed open: an artifact the loader refuses before step 7.
            {"op": "open", "variant": refused_variant, "expect": refused_load},
            # The plain retry, with no new setup, opens another artifact under
            # the RECORDED zone, never the empty setup.
            {"op": "open", "expect": {**OK, "image_zone": OTHER_ZONE}},
            {"op": "setup", "timezone": GOOD_ZONE, "expect": misuse},
        ],
    }


# The statement the closed-server case compiles: any CREATE the stub accepts.
SERVER_CASE_STATEMENT = "CREATE TABLE t (k UInt8) ENGINE = Memory"


def _nullable_handle_params(model) -> list[tuple]:
    """(function, parameter) for every handle parameter that may be NULL and
    is not a free function's (free(NULL) does nothing, and a freed handle
    passed there is no worse). Where NULL is a valid input with a meaning of
    its own, the library cannot tell a freed handle from a deliberate NULL
    (rule r7)."""
    return [
        (f, p)
        for f in model.functions
        if not f.name.endswith("_free")
        for p in f.params
        if p.kind == "handle" and p.nullable
    ]


# The nullable handle parameters the description has today. A new one must be
# looked at for rule r7 before it is added here: if its NULL means something,
# it needs its own conformance case.
KNOWN_NULLABLE = (("chs_schema_create", "server"), ("chs_preview_batch", "filter"))


def _closed_server_case(model) -> dict | None:
    """Rule r7 (spec/abi-v2/docs.md): a closed handle is never passed as NULL.

    The case is for chs_schema_create's `server`, whose NULL is the image's
    own server. It reads everything off the stub's own live-handle counts,
    which the stub's chs_schema_create makes truthful: a schema made with a
    non-NULL server holds a counted reference to the server it RECEIVED, so a
    closed server stays live exactly while such a schema does; a schema made
    with NULL holds none. A compile that reached the library with NULL in
    place of a closed server would therefore create a schema (live chs_schema
    1) and hold no server, which the refused compile's `live` step below
    rules out.

    Rust cannot express it: a Server is freed only on its last Drop and a
    compile borrows it, so a closed server is unreachable.

    Rule r7 is a generation-2 rule, and generation 1 is frozen: its file has
    no such case."""
    if model.major < 2:  # generation 1 is frozen and has no server; r7 is a generation-2 rule
        return None
    params = _nullable_handle_params(model)
    found = tuple((f.name, p.name) for f, p in params)
    if found != KNOWN_NULLABLE:
        raise ValueError(
            f"rule r7: the nullable handle parameters are {found}, not {KNOWN_NULLABLE}: decide whether the "
            f"new one's NULL means something and needs a conformance case, then extend {__file__}"
        )
    fn, param = params[0]
    out = next(q for q in fn.params if q.kind == "out_handle")
    server, schema = param.type, out.type
    misuse = {"class": _class_of(model, "CHS_INVALID_ARGUMENT")}

    def live(**counts: int) -> dict:
        return {"op": "live", "live": counts}

    return {
        "id": "server.a_closed_server_is_never_passed_as_null",
        "variant": "ok",
        "rule": "r7",
        "not_expressible": {
            "rust": "a freed handle is unreachable: Server frees on its last Drop, and a compile borrows it",
        },
        "steps": [
            {"op": "open", "expect": OK},
            live(**{server: 0, schema: 0}),
            # Control 1: an OPEN server reaches the library as non-NULL. The
            # schema holds the server it received, so the server outlives its
            # caller's close for exactly as long as the schema.
            {"op": "server_new", "as": "open_server", "expect": OK},
            {"op": "compile", "statement": SERVER_CASE_STATEMENT, "server": "open_server", "as": "on_server", "expect": OK},
            {"op": "server_close", "server": "open_server"},
            live(**{server: 1, schema: 1}),
            {"op": "schema_close", "schema": "on_server"},
            live(**{server: 0, schema: 0}),
            # The rule: a CLOSED server is refused with the binding's usage
            # error, and no schema exists afterwards. Had the binding passed
            # NULL in its place, the library would have answered CHS_OK and
            # one schema would be live.
            {"op": "server_new", "as": "closed_server", "expect": OK},
            {"op": "server_close", "server": "closed_server"},
            live(**{server: 0, schema: 0}),
            {"op": "compile", "statement": SERVER_CASE_STATEMENT, "server": "closed_server", "expect": misuse},
            live(**{server: 0, schema: 0}),
            # Control 2: with NO server the library receives NULL and answers;
            # the schema holds no server.
            {"op": "compile", "statement": SERVER_CASE_STATEMENT, "as": "no_server", "expect": OK},
            live(**{server: 0, schema: 1}),
            {"op": "schema_close", "schema": "no_server"},
            live(**{server: 0, schema: 0}),
        ],
    }


def _closed_filter_case(model) -> dict | None:
    """Rule r7 for chs_preview_batch's `filter`, whose NULL is no filter.

    A batch call mints no handle, so no live count shows whether a filter
    reached it. The stub variant "filter-observable" (emit/stub.py,
    _stubshared.FILTER_OBSERVABLE_DOCS) answers `rows_passed` 1 when the
    filter reached it and 0 when it received NULL; the case reads that
    through the public result, never a hand-set value. A closed filter must
    be refused with the usage error: a binding that passed NULL instead would
    return a result with `rows_passed` 0.

    Rust cannot express it: a Filter is an owned value freed only on Drop."""
    if model.major < 2:
        return None
    misuse = {"class": _class_of(model, "CHS_INVALID_ARGUMENT")}
    rows = {"op": "rows", "schema": "schema", "format": "CSV", "body": "1\n"}
    return {
        "id": "filter.a_closed_filter_is_never_passed_as_null",
        "variant": "filter-observable",
        "rule": "r7",
        "not_expressible": {
            "rust": "a freed handle is unreachable: a Filter is an owned value that frees only on Drop",
        },
        "steps": [
            {"op": "open", "expect": OK},
            {"op": "compile", "statement": SERVER_CASE_STATEMENT, "as": "schema", "expect": OK},
            {"op": "filter_new", "schema": "schema", "expression": "k > 1", "as": "open_filter", "expect": OK},
            # Control 1: an OPEN filter reaches the library as non-NULL.
            {**rows, "filter": "open_filter", "expect": OK, "rows_passed": 1},
            # Control 2: with NO filter the library receives NULL.
            {**rows, "expect": OK, "rows_passed": 0},
            # The rule: a CLOSED filter is refused with the usage error.
            {"op": "filter_close", "filter": "open_filter"},
            {**rows, "filter": "open_filter", "expect": misuse},
            # And the refusal leaves the schema usable, with no filter.
            {**rows, "expect": OK, "rows_passed": 0},
            {"op": "schema_close", "schema": "schema"},
            {"op": "live", "live": {"chs_schema": 0, "chs_filter": 0}},
        ],
    }


def build_cases(model) -> list[dict]:
    cases = [_latch_case(model), _retry_case(model)]
    for build in (_closed_server_case, _closed_filter_case):
        case = build(model)
        if case is not None:
            cases.append(case)
    return cases


def render(model) -> str:
    doc = {
        "_generated": banner(model),
        "schema": 1,
        "image_zone_probe": _stubshared.ZONE_PROBE["probe"],
        "cases": build_cases(model),
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def outputs(model) -> list[Output]:
    return [Output(path(model.major), content=render(model))]
