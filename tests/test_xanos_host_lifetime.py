from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"


def test_xanos_host_retains_window_through_event_loop() -> None:
    code = r"""
import gc
import sys
import weakref
from types import SimpleNamespace

from aswaxs_live.tools.linkers import xanos_host

window_reference = None

class Window:
    def close(self):
        pass

class FakeApplication:
    @staticmethod
    def instance():
        return None

    def __init__(self, _args):
        pass

    def setApplicationName(self, _name):
        pass

    def exec_(self):
        gc.collect()
        assert window_reference() is not None, "XAnoS window was released before the Qt event loop"
        return 0

def open_window(*_args, **_kwargs):
    global window_reference
    window = Window()
    window_reference = weakref.ref(window)
    return window

xanos_host.QtWidgets = SimpleNamespace(QApplication=FakeApplication)
xanos_host.open_xanos_components_window = open_window
sys.argv = ["xanos_host", "--script", "unused.py"]
raise SystemExit(xanos_host.main())
"""
    environment = os.environ.copy()
    environment["QT_API"] = "pyqt5"
    environment.pop("QT_PLUGIN_PATH", None)
    environment.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(SOURCE_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _pyqt5_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["QT_API"] = "pyqt5"
    env.pop("QT_PLUGIN_PATH", None)
    env.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SOURCE_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return env


def _run_pyqt5(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=_pyqt5_subprocess_env(),
        capture_output=True,
        text=True,
    )


def test_xanos_save_handoff_shows_error_when_original_save_raises(tmp_path: Path) -> None:
    """When original save_ASAXS raises, the handoff shows an error dialog instead of swallowing it."""
    code = r"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from aswaxs_live.tools.linkers import xanos_host

warnings_shown = []

class FakeMessageBox:
    @staticmethod
    def warning(parent, title, message):
        warnings_shown.append({"title": title, "message": message})

xanos_host.QtWidgets.QMessageBox = FakeMessageBox

class Window:
    saveASAXSPushButton = None

    def save_ASAXS(self):
        raise RuntimeError("Disk is full")

window = Window()
xanos_host._install_xmodfit_save_handoff(window)
window.save_ASAXS()
assert len(warnings_shown) == 1, f"Expected one warning dialog, got {warnings_shown}"
assert "Disk is full" in warnings_shown[0]["message"], warnings_shown
"""
    result = _run_pyqt5(code)
    assert result.returncode == 0, result.stderr


def test_xanos_save_handoff_shows_error_when_xmodfit_launch_fails(tmp_path: Path) -> None:
    """When open_xmodfit_window raises an unexpected error, the handoff shows a warning dialog."""
    output = tmp_path / "components.txt"
    output.write_text("# Q SAXS-term\n", encoding="utf-8")
    code = rf"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from aswaxs_live.tools.linkers import xanos_host

warnings_shown = []

class FakeMessageBox:
    @staticmethod
    def warning(parent, title, message):
        warnings_shown.append({{"title": title, "message": message}})

xanos_host.QtWidgets.QMessageBox = FakeMessageBox

class Window:
    saveASAXSPushButton = None

    def save_ASAXS(self):
        xanos_host.QtWidgets.QFileDialog.getSaveFileName()

xanos_host.QtWidgets.QFileDialog.getSaveFileName = staticmethod(
    lambda *_args, **_kwargs: (r"{output}", "Text files (*.txt)")
)

def failing_open_xmodfit(path=None, *, data_files=None):
    raise ValueError("Unexpected internal error")

xanos_host.open_xmodfit_window = failing_open_xmodfit
window = Window()
xanos_host._install_xmodfit_save_handoff(window)
window.save_ASAXS()
assert len(warnings_shown) == 1, f"Expected one warning dialog, got {{warnings_shown}}"
assert "Cannot Send to XModFit" in warnings_shown[0]["title"], warnings_shown
assert "Unexpected internal error" in warnings_shown[0]["message"], warnings_shown
"""
    result = _run_pyqt5(code)
    assert result.returncode == 0, result.stderr


def test_xanos_save_handoff_shows_error_when_file_not_written(tmp_path: Path) -> None:
    """If the dialog returns a path but the file doesn't exist, the handoff warns instead of silently doing nothing."""
    missing = tmp_path / "never_written.txt"
    code = rf"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from aswaxs_live.tools.linkers import xanos_host

warnings_shown = []

class FakeMessageBox:
    @staticmethod
    def warning(parent, title, message):
        warnings_shown.append({{"title": title, "message": message}})

xanos_host.QtWidgets.QMessageBox = FakeMessageBox

class Window:
    saveASAXSPushButton = None

    def save_ASAXS(self):
        xanos_host.QtWidgets.QFileDialog.getSaveFileName()

xanos_host.QtWidgets.QFileDialog.getSaveFileName = staticmethod(
    lambda *_args, **_kwargs: (r"{missing}", "Text files (*.txt)")
)

launched = []
xanos_host.open_xmodfit_window = lambda **kwargs: launched.append(kwargs)
window = Window()
xanos_host._install_xmodfit_save_handoff(window)
window.save_ASAXS()
assert launched == [], f"XModFit should not be opened when file is missing, got {{launched}}"
assert len(warnings_shown) == 1, f"Expected one warning dialog, got {{warnings_shown}}"
assert "Cannot Send to XModFit" in warnings_shown[0]["title"], warnings_shown
"""
    result = _run_pyqt5(code)
    assert result.returncode == 0, result.stderr


def test_xanos_save_handoff_forwards_saved_components_to_xmodfit(tmp_path: Path) -> None:
    output = tmp_path / "components.txt"
    output.write_text("# Q SAXS-term Cross-term Resonant-term\n", encoding="utf-8")
    code = rf"""
from pathlib import Path
from aswaxs_live.tools.linkers import xanos_host

forwarded = []

class StatusBar:
    def showMessage(self, _message):
        pass

class Window:
    saveASAXSPushButton = None

    def save_ASAXS(self):
        xanos_host.QtWidgets.QFileDialog.getSaveFileName()

    def statusBar(self):
        return StatusBar()

xanos_host.QtWidgets.QFileDialog.getSaveFileName = staticmethod(
    lambda *_args, **_kwargs: (r"{output}", "Text files (*.txt)")
)
xanos_host.open_xmodfit_window = lambda **kwargs: forwarded.append(kwargs["data_files"])
window = Window()
xanos_host._install_xmodfit_save_handoff(window)
window.save_ASAXS()
assert forwarded == [[Path(r"{output}")]]
"""
    environment = os.environ.copy()
    environment["QT_API"] = "pyqt5"
    environment.pop("QT_PLUGIN_PATH", None)
    environment.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(SOURCE_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
