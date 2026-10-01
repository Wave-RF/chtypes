"""apifix: a fixture for scripts/api-surface.py, the base API."""

VERSION = "1"


def greet(name: str) -> str:
    """Return a greeting for name."""
    return _prefix() + name


def keep() -> int:
    """Exported and identical in every variant."""
    return 1


def _prefix() -> str:
    return "hello, "
