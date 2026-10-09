"""pyFAI PONI, mask, and radial-integration setup tool."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name in {"PreprocessingWindow", "run_app"}:
        from aswaxs_live.tools.pyfai_setup import gui

        return getattr(gui, name)
    raise AttributeError(name)

__all__ = ["PreprocessingWindow", "run_app"]
