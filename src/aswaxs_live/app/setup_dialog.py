"""Setup dialog for configuring paths to external tools (XAnoS, XModFit)."""

from __future__ import annotations

from pathlib import Path

from aswaxs_live.app.qt import QtCore, QtWidgets

SETTINGS_GROUP = "external_tools"
KEY_XANOS = "xanos_components_path"
KEY_XMODFIT = "xmodfit_path"


def load_external_tool_paths() -> tuple[str, str]:
    """Return (xanos_path, xmodfit_path) from QSettings, empty strings if not set."""
    s = QtCore.QSettings()
    s.beginGroup(SETTINGS_GROUP)
    xanos = s.value(KEY_XANOS, "", type=str)
    xmodfit = s.value(KEY_XMODFIT, "", type=str)
    s.endGroup()
    return xanos, xmodfit


def save_external_tool_paths(xanos: str, xmodfit: str) -> None:
    s = QtCore.QSettings()
    s.beginGroup(SETTINGS_GROUP)
    s.setValue(KEY_XANOS, xanos)
    s.setValue(KEY_XMODFIT, xmodfit)
    s.endGroup()


class ExternalToolsSetupDialog(QtWidgets.QDialog):
    """Dialog for setting the file-system paths to XAnoS Components and XModFit."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Setup — External Tools")
        self.setMinimumWidth(560)
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Set the paths to the XAnoS Components and XModFit scripts.\n"
            "Leave a field blank to use the default search (looks for the tool "
            "in adjacent folders next to this installation)."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setSpacing(8)

        self._xanos_edit, xanos_row = self._path_row("XAnoS_Components.py")
        form.addRow("XAnoS Components:", xanos_row)

        self._xmodfit_edit, xmodfit_row = self._path_row("xmodfit.py")
        form.addRow("XModFit:", xmodfit_row)

        layout.addLayout(form)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _path_row(self, filename_hint: str) -> tuple[QtWidgets.QLineEdit, QtWidgets.QWidget]:
        container = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        edit = QtWidgets.QLineEdit()
        edit.setPlaceholderText(f"Path to {filename_hint}  (leave blank for auto-detect)")
        browse = QtWidgets.QPushButton("Browse…")
        browse.setFixedWidth(72)
        browse.clicked.connect(lambda: self._browse(edit, filename_hint))
        row.addWidget(edit, 1)
        row.addWidget(browse)
        return edit, container

    def _browse(self, edit: QtWidgets.QLineEdit, filename_hint: str) -> None:
        start = str(Path(edit.text()).parent) if edit.text() else ""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            f"Locate {filename_hint}",
            start,
            "Python scripts (*.py);;All files (*)",
        )
        if path:
            edit.setText(path)

    def _load(self) -> None:
        xanos, xmodfit = load_external_tool_paths()
        self._xanos_edit.setText(xanos)
        self._xmodfit_edit.setText(xmodfit)

    def _save_and_accept(self) -> None:
        save_external_tool_paths(
            self._xanos_edit.text().strip(),
            self._xmodfit_edit.text().strip(),
        )
        self.accept()
