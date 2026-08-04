"""Single Qt binding boundary for all first-party FrameByFrame GUIs.

Import this module before Matplotlib, pyqtgraph, or pyFAI GUI modules.
External Qt5 applications are launched in separate processes and must never be
imported through this module.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


os.environ["QT_API"] = "pyside6"


def _configure_pyside6_plugins() -> tuple[Path, Path]:
    """Pin Qt's plugin lookup to this interpreter's PySide6 installation."""
    spec = importlib.util.find_spec("PySide6")
    locations = spec.submodule_search_locations if spec is not None else None
    if not locations:
        raise RuntimeError("PySide6 is not installed in the active Python environment.")
    package_dir = Path(next(iter(locations))).resolve()
    plugin_dir = package_dir / "plugins"
    platform_dir = plugin_dir / "platforms"
    if not platform_dir.is_dir():
        raise RuntimeError(f"PySide6 platform plugins were not found at: {platform_dir}")
    os.environ["QT_PLUGIN_PATH"] = str(plugin_dir)
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(platform_dir)
    return plugin_dir, platform_dir


PYSIDE6_PLUGIN_PATH, PYSIDE6_PLATFORM_PLUGIN_PATH = _configure_pyside6_plugins()


def _loaded_module(prefix: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for name in sys.modules)


def assert_no_mixed_qt() -> None:
    """Reject a process that already contains a competing Qt binding."""
    loaded = [binding for binding in ("PyQt5", "PyQt6") if _loaded_module(binding)]
    if loaded:
        names = ", ".join(loaded)
        raise RuntimeError(
            f"FrameByFrame v6 requires PySide6 only; competing Qt binding loaded: {names}. "
            "Launch Qt5 external tools in their isolated subprocesses."
        )


assert_no_mixed_qt()

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from PySide6.QtCore import Property, Signal, Slot  # noqa: E402


QT_API = "pyside6"
QAction = QtGui.QAction
QActionGroup = QtGui.QActionGroup
QShortcut = QtGui.QShortcut


__all__ = [
    "Property",
    "PYSIDE6_PLATFORM_PLUGIN_PATH",
    "PYSIDE6_PLUGIN_PATH",
    "QAction",
    "QActionGroup",
    "QShortcut",
    "QT_API",
    "QtCore",
    "QtGui",
    "QtWidgets",
    "Signal",
    "Slot",
    "assert_no_mixed_qt",
]
