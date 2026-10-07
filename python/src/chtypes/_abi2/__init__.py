"""The ABI v2 loader and generated declaration layer (plan
PLAN-sdk-v1-ffi-2026-10-01.md), private to this binding. Not part of `chtypes`'s
public surface: the public API (`chtypes.library`, `chtypes.registry`) is built
over it, and re-exports the vocabularies and errors it defines.

- `_decls`: GENERATED (scripts/abi-v1/emit/python.py) -- ctypes signatures,
  handle classes, the typed call wrappers on `Api`, and the test-only
  `invoke_by_name()` dispatcher.
- `_vocab`: GENERATED -- `Format`, `Status`, `Outcome`, `Verdict`, `Reason`,
  `Source`, `DefaultKind`, `DiscoverQueryParam`, `DocFlags` and the facts the
  description attaches, each vocabulary with its unknown(n) member (rule r3).
- `_errors`: the public `chtypes.errors` classes, re-exported under the names
  `_errmap` uses.
- `_errmap`: GENERATED -- the sdk.json status/reason -> `_errors` class maps.
- `_loader`: hand-written -- `open()`/`open_unverified()`, the loader steps,
  and rule r6's exact refusal of another fingerprint.
"""

from __future__ import annotations
