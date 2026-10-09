"""The server profile (`Library.new_server`, `compile_table(server=)`, `Server`
and the description's `server` and `replicated` members). The profile document is
checked byte for byte with no library; everything else runs over the ABI v2 test
stub, whose `chs_schema_create` takes a counted reference to the server it
RECEIVES, so the stub's own live-handle counters say whether a non-NULL server
reached it."""

from __future__ import annotations

import gc
import json
import threading

import pytest

from chtypes import (
    DeclinedSetting,
    DeclinedTier,
    SchemaReplicated,
    SchemaServer,
    ServerProfile,
    Status,
    UsageError,
)
from chtypes import _decode as decode
from chtypes._input import server_profile_json
from chtypes.errors import InternalError

CREATE = b"CREATE TABLE t (k UInt8) ENGINE = Memory"


def _servers(lib) -> int:
    (name,) = [k for k in lib.live_handles() if k.endswith("server")]
    return lib.live_handles()[name]


@pytest.mark.parametrize(
    ("profile", "want"),
    [
        (ServerProfile(), b"{}"),
        (ServerProfile(timezone=""), b"{}"),
        (ServerProfile(timezone="Asia/Tokyo"), b'{"timezone":"Asia/Tokyo"}'),
        (ServerProfile(timezone="UTC", settings=None), b'{"timezone":"UTC"}'),
        (ServerProfile(settings={}), b'{"settings":{}}'),
        (ServerProfile(macros=None), b"{}"),
        (ServerProfile(macros={}), b'{"macros":{}}'),
        (
            ServerProfile(
                timezone="Europe/Berlin",
                settings={"max_threads": "8"},
                macros={"shard": "01", "replica": "r1"},
            ),
            b'{"timezone":"Europe/Berlin","settings":{"max_threads":"8"},'
            b'"macros":{"shard":"01","replica":"r1"}}',
        ),
        # Passed through as given: the library judges the zone, the setting, the macro.
        (
            ServerProfile(
                timezone="Not/AZone",
                settings={"no_such_setting": "eight"},
                macros={"": "x", "a.b": "<&>"},
            ),
            b'{"timezone":"Not/AZone","settings":{"no_such_setting":"eight"},'
            b'"macros":{"":"x","a.b":"<&>"}}',
        ),
    ],
)
def test_the_profile_document_byte_for_byte(profile, want) -> None:
    assert server_profile_json(profile) == want


def test_absent_macros_is_not_empty_macros() -> None:
    absent = server_profile_json(ServerProfile(timezone="UTC"))
    empty = server_profile_json(ServerProfile(timezone="UTC", macros={}))
    assert absent != empty
    assert b"macros" not in absent and b'"macros":{}' in empty


