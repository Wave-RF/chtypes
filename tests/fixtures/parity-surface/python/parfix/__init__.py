"""parfix: a fixture for scripts/parity-surface.py — the Python surface
tests/fixtures/parity-surface/doc.md describes, plus one allowlisted extra
(Verdict.of). Nothing here does anything."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Mapping, Optional, Union

BytesIn = Union[bytes, str]
Settings = Mapping[str, str]


def setup(*, timezone: Optional[str] = None) -> None:
    """Record the setup."""


@dataclass(frozen=True)
class Resolved:
    library_path: str
    warnings: tuple[str, ...]


class Registry:
    def __init__(self, *, autofetch: Optional[bool] = None) -> None:
        self._autofetch = autofetch

    def for_version(self, request: str) -> Library:
        return Library()

    def installed(self) -> tuple[Resolved, ...]:
        return ()


class Library:
    @property
    def version(self) -> str:
        return ""

    def validate_type(self, type_expr: BytesIn) -> bytes:
        return b""

    def compile_table(self, create_table: BytesIn, *, settings: Optional[Settings] = None) -> Schema:
        return Schema()


class _Handle:
    def close(self) -> None:
        """Release the handle."""

    def __enter__(self) -> _Handle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Schema(_Handle):
    pass


class Format(IntEnum):
    JSON_EACH_ROW = 0
    CSV = 1

    @property
    def ch_name(self) -> str:
        return ("JSONEachRow", "CSV")[self]


class Verdict(StrEnum):
    TRUE = "t"
    FALSE = "f"

    @property
    def answered(self) -> bool:
        return True

    @classmethod
    def of(cls, text: str) -> Verdict:
        return cls(text)


class ChtypesError(Exception):
    pass


class CallError(ChtypesError):
    def __init__(self, ch_code: int = 0) -> None:
        super().__init__()
        self.ch_code = ch_code


class SchemaError(CallError):
    pass


class ArtifactError(ChtypesError):
    def __init__(self, message: str = "", reason: str = "", path: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.path = path


class ArtifactIncompatibleError(ArtifactError):
    pass


class ArtifactMissingError(ArtifactError):
    pass


class ArtifactPinnedError(ArtifactError):
    pass


@dataclass(frozen=True)
class Span:
    off: int
    len: int


@dataclass(frozen=True)
class Header:
    consumed: bool
    lines: int


@dataclass(frozen=True)
class Framing:
    bom_skipped: Optional[bool]
    header: Optional[Header]


@dataclass(frozen=True)
class RowError:
    row: int
    msg: bytes


@dataclass(frozen=True)
class BatchResult:
    rows_read: int
    partition_id: Optional[bytes]
    columns_sql: bytes
    spans: Optional[tuple[Span, ...]]
    framing: Optional[Framing]
    errors: tuple[RowError, ...]


@dataclass(frozen=True)
class ErrorCodeEntry:
    code: int
    name: str


class ErrorCodeTable:
    def name(self, code: int) -> Optional[str]:
        return None

    def all(self) -> tuple[ErrorCodeEntry, ...]:
        return ()
