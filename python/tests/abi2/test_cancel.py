"""`CancelToken` and `cancel=` over the stub library: the six cancelable calls,
a fire before the call and during it, a token that never fires, one source
across many calls, independent sources, live handles back at baseline, and no
fire after free.

The stub's `!B:` directive (a first bytes argument beginning `!B:`) blocks a
call until its token fires; its `!C:` probe (`chs_type_validate`) reads how many
calls are blocked and how many fires reached a freed token. Every wait below is
sequenced on that probe, never on a sleep; the poll only bounds how long a
broken binding can hang a test.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable

import pytest

from chtypes import CallCanceledError, CancelToken, Format, InsertResult, UsageError
from chtypes._abi2 import _decls

CREATE = b"CREATE TABLE t (a UInt8) ENGINE = Memory"
BLOCK = b"!B:1\n"
REASON = "call-canceled"
ENTRY = b"call canceled at entry: the token was fired before the call"
BLOCKED = b"call canceled at the stub's block check point"
_ID = re.compile(rb'\{"cancel":"c[0-9a-f]{16}"\}')


def _probe(lib) -> dict:
    return json.loads(lib.validate_type(b"!C:"))


def _live_cancel(lib) -> int:
    (name,) = [k for k in lib.live_handles() if k.endswith("cancel")]
    return lib.live_handles()[name]


def _calls(lib, schema, flt) -> dict[str, Callable[..., object]]:
    """Each cancelable call, given a first bytes argument and the `cancel=`."""
    return {
        "row": lambda body, **kw: schema.row(Format.CSV, body, **kw),
        "rows": lambda body, **kw: schema.rows(Format.CSV, body, **kw),
        "insert": lambda body, **kw: schema.insert(Format.CSV, body, Format.CSV, **kw),
        "parse_block": lambda body, **kw: schema.parse_block(Format.CSV, body, **kw).close(),
        "filter_rows": lambda body, **kw: flt.rows(Format.CSV, body, **kw),
        "compile_table": lambda body, **kw: lib.compile_table(body, **kw).close(),
    }


CALLS = ["row", "rows", "insert", "parse_block", "filter_rows", "compile_table"]


@pytest.fixture
def handles(lib):
    schema = lib.compile_table(CREATE)
    flt = schema.compile_filter("a > 1")
    yield _calls(lib, schema, flt)
    flt.close()
    schema.close()


def _wait_inflight(lib, want: int) -> None:
    for _ in range(5000):
        if _probe(lib)["inflight"] >= want:
            return
        time.sleep(0.001)
    raise AssertionError(f"the stub never read {want} calls in flight: {_probe(lib)}")


class _Worker(threading.Thread):
    """Runs one call; keeps its answer or its exception for the main thread."""

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__(daemon=True)
        self._fn = fn
        self.result: object = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            self.error = exc


def _assert_canceled(err: BaseException | None, message: bytes) -> None:
    assert isinstance(err, CallCanceledError), repr(err)
    assert err.reason == REASON
    assert err.message == message, err.message


# ---------------------------------------------------------------- the token


def test_cancel_token_is_idempotent_and_reports_canceled() -> None:
    tok = CancelToken()
    assert not tok.canceled
    tok.cancel()
    tok.cancel()
    assert tok.canceled


def test_cancel_token_cancel_from_many_threads_fires_each_call_once() -> None:
    tok = CancelToken()
    fired: list[int] = []
    keys = [tok._register(lambda i=i: fired.append(i)) for i in range(8)]
    threads = [threading.Thread(target=tok.cancel) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(fired) == list(range(8))
    for k in keys:
        tok._unregister(k)


def test_a_fire_registered_after_cancel_runs_at_once() -> None:
    tok = CancelToken()
    tok.cancel()
    fired: list[int] = []
    assert tok._register(lambda: fired.append(1)) is None
    assert fired == [1]


def test_cancel_must_be_a_cancel_token(lib) -> None:
    with lib.compile_table(CREATE) as schema, pytest.raises(TypeError, match="CancelToken"):
        schema.row(Format.CSV, b"1", cancel=object())  # type: ignore[arg-type]


# ------------------------------------------------------------- fired before


@pytest.mark.parametrize("name", CALLS)
def test_a_token_fired_before_the_call_cancels_it_at_entry(lib, handles, name) -> None:
    base = _live_cancel(lib)
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(CallCanceledError) as info:
        handles[name](b"1\n" if name != "compile_table" else CREATE, cancel=tok)
    _assert_canceled(info.value, ENTRY)
    assert _live_cancel(lib) == base


# ------------------------------------------------------------- fired during


@pytest.mark.parametrize("name", CALLS)
def test_a_token_fired_during_the_call_cancels_it(lib, handles, name) -> None:
    """The worker sits inside C, blocked on its token, while this thread polls
    the probe and cancels: that only works if the call released the GIL."""
    base = _live_cancel(lib)
    before = _probe(lib)["inflight"]
    tok = CancelToken()
    w = _Worker(lambda: handles[name](BLOCK, cancel=tok))
    w.start()
    _wait_inflight(lib, before + 1)
    assert w.is_alive()
    tok.cancel()
    w.join(10)
    assert not w.is_alive()
    _assert_canceled(w.error, BLOCKED)
    assert _live_cancel(lib) == base
    assert _probe(lib) == {"fire_after_free": 0, "inflight": before}


def test_cancel_after_last_check_point(lib, handles) -> None:
    """A cancel that arrives after the library's last check point finds a
    complete answer, and the call returns it: a complete answer is never
    turned into a cancel. The stub's `!L:` body passes its last check point,
    waits for the token, then answers its echo."""
    body = b"!L:late"
    want = handles["row"](body)
    base = _live_cancel(lib)
    before = _probe(lib)["inflight"]
    tok = CancelToken()
    w = _Worker(lambda: handles["row"](body, cancel=tok))
    w.start()
    _wait_inflight(lib, before + 1)
    assert w.is_alive()
    tok.cancel()
    w.join(10)
    assert not w.is_alive()
    assert w.error is None, repr(w.error)
    assert w.result == want
    assert tok.canceled
    assert _live_cancel(lib) == base
    assert _probe(lib) == {"fire_after_free": 0, "inflight": before}


# -------------------------------------------------------------- never fired


def _spy(lib, monkeypatch, name: str) -> list[tuple]:
    calls: list[tuple] = []
    real = getattr(lib._api, name)

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(lib._api, name, spy)
    return calls


_FN = {
    "row": "preview_row",
    "rows": "preview_batch",
    "insert": "preview_insert",
    "parse_block": "block_create",
    "filter_rows": "filter_eval_body",
    "compile_table": "schema_create",
}


@pytest.mark.parametrize("name", CALLS)
def test_a_token_never_fired_changes_nothing_but_the_options_id(
    lib, handles, monkeypatch, name
) -> None:
    """Every argument but the options document is the same as without a token,
    and that document is exactly the call's own cancel id; the answer is the
    same answer."""
    spy = _spy(lib, monkeypatch, _FN[name])
    body = b"1\n" if name != "compile_table" else CREATE
    plain = handles[name](body)
    tok = CancelToken()
    with_tok = handles[name](body, cancel=tok)
    (a, b) = spy
    assert a[:-1] == b[:-1]
    assert a[-1] is None and _ID.fullmatch(b[-1]), b[-1]
    if isinstance(plain, InsertResult):
        assert plain.outcome == with_tok.outcome  # type: ignore[union-attr]
    elif plain is not None:
        assert type(plain) is type(with_tok)
    assert not tok.canceled and _live_cancel(lib) == 0


def test_the_cancel_id_joins_the_other_options_and_wins_over_an_extra(lib, monkeypatch) -> None:
    rows = _spy(lib, monkeypatch, "preview_row")
    schemas = _spy(lib, monkeypatch, "schema_create")
    with lib.compile_table(CREATE, posture="load", cancel=CancelToken()) as schema:
        schema.row(
            Format.CSV,
            b"1",
            verdict_basis="export",
            insert_options_extra={"cancel": '"c0000000000000000"'},
            cancel=CancelToken(),
        )
    opts = json.loads(rows[0][-1])
    assert opts["verdict_basis"] == "export"
    assert re.fullmatch("c[0-9a-f]{16}", opts["cancel"]) and opts["cancel"] != "c" + "0" * 16
    sopts = json.loads(schemas[0][-1])
    assert sopts["posture"] == "load" and re.fullmatch("c[0-9a-f]{16}", sopts["cancel"])


def test_no_source_creates_no_token_and_makes_no_extra_call(lib, monkeypatch) -> None:
    def refuse(*_a):
        raise AssertionError("a call with no cancel source created a token")

    monkeypatch.setattr(lib._api, "cancel_create", refuse)
    monkeypatch.setattr(lib._api, "cancel_fire", refuse)
    with lib.compile_table(CREATE) as schema, schema.compile_filter("a > 1") as flt:
        for fn in _calls(lib, schema, flt).values():
            fn(b"1\n")


def test_misuse_is_raised_before_any_token_is_created(lib, monkeypatch) -> None:
    created = _spy(lib, monkeypatch, "cancel_create")
    schema = lib.compile_table(CREATE)
    schema.close()
    with pytest.raises(UsageError):
        schema.row(Format.CSV, b"1", cancel=CancelToken())
    with pytest.raises(UsageError):
        lib.compile_table(CREATE, schema_options_extra={"x": "not json"}, cancel=CancelToken())
    assert created == []


# ------------------------------------------------------- one source, many


def test_one_source_across_concurrent_calls_cancels_all_of_them(lib, handles) -> None:
    before = _probe(lib)["inflight"]
    tok = CancelToken()
    workers = [_Worker(lambda n=n: handles[n](BLOCK, cancel=tok)) for n in CALLS]
    for w in workers:
        w.start()
    _wait_inflight(lib, before + len(workers))
    tok.cancel()
    for w in workers:
        w.join(10)
        _assert_canceled(w.error, BLOCKED)
    assert _live_cancel(lib) == 0


def test_independent_sources_cancel_only_their_own_call(lib, handles) -> None:
    before = _probe(lib)["inflight"]
    a, b = CancelToken(), CancelToken()
    wa = _Worker(lambda: handles["rows"](BLOCK, cancel=a))
    wb = _Worker(lambda: handles["row"](BLOCK, cancel=b))
    wa.start()
    wb.start()
    _wait_inflight(lib, before + 2)
    a.cancel()
    wa.join(10)
    _assert_canceled(wa.error, BLOCKED)
    assert wb.is_alive() and _probe(lib)["inflight"] == before + 1
    assert not b.canceled
    b.cancel()
    wb.join(10)
    _assert_canceled(wb.error, BLOCKED)


# ------------------------------------------------------------ races, soak


def test_racing_cancels_and_returns_leave_no_handle_and_no_fire_after_free(lib, handles) -> None:
    """Calls that return on their own race a cancel from another thread, over
    and over; then calls blocked in C are canceled. Every C token is freed, and
    no fire ever reaches a freed one."""
    base = _live_cancel(lib)
    for i in range(300):
        tok = CancelToken()
        name = CALLS[i % len(CALLS)]
        body = b"1\n" if name != "compile_table" else CREATE
        w = _Worker(lambda tok=tok, name=name, body=body: handles[name](body, cancel=tok))
        c = threading.Thread(target=tok.cancel)
        if i % 2:
            c.start()
            w.start()
        else:
            w.start()
            c.start()
        w.join(10)
        c.join(10)
        assert w.error is None or isinstance(w.error, CallCanceledError), repr(w.error)
    for _ in range(20):
        before = _probe(lib)["inflight"]
        tok = CancelToken()
        ws = [_Worker(lambda n=n, tok=tok: handles[n](BLOCK, cancel=tok)) for n in CALLS]
        for w in ws:
            w.start()
        _wait_inflight(lib, before + len(ws))
        tok.cancel()
        for w in ws:
            w.join(10)
            _assert_canceled(w.error, BLOCKED)
    assert _live_cancel(lib) == base
    assert _probe(lib)["fire_after_free"] == 0


def test_planted_red_a_fire_kept_past_the_free_is_counted(lib, handles, monkeypatch) -> None:
    """The probe this suite relies on does see a fire after free: with the drain
    removed (the call never unregisters) and the fire holding the raw pointer
    rather than the handle the binding closes, the late `cancel()` reaches the
    freed token and the stub counts it. The real path keeps the drain, and the
    soak above holds the counter at 0."""
    real_fire = lib._api.cancel_fire

    def stale_fire(handle):
        # A copy of the pointer that the binding's close does not clear.
        real_fire(_decls.CancelHandle(stale_fire.ptr, lambda _p: None))

    monkeypatch.setattr("chtypes._cancel.CancelToken._unregister", lambda self, key: None)
    real_create = lib._api.cancel_create

    def create():
        handle, cid = real_create()
        stale_fire.ptr = handle._ptr
        return handle, cid

    monkeypatch.setattr(lib._api, "cancel_create", create)
    monkeypatch.setattr(lib._api, "cancel_fire", stale_fire)
    tok = CancelToken()
    handles["row"](b"1\n", cancel=tok)
    assert _probe(lib)["fire_after_free"] == 0
    tok.cancel()
    assert _probe(lib)["fire_after_free"] == 1
