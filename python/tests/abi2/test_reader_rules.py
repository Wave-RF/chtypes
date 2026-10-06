"""ABI v2's reader rules (spec/abi-v2/docs.md), through the public API, over the
generated v2 stub:

- r2: a member the description does not name is ignored at every object level,
  `_b64` members included, in every result document and in build_info (stub
  variant "r2-unknown-members");
- r3: a value a vocabulary does not list is kept as that vocabulary's unknown(n),
  for that field alone, and never fails the document, the row or the batch; the
  fallback's fail-closed reading stays (stub variant "r3-unknown-values", one
  planted value per "!E:<id>" body, and "r3-unknown-capabilities"); a call status
  outside the closed set is an internal error naming unknown(n).

The documents and the planted values are scripts/abi-v1/emit/_stubshared.py's
R2_DOCS and R3_MUTATIONS, the shapes public pull request #509 measured on the
released 1.0.4 bindings; `test_r3_every_planted_value_is_checked` holds this file
to every id in R3_MUTATIONS. `test_r3_every_described_vocabulary` covers the enums
no document carries (chs_format, discover_query_param) from the generated map of
every enum the description defines. Without `$CHTYPES_ABI2_STUBS` the stub tests
skip loudly by name.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from chtypes import (
    DefaultKind,
    FilterOutcome,
    Format,
    InternalError,
    Outcome,
    Reason,
    Source,
    Status,
    Verdict,
    library,
)
from chtypes._abi2 import _vocab

from .conftest import REPO_ROOT, stub_path

JSON = Format.JSON_EACH_ROW
CREATE = b"CREATE TABLE t (x Int32)"


@pytest.fixture
def open_variant(stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch):
    """A factory: the named stub variant, opened through the public API."""
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")

    def open_one(name: str) -> library.Library:
        target = tmp_path / f"{name}.so"
        shutil.copyfile(stub_path(stubs_dir, stubs_manifest["variants"][name]), target)
        target.chmod(0o755)
        with pytest.warns(UserWarning, match="UNVERIFIED"):
            return library.open_unverified(target, allow=True)

    return open_one


def test_r2_unknown_members_are_ignored(open_variant) -> None:
    lib = open_variant("r2-unknown-members")

    info = lib.build_info
    assert info.clickhouse_version == "26.8.15.10" and info.channel == "lts"
    assert "default_generators" in info.capabilities.features
    handles = lib.live_handles()
    assert any(kind.endswith("schema") for kind in handles), handles
    assert lib.error_codes().name(53) == "TYPE_MISMATCH"

    schema = lib.compile_table(CREATE)
    desc = schema.describe()
    assert [(c.name, c.type) for c in desc.columns] == [(b"x", b"Int32")]

    row = schema.row(JSON, b'{"x":1}')
    assert row.outcome is Outcome.ACCEPTED and len(row.columns) == 1
    assert row.columns[0].text == b"abc" and row.columns[0].value == b"abc"
    assert row.input_span is not None and row.input_span.len == 3
    assert len(row.computed) == 1 and len(row.transformed) == 1
    assert row.unknown_fields == (b"u",) and row.unsupported_settings == (b"st",)

    batch = schema.rows(JSON, b'{"x":1}', export=JSON)
    assert batch.outcome is Outcome.ACCEPTED and batch.rows_read == 1
    assert len(batch.rows) == 1 and len(batch.rows[0].columns) == 1
    assert batch.engine_rows is not None and len(batch.engine_rows) == 1
    assert batch.spans is not None and len(batch.spans) == 1 and len(batch.unconsumed) == 1
    assert batch.framing is not None and batch.framing.header is not None
    assert batch.framing.header.names == (b"s",)
    assert batch.payload == b'{"s":"abc"}\n'

    with schema.compile_filter("x > 1") as flt:
        res = flt.rows(JSON, b'{"x":1}')
    assert res.outcome is FilterOutcome.OK and res.rows_read == 2
    assert res.verdicts == (Verdict.TRUE, Verdict.FALSE)
    assert len(res.errors) == 1 and res.unsupported_settings == (b"st",)

    disc = lib.discover_columns(b"{}")
    assert [c.declaration for c in disc.columns] == [b"c String"]
    assert disc.columns_sql == b"c String"


# Every planted value this file checks, by its R3_MUTATIONS id.
CHECKED: set[str] = set()


def _checked(mutation: str) -> bytes:
    CHECKED.add(mutation)
    return f"!E:{mutation}".encode()


def test_r3_unknown_values_are_kept(open_variant) -> None:
    lib = open_variant("r3-unknown-values")
    clean = lib.compile_table(CREATE)

    def row(mutation: str):
        return clean.row(JSON, _checked(mutation))

    def batch(mutation: str):
        return clean.rows(JSON, _checked(mutation), export=JSON)

    # The control: the clean document decodes with every value listed.
    control = clean.row(JSON, b"{}")
    assert control.outcome is Outcome.ACCEPTED and Source.known(control.columns[0].source)

    r = row("row.outcome")
    assert r.outcome == "x_future_outcome" and not r.outcome.known
    assert r.outcome is not Outcome.ACCEPTED and len(r.columns) == 1

    r = row("row.cols.src")
    col = r.columns[0]
    assert col.source == "x_future_src" and not Source.known(col.source)
    assert col.is_stored is False and col.text == b"abc" and r.outcome is Outcome.ACCEPTED

    r = row("row.transformed.reason")
    reason = r.transformed[0].reason
    assert reason == "x_future_reason" and not Reason.known(reason)
    assert r.transformed[0].lossy is Reason.lossy(Reason.VALUE_CHANGED)

    r = row("row.verdict")
    assert r.verdict == "x" and not r.verdict.known and r.verdict.answered is False

    b = batch("batch.outcome")
    assert b.outcome == "x_future_outcome" and not b.outcome.known
    assert b.outcome is not Outcome.ACCEPTED and len(b.rows) == 1

    b = batch("batch.rows.outcome")
    assert b.rows[0].outcome == "x_future_outcome" and not b.rows[0].outcome.known
    assert b.outcome is Outcome.ACCEPTED  # the batch's own value untouched

    b = batch("batch.rows.cols.src")
    assert b.rows[0].columns[0].source == "x_future_src" and len(b.rows) == 1
    assert not Source.known(b.rows[0].columns[0].source)

    b = batch("batch.transformed.reason")
    assert b.transformed[0].reason == "x_future_reason"
    assert not Reason.known(b.transformed[0].reason)

    # A member this binding does not read carries the unlisted reason: the batch stands.
    b = batch("batch.storage_transforms.reason")
    assert b.outcome is Outcome.ACCEPTED and len(b.rows) == 1

    # The schema's enum constrains the writer, never the reader (r3).
    b = batch("batch.framing.container")
    assert b.framing is not None and b.framing.container == "x_future_container"

    with clean.compile_filter("x > 1") as flt:
        f = flt.rows(JSON, _checked("filter.outcome"))
        assert f.outcome == "x_future_outcome" and not f.outcome.known
        assert f.outcome is not FilterOutcome.OK
        f = flt.rows(JSON, _checked("filter.verdicts"))
        assert f.verdicts[0] is Verdict.TRUE and f.verdicts[1] == "x"
        assert not f.verdicts[1].known and f.verdicts[1].answered is False

    # chs_schema_describe has no body: the stub answers the mutation the
    # schema's own statement named.
    mutated = lib.compile_table(_checked("describe.default_kind"))
    col = mutated.describe().columns[0]
    assert col.default_kind == "X_FUTURE" and not col.default_kind.known
    assert isinstance(col.default_kind, DefaultKind) and col.name == b"x"

    disc = lib.discover_columns(_checked("discovery.default_kind"))
    assert [c.declaration for c in disc.columns] == [b"c String"]


def test_r3_every_planted_value_is_checked(open_variant) -> None:
    """The planted ids come from the generator, so a value the stub learns to
    plant fails here until this file checks it."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts" / "abi-v1"))
    from emit._stubshared import R3_MUTATIONS

    if not CHECKED:
        test_r3_unknown_values_are_kept(open_variant)
    assert CHECKED == set(R3_MUTATIONS), sorted(set(R3_MUTATIONS) ^ CHECKED)


