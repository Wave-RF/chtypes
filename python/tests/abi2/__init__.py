"""A real package (not pytest's rootless import mode) so test_conformance.py
and test_loader.py can both do `from .conftest import ...`, and so each case
in tests/fixtures/abi-v2/cases.json is parametrized into EXACTLY ONE test
item across the two files (by kind), rather than one file skipping what the
other runs -- a skip raises `Skipped` during the "call" phase like any other
exception, which `conftest.py`'s `pytest_runtest_makereport` would otherwise
have to special-case to avoid recording a skip as a FAILED case in the
report it writes.
"""
