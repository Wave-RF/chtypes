"""`CHS_CANCELED` maps to `CallCanceledError`, which is not `asyncio.CancelledError`."""

from __future__ import annotations

import asyncio

import chtypes.errors
from chtypes._abi2 import _errmap, _errors


def test_status_canceled_maps_to_call_canceled_error() -> None:
    assert _errmap.call_error_class("CHS_CANCELED") is _errors.CallCanceledError
    assert _errmap.call_error_class("CHS_CANCELED") is chtypes.errors.CallCanceledError


def test_call_canceled_error_is_a_call_error_and_not_asyncio_canceled() -> None:
    assert issubclass(chtypes.errors.CallCanceledError, chtypes.errors.CallError)
    assert not issubclass(chtypes.errors.CallCanceledError, asyncio.CancelledError)
    assert not issubclass(chtypes.errors.CallCanceledError, chtypes.errors.UsageError)