def test_a_profile_is_a_server_profile_and_maps_strings_to_strings() -> None:
    with pytest.raises(TypeError):
        server_profile_json({"timezone": "UTC"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        server_profile_json(ServerProfile(settings={"a": 1}))  # type: ignore[dict-item]


def test_every_profile_member_is_the_descriptions_document(lib) -> None:
    # The stub's closed-document check, generated from input:server_profile,
    # refuses a key the description does not list.
    with lib.new_server(ServerProfile(timezone="Asia/Tokyo", settings={"a": "b"}, macros={})):
        pass


def test_new_server_and_close_twice(lib) -> None:
    srv = lib.new_server(ServerProfile(timezone="UTC"))
    assert _servers(lib) == 1
    srv.close()
    srv.close()  # idempotent
    assert _servers(lib) == 0


def test_the_server_is_a_context_manager(lib) -> None:
    with lib.new_server(ServerProfile()) as srv:
        assert _servers(lib) == 1
        assert srv is not None
    assert _servers(lib) == 0


def test_options_documents_are_empty(lib, monkeypatch) -> None:
    seen: list[tuple] = []
    real_create, real_schema = lib._api.server_create, lib._api.schema_create

    def spy_server(profile, options):
        seen.append(("server", profile, options))
        return real_create(profile, options)

    def spy_schema(server, create_table, settings, options):
        seen.append(("schema", server is not None, options))
        return real_schema(server, create_table, settings, options)

    monkeypatch.setattr(lib._api, "server_create", spy_server)
    monkeypatch.setattr(lib._api, "schema_create", spy_schema)
    with lib.new_server(ServerProfile(timezone="UTC")) as srv:
        lib.compile_table(CREATE, server=srv).close()
    assert seen == [("server", b'{"timezone":"UTC"}', None), ("schema", True, None)]


def test_compile_on_a_server_passes_a_non_null_server_to_the_library(lib) -> None:
    srv = lib.new_server(ServerProfile(timezone="Asia/Tokyo"))
    schema = lib.compile_table(CREATE, server=srv, settings={"a": "b"})
    srv.close()
    # The stub's chs_schema_create holds the server it RECEIVED: still live
    # after the caller closed it, exactly when the schema got a non-NULL server.
    assert _servers(lib) == 1
    schema.describe()  # a schema whose server the caller closed keeps working
    schema.close()
    assert _servers(lib) == 0


@pytest.mark.parametrize("kwargs", [{}, {"server": None}], ids=["omitted", "None"])
def test_no_server_means_the_library_receives_null(lib, kwargs) -> None:
    srv = lib.new_server(ServerProfile())
    plain = lib.compile_table(CREATE, **kwargs)
    srv.close()
    assert _servers(lib) == 0  # a schema with no server holds none
    plain.close()


def test_a_closed_server_is_refused_before_any_call(lib, monkeypatch) -> None:
    srv = lib.new_server(ServerProfile(timezone="UTC"))
    srv.close()
    monkeypatch.setattr(
        lib._api, "schema_create", lambda *a, **k: pytest.fail("a closed server reached C")
    )
    with pytest.raises(UsageError, match="Server is closed"):
        lib.compile_table(CREATE, server=srv)


def test_a_server_must_be_a_server(lib) -> None:
    with pytest.raises(TypeError):
        lib.compile_table(CREATE, server=object())  # type: ignore[arg-type]


def test_a_server_from_another_library_is_the_librarys_to_refuse(stub_copy, monkeypatch) -> None:
    from chtypes import open_unverified

    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    with pytest.warns(UserWarning):
        a = open_unverified(stub_copy()[0], allow=True)
        b = open_unverified(stub_copy()[0], allow=True)
    with a.new_server(ServerProfile()) as srv, pytest.raises(UsageError) as info:
        b.compile_table(CREATE, server=srv)
    assert info.value.status == Status.INVALID_ARGUMENT


def test_a_server_and_its_schemas_close_in_any_order(lib) -> None:
    srv = lib.new_server(ServerProfile())
    first, second = lib.compile_table(CREATE, server=srv), lib.compile_table(CREATE, server=srv)
    first.close()
    srv.close()
    second.describe()
    second.close()
    assert _servers(lib) == 0


def test_concurrent_compiles_and_close(lib) -> None:
    srv = lib.new_server(ServerProfile(timezone="UTC"))
    stop = threading.Event()
    bad: list[BaseException] = []

    def work() -> None:
        while not stop.is_set():
            try:
                lib.compile_table(CREATE, server=srv).close()
            except UsageError:
                pass  # the close guard, after close
            except BaseException as e:  # noqa: BLE001
                bad.append(e)
                return

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    threading.Event().wait(0.05)
    srv.close()
    threading.Event().wait(0.02)
    stop.set()
    for t in threads:
        t.join(10)
    assert bad == []
    assert _servers(lib) == 0


def test_an_abandoned_server_is_freed(lib) -> None:
    def churn() -> None:
        srv = lib.new_server(ServerProfile())
        lib.compile_table(CREATE, server=srv)  # abandoned, never closed

    churn()
    gc.collect()
    assert lib.live_handles() == {k: 0 for k in lib.live_handles()}


_COLS = {"columns": [{"name": "k", "type": "UInt8", "default_kind": "", "default_expression": ""}]}


def _describe(**extra) -> object:
    return decode.decode_schema_description(json.dumps({**_COLS, **extra}).encode())


def test_a_description_without_a_server_has_neither_member() -> None:
    d = _describe()
    assert d.server is None and d.replicated is None and len(d.columns) == 1


def test_the_stubs_own_description_carries_no_server(lib) -> None:
    with lib.compile_table(CREATE) as schema:
        d = schema.describe()
    assert d.server is None and d.replicated is None


def test_the_server_member_decodes_with_macros_absent_or_present() -> None:
    d = _describe(server={"timezone": "UTC", "settings": {}})
    assert d.server == SchemaServer(timezone="UTC", settings={}, macros=None)
    d = _describe(server={"timezone": "Asia/Tokyo", "settings": {"max_threads": "8"}, "macros": {}})
    assert d.server == SchemaServer("Asia/Tokyo", {"max_threads": "8"}, {})
    assert d.server.macros is not None and len(d.server.macros) == 0  # {} is not absent


def test_macros_a_replicated_path_and_unknown_members() -> None:
    d = _describe(
        server={"timezone": "UTC", "settings": {}, "macros": {"shard": "01"}, "x_future": 1},
        replicated={
            "zookeeper_path": "/clickhouse/tables/01/db/t",
            "replica_name_b64": "/3Ix",
            "x_future": {"a": [1]},
        },
        x_future_top=[1],
    )
    assert d.server.macros == {"shard": "01"}
    assert d.replicated == SchemaReplicated(b"/clickhouse/tables/01/db/t", b"\xffr1")


@pytest.mark.parametrize(
    "extra",
    [
        {"server": "UTC"},
        {"server": {"timezone": "UTC", "settings": {"max_threads": 8}}},
        {"server": {"timezone": "UTC", "settings": {}, "macros": []}},
        {"server": {"timezone": 1, "settings": {}}},
        {"replicated": {"zookeeper_path": "/a", "zookeeper_path_b64": "L2E=", "replica_name": "r"}},
        {"replicated": {"zookeeper_path": "/a", "replica_name_b64": "*"}},
    ],
)
def test_a_malformed_server_or_replicated_member_is_an_internal_error(extra) -> None:
    with pytest.raises(InternalError, match="schema_description"):
        _describe(**extra)


_PROFILE = {
    "timezone": "UTC",
    "settings": {"final": "1", "aggregate_functions_null_for_empty": "1"},
}


def _declined(entries) -> tuple:
    d = _describe(server={**_PROFILE, "filter_declined_settings": entries})
    return d.server.filter_declined_settings


def test_filter_declined_settings_empty_and_absent() -> None:
    # [] is present and empty; absent (a document of no build at this
    # fingerprint) reads as empty too. Neither is a failure.
    assert _declined([]) == ()
    absent = _describe(server={"timezone": "UTC", "settings": {}})
    assert absent.server.filter_declined_settings == ()


def test_filter_declined_settings_in_profile_order_b64_and_an_unknown_tier() -> None:
    got = _declined(
        [
            {"name": "final", "tier": "result-content", "x_future": 1},
            {"name": "aggregate_functions_null_for_empty", "tier": "predicate"},
            {"name_b64": "eP95", "tier": "predicate-unflipped"},
            {"name": "x_future_setting", "tier": "x_future_tier", "x_future_obj": {"a": [1]}},
        ]
    )
    assert got == (
        DeclinedSetting(b"final", DeclinedTier.RESULT_CONTENT),
        DeclinedSetting(b"aggregate_functions_null_for_empty", DeclinedTier.PREDICATE),
        DeclinedSetting(b"x\xffy", DeclinedTier.PREDICATE_UNFLIPPED),
        DeclinedSetting(b"x_future_setting", DeclinedTier.of("x_future_tier")),
    )
    # Rule r3: an unlisted tier is kept as it came, as its unknown(n).
    assert [e.tier.known for e in got] == [True, True, True, False]
    assert got[3].tier == "x_future_tier"


@pytest.mark.parametrize(
    "entries",
    [
        {"name": "final", "tier": "predicate"},
        ["final"],
        [{"name": "final", "name_b64": "ZmluYWw=", "tier": "predicate"}],
        [{"tier": "predicate"}],
        [{"name_b64": "*", "tier": "predicate"}],
        [{"name": "final", "tier": 1}],
    ],
)
def test_a_malformed_filter_declined_settings_is_an_internal_error(entries) -> None:
    with pytest.raises(InternalError, match="schema_description"):
        _declined(entries)
