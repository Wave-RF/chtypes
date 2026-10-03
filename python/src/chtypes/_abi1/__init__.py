"""The ABI v1 loader and generated declaration layer (plan
PLAN-sdk-v1-ffi-2026-10-01.md), private to this binding until wave C wires it
behind the public API. Not part of `chtypes`'s public surface: nothing here
is re-exported from `chtypes/__init__.py`.

- `_decls`: GENERATED (scripts/abi-v1/emit/python.py) -- ctypes signatures,
  handle classes and the test-only `invoke_by_name()` dispatcher.
- `_errors`: hand-written exception classes.
- `_errmap`: GENERATED -- the sdk.json status/reason -> `_errors` class maps.
- `_loader`: hand-written -- `open()`/`open_unverified()`, the loader steps.
"""

from __future__ import annotations
