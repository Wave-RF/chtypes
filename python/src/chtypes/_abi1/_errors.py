"""The error classes the generated layer and the loader raise. They ARE the
public classes of `chtypes.errors` (one family, docs/reference/bindings-v1.md
section 4): this module only re-exports them under the names the generated
`_errmap` refers to, so the status and refusal tables stay a pure mapping.
"""

from __future__ import annotations

from chtypes.errors import (
    ArtifactCorruptError,
    ArtifactIncompatibleError,
    CallError,
    ChtypesError,
    InternalError,
    SchemaError,
    UnsupportedError,
    UsageError,
    misuse,
)

__all__ = [
    "ArtifactCorruptError",
    "ArtifactIncompatibleError",
    "CallError",
    "ChtypesError",
    "InternalError",
    "SchemaError",
    "UnsupportedError",
    "UsageError",
    "misuse",
]
