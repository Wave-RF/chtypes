"""apifix: the base without greet, so this must read changed."""

VERSION = "1"


def keep() -> int:
    """Exported and identical in every variant."""
    return 1


def _prefix() -> str:
    return "hello, "
