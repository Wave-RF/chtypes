"""The example tour compiles, imports, and names every section, in order.

The tour itself runs only against a real artifact (examples/chplay.sh, the
`examples` CI job); this is the check that needs none, so a rename in the public
API that the tour still spells the old way fails here, at `import`, not there.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

DEMO = Path(__file__).resolve().parents[2] / "examples" / "python" / "demo.py"
SECTIONS = 21


def test_the_tour_defines_every_section_in_order() -> None:
    tree = ast.parse(DEMO.read_text(encoding="utf-8"), filename=str(DEMO))
    numbered = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "section"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            numbered.append(node.args[0].value)
    assert sorted(numbered) == list(range(1, SECTIONS + 1))


def test_the_tour_imports_and_every_section_function_exists() -> None:
    spec = importlib.util.spec_from_file_location("chtypes_tour_demo", DEMO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for n in range(1, SECTIONS + 1):
        assert callable(getattr(module, f"section{n}")), f"section{n}"
    assert callable(module.main)


def _tour():
    spec = importlib.util.spec_from_file_location("chtypes_tour_demo_gate", DEMO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_advertised_insert_capability_never_skips() -> None:
    import pytest

    tour = _tour()
    features = {tour.INSERT_FEATURE, tour.MEMORY_FEATURE}
    formats = {tour.NATIVE_FORMAT}
    for n in (19, 20, 21):
        # Advertised: the section runs, and a decline anyway fails the tour.
        assert tour.capability_gate(n, features, formats) is False
        with pytest.raises(SystemExit):
            tour.capability_gate(n, features, formats, declined=True)
        # Not advertised: a named skip, never a failure.
        assert tour.capability_gate(n, set(), set(), declined=True) is True
    assert tour.capability_gate(20, features, set()) is True
    assert tour.capability_gate(21, {tour.INSERT_FEATURE}, formats) is True
