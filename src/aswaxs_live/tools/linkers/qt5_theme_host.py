"""Shared visual styling for the v5 scientific tool windows."""

from __future__ import annotations

from PyQt5 import QtCore, QtGui, QtWidgets


PROFESSIONAL_TOOL_STYLESHEET = """
QDialog, QMainWindow {
    background: #f3f4f6;
}
QWidget {
    color: #20242a;
    font-size: 9pt;
}
QGroupBox {
    background: #ffffff;
    border: 1px solid #c8ccd2;
    border-radius: 3px;
    color: #28313f;
    font-weight: 600;
    margin-top: 14px;
    padding-top: 10px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #ffffff;
    border: 1px solid #b9c0ca;
    border-radius: 2px;
    padding: 4px 6px;
    selection-background-color: #2f6fae;
    selection-color: #ffffff;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: #2f6fae;
}
QPushButton {
    background: #f8f9fb;
    border: 1px solid #b9c0ca;
    border-radius: 3px;
    color: #20242a;
    min-height: 22px;
    padding: 4px 10px;
}
QPushButton:hover {
    background: #edf4fd;
    border-color: #6f9ac8;
}
QPushButton:pressed {
    background: #d9e8f7;
}
QPushButton#PrimaryActionButton {
    background: #2f6fae;
    border-color: #245c91;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#PrimaryActionButton:hover {
    background: #245f99;
    border-color: #194f83;
}
QPushButton#PrimaryActionButton:pressed {
    background: #1d4f80;
}
QPushButton:default {
    background: #2f6fae;
    border-color: #245c91;
    color: #ffffff;
}
QPushButton:disabled {
    background: #e5e7eb;
    border-color: #cdd2da;
    color: #8a9099;
}
QTableWidget, QTableView, QListWidget, QTreeWidget {
    background: #ffffff;
    alternate-background-color: #f7f9fb;
    border: 1px solid #c8ccd2;
    gridline-color: #d9dde3;
    selection-background-color: #2f6fae;
    selection-color: #ffffff;
}
QHeaderView::section {
    background: #e6e9ef;
    border: 0;
    border-bottom: 1px solid #c8ccd2;
    border-right: 1px solid #c8ccd2;
    color: #20242a;
    font-weight: 600;
    padding: 5px 6px;
}
QTabWidget::pane {
    background: #ffffff;
    border: 1px solid #c8ccd2;
}
QTabBar::tab {
    background: #e6e9ef;
    border: 1px solid #c8ccd2;
    border-bottom: 0;
    min-width: 86px;
    padding: 5px 14px;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1f4f7f;
    font-weight: 600;
}
QScrollArea {
    background: #ffffff;
    border: 1px solid #c8ccd2;
}
QToolBar {
    background: #eceff3;
    border: 0;
    border-bottom: 1px solid #c8ccd2;
    spacing: 3px;
    padding: 3px;
}
QToolBar QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 2px;
    min-height: 28px;
    min-width: 28px;
    padding: 2px;
    qproperty-iconSize: 20px 20px;
}
QToolBar QToolButton:hover {
    background: #edf4fd;
    border-color: #6f9ac8;
}
QFileDialog QToolButton {
    min-height: 26px;
    min-width: 26px;
    padding: 2px;
    qproperty-iconSize: 20px 20px;
}
QSplitter::handle {
    background: #d9dde3;
}
QSplitter::handle:horizontal {
    width: 5px;
}
QSplitter::handle:vertical {
    height: 5px;
}
QLabel#ToolStatus {
    background: #eef3f8;
    border: 1px solid #c8d4e1;
    border-radius: 2px;
    color: #34465a;
    padding: 5px 7px;
}
QDialogButtonBox QPushButton {
    min-width: 86px;
}
QToolTip {
    background: #fffff2;
    border: 1px solid #8a9099;
    color: #20242a;
    padding: 3px;
}
"""


def apply_tool_theme(widget: QtWidgets.QWidget) -> None:
    """Apply the shared professional style to one independent tool window."""
    widget.setStyleSheet(PROFESSIONAL_TOOL_STYLESHEET)


def fit_window_to_available_screen(
    widget: QtWidgets.QWidget,
    preferred: QtCore.QSize | tuple[int, int],
    *,
    minimum: QtCore.QSize | tuple[int, int] | None = None,
    margin: int = 72,
) -> None:
    """Resize and center a window without exceeding the current screen."""
    preferred_size = _to_size(preferred)
    requested_minimum = _to_size(minimum) if minimum is not None else widget.minimumSize()
    screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or QtWidgets.QApplication.primaryScreen()
    if screen is None:
        widget.resize(preferred_size)
        return

    available = screen.availableGeometry()
    max_width = max(320, available.width() - margin)
    max_height = max(260, available.height() - margin)
    minimum_width = min(max_width, max(320, requested_minimum.width()))
    minimum_height = min(max_height, max(240, requested_minimum.height()))
    width = min(max_width, max(minimum_width, preferred_size.width()))
    height = min(max_height, max(minimum_height, preferred_size.height()))

    widget.setMinimumSize(minimum_width, minimum_height)
    widget.resize(width, height)
    widget.move(
        available.x() + max(0, (available.width() - width) // 2),
        available.y() + max(0, (available.height() - height) // 2),
    )


def _to_size(value: QtCore.QSize | tuple[int, int]) -> QtCore.QSize:
    if isinstance(value, QtCore.QSize):
        return value
    return QtCore.QSize(int(value[0]), int(value[1]))
