"""ZMQ-driven online reduction using FrameByFrame's live reduction engine."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name in {"MainWindow", "main"}:
        from . import app

        return getattr(app, name)
    if name == "OnlineConfig":
        from .config import OnlineConfig

        return OnlineConfig
    if name == "OnlineReductionEngine":
        from .engine import OnlineReductionEngine

        return OnlineReductionEngine
    raise AttributeError(name)

__all__ = ["MainWindow", "OnlineConfig", "OnlineReductionEngine", "main"]
