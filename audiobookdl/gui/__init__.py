"""Desktop GUI package (lazy entry points so headless helpers import without tkinter)."""

from typing import Any

__all__ = ["AudiobookDLApp", "main"]


def __getattr__(name: str) -> Any:
    if name in ("AudiobookDLApp", "main"):
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
