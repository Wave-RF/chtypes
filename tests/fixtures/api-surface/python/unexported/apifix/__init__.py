"""apifix: only private code and docstrings differ from the base, so this must read unchanged."""

VERSION = "1"


def greet(name: str) -> str:
    """Return a friendly greeting for name, built by a renamed helper."""
    return _salutation() + name


def keep() -> int:
    """Exported and identical in every variant."""
    return 1


def _salutation() -> str:
    return "hi, "


class _State:
    calls = 0
