"""Main application GUI and shared Qt support."""

from __future__ import annotations

from typing import Any


def main() -> int:
    """Lazily import the GUI launcher so non-GUI helpers do not load Qt."""
    from .launcher import main as launch

    return launch()

__all__ = ["main"]