def test_r3_unknown_status_is_internal_naming_it(open_variant) -> None:
    """A call status outside the closed set is unknown(n), and the call still
    fails, as an internal error naming n."""
    lib = open_variant("r3-unknown-values")
    with pytest.raises(InternalError) as info:
        lib.validate_type("!U:")
    err = info.value
    assert err.status == 99
    status = Status(err.status)
    assert not status.known and status.name == "unknown(99)"
    assert "unknown(99)" in str(err)


def test_r3_unknown_capabilities_are_kept(open_variant) -> None:
    caps = open_variant("r3-unknown-capabilities").build_info.capabilities
    assert "XFutureFormat" in caps.input_formats and "XFutureFormat" in caps.export_formats
    assert "x_future_flag" in caps.doc_flags
    assert "x_future_feature" in caps.features and "default_generators" in caps.features


def test_r3_every_described_vocabulary() -> None:
    """Every enum the description defines (read from spec/abi-v2/abi.json, never
    a hand-kept list): an unlisted value builds that vocabulary's unknown(n),
    whose `known` is False and whose raw value reads back unchanged; every listed
    value is known. Needs no library."""
    abi = json.loads((REPO_ROOT / "spec" / "abi-v2" / "abi.json").read_text(encoding="utf-8"))
    names = list(abi["enums"])
    assert len(names) >= 10, names
    assert list(_vocab.DESCRIBED_VOCABULARIES) == names
    for name in names:
        for raw in (f"x_unlisted_{name}", "2147483000", "-7"):
            got = _vocab.described_vocabulary(name, raw)
            if got is None:
                # Only an int32 enum refuses a spelling, and only a non-integer one.
                assert not raw.lstrip("-").isdigit(), (name, raw)
                assert issubclass(_vocab.DESCRIBED_VOCABULARIES[name], int), name
                continue
            assert got == (False, raw), (name, raw, got)
        for value in abi["enums"][name]["values"]:
            raw = str(value["value"])
            assert _vocab.described_vocabulary(name, raw) == (True, raw), (name, raw)
    assert _vocab.described_vocabulary("no_such_enum", "1") is None
