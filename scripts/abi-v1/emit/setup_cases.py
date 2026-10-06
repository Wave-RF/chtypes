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
         unverified open (Go `OpenUnverified(path, true)`, Python
         `open_unverified(path, allow=True)`, TypeScript `openUnverified(path,
         { allow: true })`, Rust `Library::open_unverified(path, true)`) of the
         case's ONE copy of that variant (the case's own `variant` when the
         step names none), the same file for every open step that names it.

`expect` is {"ok": true}, or the error the step must raise: `class` is a key of
spec/abi-v1/sdk.json's errors.classes (each binding's own class for it). An
error the library answered also names the `status`, `ch_code` and `ch_name` it
must carry, verbatim; a loader refusal names its `reason`, sdk.json's word.

THE CASE. Setup latches only once an image completes load step 7 (chs_initialize,
then chs_set_defaults when there are defaults). Before that:

  * a second, different setup before any open is attempted is a UsageError,
    never a silent last-wins;
  * ANY failed open clears the setup record, whatever failed: here a load the
    loader refuses at step 4 (_stubshared.plan's "fingerprint-other" variant,
    an incompatible artifact), and a zone the library refuses at step 7. The
    next setup is then accepted, whatever it is, and the next open runs with
    it;
  * a failed open is never remembered, so the same open runs again.

Once an image has completed step 7, a different setup is a UsageError. The bad
zone, its refusal and the stub rule behind both come from _stubshared.IMAGE_ZONE,
which the stub is generated from too; the refused load and its reason come from
_stubshared.plan, which build-stubs.sh builds from.
"""

from __future__ import annotations

import json

from . import Output, _stubshared, banner

PATH = "tests/fixtures/abi-v1/setup-cases.json"

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
            # A load the loader refuses before step 7. No image has completed
            # step 7, so the failed open clears the record.
            {"op": "open", "variant": refused_variant, "expect": refused_load},
            # So a different setup is accepted now: a zone the library
            # refuses. The open that reaches step 7 raises the library's own
            # refusal, and clears the record again.
            {"op": "setup", "timezone": rule["bad_zone"], "expect": OK},
            {"op": "open", "expect": refused},
            # No image has completed step 7, so the record was cleared: the
            # same setup records again, and the open runs step 7 again rather
            # than remembering the failure.
            {"op": "setup", "timezone": rule["bad_zone"], "expect": OK},
            {"op": "open", "expect": refused},
            # A corrected setup is no longer refused, and the same image now
            # opens under it.
            {"op": "setup", "timezone": GOOD_ZONE, "expect": OK},
            {"op": "open", "expect": OK},
            # Step 7 has completed: the setup is latched, and a different one
            # is misuse.
            {"op": "setup", "timezone": OTHER_ZONE, "expect": misuse},
        ],
    }


def build_cases(model) -> list[dict]:
    return [_latch_case(model)]


def render(model) -> str:
    doc = {
        "_generated": banner(model),
        "schema": 1,
        "cases": build_cases(model),
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def outputs(model) -> list[Output]:
    return [Output(PATH, content=render(model))]
