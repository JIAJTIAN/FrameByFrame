"""Application entry point shared by installed and source-tree launchers."""

from __future__ import annotations

import sys

from aswaxs_live.app.qt import QtWidgets
from aswaxs_live.app.qt_runtime import suppress_glx_warning


def _quiet_server_glx_warning() -> None:
    """Suppress noisy Qt/GLX warnings seen on remote Linux displays."""
    suppress_glx_warning()


def require_python_310(version_info: object = sys.version_info) -> None:
    """Fail clearly when a source-tree launch bypasses package metadata."""
    major_value = getattr(version_info, "major", None)
    minor_value = getattr(version_info, "minor", None)
    if major_value is None or minor_value is None:
        major_value, minor_value = version_info[0], version_info[1]
    major = int(major_value)
    minor = int(minor_value)
    if (major, minor) != (3, 10):
        raise RuntimeError(
            "FrameByFrame-ASWAXS v6 requires Python 3.10. "
            f"Current interpreter is Python {major}.{minor}: {sys.executable}"
        )


def main() -> int:
    """Start the FrameByFrame-ASWAXS dashboard."""
    require_python_310()
    _quiet_server_glx_warning()

    from aswaxs_live.app.dashboard import DashboardWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("FrameByFrame-ASWAXS")
    app.setOrganizationName("ChemMatCARS")
    window = DashboardWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
