"""Capture and validate every first-party FrameByFrame v6 GUI offscreen."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


os.environ["QT_API"] = "pyside6"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets  # noqa: E402
from aswaxs_live.app.dashboard import DashboardWindow  # noqa: E402
from aswaxs_live.tools.iq_viewer.qc_widget import FrameStabilityWidget  # noqa: E402
from aswaxs_live.tools.iq_viewer.viewer import (  # noqa: E402
    H5IqViewerDialog,
    H5StructureViewerDialog,
)
from aswaxs_live.tools.online_reducer.app import MainWindow as OnlineReducerWindow  # noqa: E402
from aswaxs_live.tools.pyfai_setup.gui import PreprocessingWindow  # noqa: E402
from aswaxs_live.tools.rack_builder.app import RackBuilderDialog  # noqa: E402


SIZES = ((1280, 800), (1920, 1080))


def _construct_views() -> list[tuple[str, QtWidgets.QWidget]]:
    dashboard = DashboardWindow()
    dashboard.tabs.setCurrentIndex(1)
    task_builder = DashboardWindow()
    task_builder.tabs.setCurrentIndex(0)
    frame_qc = FrameStabilityWidget(show_file_controls=True, compact=False)
    frame_qc.setWindowTitle("Frame QC")
    return [
        ("main_dashboard", dashboard),
        ("task_builder", task_builder),
        ("iq_viewer", H5IqViewerDialog()),
        ("metadata_viewer", H5StructureViewerDialog()),
        ("frame_qc", frame_qc),
        ("online_reducer", OnlineReducerWindow()),
        ("pyfai_setup", PreprocessingWindow()),
        ("rack_builder", RackBuilderDialog(group_count=13)),
    ]


def _validate_widget(name: str, widget: QtWidgets.QWidget, target: tuple[int, int]) -> list[str]:
    issues: list[str] = []
    if widget.minimumSizeHint().width() > target[0] or widget.minimumSizeHint().height() > target[1]:
        issues.append(
            f"{name}: minimumSizeHint {widget.minimumSizeHint().width()}x"
            f"{widget.minimumSizeHint().height()} exceeds {target[0]}x{target[1]}"
        )
    for button in widget.findChildren(QtWidgets.QAbstractButton):
        if not button.isVisible() or not button.text().strip():
            continue
        if (
            isinstance(button, QtWidgets.QToolButton)
            and button.toolButtonStyle() == QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly
        ):
            continue
        needed = button.sizeHint().width()
        if button.width() + 2 < needed:
            issues.append(
                f"{name}: clipped button '{button.text()}' ({button.width()}px < {needed}px)"
            )
    for tab_bar in widget.findChildren(QtWidgets.QTabBar):
        for index in range(tab_bar.count()):
            needed = tab_bar.fontMetrics().horizontalAdvance(tab_bar.tabText(index)) + 24
            if tab_bar.tabRect(index).width() + 2 < needed:
                issues.append(
                    f"{name}: clipped tab '{tab_bar.tabText(index)}' "
                    f"({tab_bar.tabRect(index).width()}px < {needed}px)"
                )
    for splitter in widget.findChildren(QtWidgets.QSplitter):
        visible_sizes = [
            size
            for index, size in enumerate(splitter.sizes())
            if splitter.widget(index).isVisible()
        ]
        if splitter.isVisible() and any(size <= 0 for size in visible_sizes):
            issues.append(f"{name}: splitter has a collapsed visible pane: {splitter.sizes()}")
    return issues


def _file_dialog_metrics(app: QtWidgets.QApplication) -> dict[str, int]:
    style = app.style()
    small = style.pixelMetric(QtWidgets.QStyle.PixelMetric.PM_SmallIconSize)
    list_view = style.pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ListViewIconSize)
    if not (16 <= small <= 64 and 16 <= list_view <= 64):
        raise RuntimeError(f"Unexpected file-dialog icon metrics: small={small}, list={list_view}")
    dialog = QtWidgets.QFileDialog()
    dialog.setOption(QtWidgets.QFileDialog.Option.DontUseNativeDialog, True)
    dialog.ensurePolished()
    dialog.close()
    return {"small_icon_px": small, "list_view_icon_px": list_view}


def capture(output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setApplicationName("FrameByFrame-ASWAXS v6 GUI smoke")
    if not QtGui.QFontDatabase.families():
        font_id = QtGui.QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\segoeui.ttf")
        families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QtGui.QFont(families[0], 9))
    screen = app.primaryScreen()
    report: dict[str, object] = {
        "qt_api": os.environ.get("QT_API"),
        "qt_version": QtCore.qVersion(),
        "platform": os.environ.get("QT_QPA_PLATFORM"),
        "device_pixel_ratio": screen.devicePixelRatio() if screen else None,
        "logical_dpi": screen.logicalDotsPerInch() if screen else None,
        "file_dialog_icons": _file_dialog_metrics(app),
        "captures": [],
        "issues": [],
    }
    for width, height in SIZES:
        for name, widget in _construct_views():
            widget.resize(width, height)
            widget.show()
            app.processEvents()
            path = output_dir / f"{name}_{width}x{height}.png"
            if not widget.grab().save(str(path), "PNG"):
                raise RuntimeError(f"Could not save screenshot: {path}")
            report["captures"].append(
                {
                    "name": name,
                    "target": [width, height],
                    "actual": [widget.width(), widget.height()],
                    "path": str(path),
                }
            )
            report["issues"].extend(_validate_widget(name, widget, (width, height)))
            widget.close()
            widget.deleteLater()
            app.processEvents()
    report_path = output_dir / "gui_smoke_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_DIR / "outputs" / "gui_smoke_qt6",
    )
    args = parser.parse_args()
    report = capture(args.output_dir.resolve())
    print(json.dumps(report, indent=2))
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
