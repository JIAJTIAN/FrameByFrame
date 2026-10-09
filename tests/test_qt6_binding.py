from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"


def test_first_party_gui_process_uses_only_pyside6() -> None:
    import aswaxs_live.app.dashboard  # noqa: F401
    import aswaxs_live.tools.iq_viewer.viewer  # noqa: F401
    import aswaxs_live.tools.online_reducer.app  # noqa: F401
    import aswaxs_live.tools.pyfai_setup.gui  # noqa: F401
    import aswaxs_live.tools.rack_builder.app  # noqa: F401

    assert os.environ["QT_API"] == "pyside6"
    assert "PySide6.QtCore" in sys.modules
    assert not any(name == "PyQt5" or name.startswith("PyQt5.") for name in sys.modules)
    assert not any(name == "PyQt6" or name.startswith("PyQt6.") for name in sys.modules)
    assert "aswaxs_live.tools.linkers.xanos_host" not in sys.modules
    assert "aswaxs_live.tools.linkers.xmodfit_host" not in sys.modules


def test_pyside6_platform_plugin_path_is_explicit_and_valid() -> None:
    from aswaxs_live.app.qt import PYSIDE6_PLATFORM_PLUGIN_PATH, PYSIDE6_PLUGIN_PATH

    assert Path(os.environ["QT_PLUGIN_PATH"]).resolve() == PYSIDE6_PLUGIN_PATH
    assert Path(os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"]).resolve() == PYSIDE6_PLATFORM_PLUGIN_PATH
    assert PYSIDE6_PLATFORM_PLUGIN_PATH.is_dir()
    if sys.platform == "win32":
        assert (PYSIDE6_PLATFORM_PLUGIN_PATH / "qwindows.dll").is_file()


def test_qt_boundary_rejects_preloaded_pyqt5() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(SOURCE_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import PyQt5; import aswaxs_live.app.qt",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "requires PySide6 only" in result.stderr


def test_qt_boundary_rejects_preloaded_pyqt6_when_available() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(SOURCE_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, types; sys.modules['PyQt6'] = types.ModuleType('PyQt6'); "
            "import aswaxs_live.app.qt",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "requires PySide6 only" in result.stderr
