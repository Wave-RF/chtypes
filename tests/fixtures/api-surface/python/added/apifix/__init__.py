"""apifix: the base plus one public function, so this must read changed."""

VERSION = "1"


def greet(name: str) -> str:
    """Return a greeting for name."""
    return _prefix() + name


def keep() -> int:
    """Exported and identical in every variant."""
    return 1


def farewell(name: str) -> str:
    """The addition."""
    return "bye, " + name


def _prefix() -> str:
    return "hello, "
