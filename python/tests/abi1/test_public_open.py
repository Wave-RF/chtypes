"""The process setup, step 7, the image cache and the unverified open, over the
stub library."""

from __future__ import annotations

import os

import pytest

from chtypes import (
    ArtifactCorruptError,
    ArtifactError,
    SchemaError,
    UsageError,
    _setup,
    library,
    open_unverified,
    setup,
)
from chtypes._abi1 import _decls, _loader


@pytest.fixture
def opened(stub_copy, monkeypatch):
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")

    def open_one():
        with pytest.warns(UserWarning, match="UNVERIFIED"):
            return open_unverified(stub_copy()[0], allow=True)

    return open_one


def test_setup_twice_with_the_same_setup_is_a_no_op(clean_process) -> None:
    setup(timezone="Asia/Tokyo", defaults={"a": "b"})
    setup(timezone="Asia/Tokyo", defaults={"a": "b"})


@pytest.mark.parametrize(
    "different",
    [
        {"timezone": "UTC", "defaults": {"a": "b"}},
        {"timezone": "Asia/Tokyo", "defaults": {"a": "c"}},
        {"timezone": "Asia/Tokyo"},
        {"defaults": {"a": "b"}},
    ],
)
def test_a_different_setup_is_a_usage_error_naming_both_and_the_first_stands(
    clean_process, different
) -> None:
    setup(timezone="Asia/Tokyo", defaults={"a": "b"})
    with pytest.raises(UsageError) as info:
        setup(**different)
    message = str(info.value)
    assert "Asia/Tokyo" in message and "setup" in message
    setup(timezone="Asia/Tokyo", defaults={"a": "b"})  # the first setup stands


def test_the_first_open_commits_the_empty_setup(opened, clean_process) -> None:
    opened()
    setup()  # exactly the setup in effect: fine
    setup(timezone="", defaults={})
    with pytest.raises(UsageError):
        setup(timezone="Asia/Tokyo")


def test_setup_is_applied_at_step_7_once_per_image_zone_then_defaults(
    stub_copy, monkeypatch, clean_process
) -> None:
    order: list[tuple[str, bytes]] = []
    real_init, real_defaults = _decls.Api.initialize, _decls.Api.set_defaults
    monkeypatch.setattr(
        _decls.Api,
        "initialize",
        lambda self, tz: (order.append(("zone", tz)), real_init(self, tz))[1],
    )
    monkeypatch.setattr(
        _decls.Api,
        "set_defaults",
        lambda self, d: (order.append(("defaults", d)), real_defaults(self, d))[1],
    )
    path, predicate = stub_copy()
    setup(timezone="Asia/Tokyo", defaults={"max_threads": "2"})
    first = library.open_image(path, predicate, None)
    assert order == [("zone", b"Asia/Tokyo"), ("defaults", b'{"max_threads":"2"}')]
    # The same image again: no second step 7.
    assert library.open_image(path, predicate, None) is first
    assert len(order) == 2


def test_no_defaults_means_no_set_defaults_call(stub_copy, monkeypatch, clean_process) -> None:
    called: list[bytes] = []
    monkeypatch.setattr(_decls.Api, "set_defaults", lambda self, d: called.append(d))
    path, predicate = stub_copy()
    setup(timezone="UTC")
    library.open_image(path, predicate, None)
    assert called == []


def test_a_step_7_failure_is_the_calls_own_error_never_a_refusal_reason(
    stub_copy, clean_process
) -> None:
    path, predicate = stub_copy()
    # A zone the library will not load: ClickHouse's own refusal, a SchemaError.
    with pytest.raises(SchemaError) as info:
        _loader.open(
            path, predicate, timezone=b"!S:CHS_REJECTED:36:BAD_ARGUMENTS:Unknown time zone"
        )
    assert info.value.ch_code == 36 and info.value.message == b"Unknown time zone"
    # A different spelling than the image already has: the library answers
    # INVALID_ARGUMENT, which is a UsageError (the stub injects the status, as
    # it does not keep a zone).
    path2, predicate2 = stub_copy()
    with pytest.raises(UsageError) as usage:
        _loader.open(
            path2,
            predicate2,
            timezone=b"!S:CHS_INVALID_ARGUMENT:0:: image zone is Asia/Tokyo, not Europe/Paris",
        )
    assert usage.value.status == 3 and b"Asia/Tokyo" in usage.value.message
    assert not isinstance(usage.value, ArtifactError)


def test_one_image_per_file_a_hardlink_shares_it_and_mismatches_refuse_only_the_request(
    stub_copy, tmp_path, clean_process
) -> None:
    path, predicate = stub_copy()
    link = tmp_path / "linked.so"
    os.link(path, link)
    first = library.open_image(path, predicate, "first")
    again = library.open_image(str(link), predicate, "second")
    assert again is first and first.resolved == "first"  # the record that first opened it
    # A new signed statement the image does not match refuses THAT request...
    bad = dict(predicate, inputs_sha256="0" * 64)
    with pytest.raises(ArtifactCorruptError) as info:
        library.open_image(str(link), bad, "third")
    assert info.value.reason == "build_info_mismatch:inputs_sha256"
    assert info.value.want == "0" * 64
    # ...and the image stays open for the requests it did match.
    assert library.open_image(path, predicate, "fourth") is first
    assert first.validate_type("UInt8")


def test_open_unverified_needs_both_opt_ins_and_has_no_resolved(stub_copy, monkeypatch) -> None:
    path, _ = stub_copy()
    monkeypatch.delenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", raising=False)
    with pytest.raises(UsageError):
        open_unverified(path, allow=True)  # env var unset
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    with pytest.raises(UsageError):
        open_unverified(path)  # allow not passed
    with pytest.warns(UserWarning, match="UNVERIFIED"):
        lib = open_unverified(path, allow=True)
    assert lib.resolved is None
    _setup._reset_for_tests()


def test_a_defaults_failure_at_step_7_clears_the_record_too(
    stub_copy, monkeypatch, clean_process
) -> None:
    # The shared setup case (test_public_setup_cases.py) covers a zone the
    # library refuses; the stub cannot refuse a default, so this forces the
    # chs_set_defaults half of step 7 to fail. The rule is the same: no image
    # has completed step 7, so the record is cleared and a corrected setup is
    # accepted, and the open that follows runs step 7 again under it.
    path, predicate = stub_copy()

    def refuse(self, d):
        raise SchemaError(1, 115, "UNKNOWN_SETTING", b"Unknown setting no_such_setting")

    monkeypatch.setattr(_decls.Api, "set_defaults", refuse)
    setup(timezone="Asia/Tokyo", defaults={"no_such_setting": "1"})
    with pytest.raises(SchemaError) as info:
        library.open_image(path, predicate, None)
    assert info.value.ch_code == 115
    setup(timezone="Asia/Tokyo")  # the corrected setup: no longer refused
    first = library.open_image(path, predicate, None)
    with pytest.raises(UsageError):
        setup(timezone="Asia/Tokyo", defaults={"no_such_setting": "1"})  # now latched
    assert library.open_image(path, predicate, None) is first
