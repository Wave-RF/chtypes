"""apifix: greet gains an optional parameter, so this must read changed.

`griffe check` alone would not report it: an optional parameter breaks no
caller, and that command reports only breaking changes.
"""

VERSION = "1"


def greet(name: str, excited: bool = False) -> str:
    """Return a greeting for name."""
    return _prefix() + name + ("!" if excited else "")


def keep() -> int:
    """Exported and identical in every variant."""
    return 1


def _prefix() -> str:
    return "hello, "
