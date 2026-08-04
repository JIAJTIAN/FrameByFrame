"""Visual capillary rack helper for FrameByFrame-ASWAXS task setup.

This dialog is intentionally isolated from the reducer and queue code.  It only
returns GUI form values: background groups and ASAXS sample/solvent pairs.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from aswaxs_live.app.qt_runtime import suppress_glx_warning

suppress_glx_warning()

from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets, Signal, Slot

from aswaxs_live.app.theme import apply_tool_theme, fit_window_to_available_screen


ROLE_SAMPLE = "Sample"
ROLE_SOLVENT = "Solvent"
ROLE_GC = "GC"
ROLE_AIR = "Air"
ROLE_EMPTY = "Empty"
ROLE_SKIP = "Skip"
ROLES = [ROLE_SAMPLE, ROLE_SOLVENT, ROLE_GC, ROLE_AIR, ROLE_EMPTY, ROLE_SKIP]

ROLE_COLORS = {
    ROLE_SAMPLE: QtGui.QColor("#5b8ff9"),
    ROLE_SOLVENT: QtGui.QColor("#5ad8a6"),
    ROLE_GC: QtGui.QColor("#f6bd16"),
    ROLE_AIR: QtGui.QColor("#e86452"),
    ROLE_EMPTY: QtGui.QColor("#6dc8ec"),
    ROLE_SKIP: QtGui.QColor("#d8dce3"),
}


@dataclass
class RackResult:
    gc_group: int | None
    air_group: int | None
    empty_group: int | None
    pairs: list[tuple[str, int, int]]


class RackCanvas(QtWidgets.QWidget):
    selectedChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(230)
        self.setMouseTracking(True)
        self.positions: list[dict[str, object]] = []
        self.selected_index = 0

    def set_positions(self, positions: list[dict[str, object]]) -> None:
        self.positions = positions
        self.selected_index = min(self.selected_index, max(0, len(self.positions) - 1))
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:  # noqa: N802 - Qt override name.
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        if not self.positions:
            return

        margin_x = 32
        rack_top = 92
        rack_height = 96
        painter.setPen(QtGui.QPen(QtGui.QColor("#111111"), 4))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(margin_x, rack_top, self.width() - 2 * margin_x, rack_height)

        slot_width = 18
        slot_height = 116
        y = rack_top - 42
        for index, position in enumerate(self.positions):
            center_x = self._slot_center_x(index)
            rect = QtCore.QRectF(center_x - slot_width / 2, y, slot_width, slot_height)
            role = str(position.get("role", ROLE_SKIP))
            color = ROLE_COLORS.get(role, ROLE_COLORS[ROLE_SKIP])
            painter.setBrush(QtGui.QBrush(color.lighter(145)))
            pen = QtGui.QPen(QtGui.QColor("#111111"), 3)
            if index == self.selected_index:
                pen = QtGui.QPen(QtGui.QColor("#000000"), 5)
            painter.setPen(pen)
            painter.drawRoundedRect(rect, 6, 6)

            painter.setPen(QtGui.QColor("#222222"))
            painter.setFont(QtGui.QFont("Arial", 8))
            painter.drawText(QtCore.QRectF(center_x - 26, y + slot_height + 5, 52, 18), QtCore.Qt.AlignmentFlag.AlignCenter, str(index + 1))
            name = str(position.get("name", "")).strip()
            label = name if name else role
            painter.drawText(QtCore.QRectF(center_x - 42, y - 26, 84, 22), QtCore.Qt.AlignmentFlag.AlignCenter, label[:12])

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 - Qt override name.
        if not self.positions:
            return
        distances = [(abs(event.x() - self._slot_center_x(index)), index) for index in range(len(self.positions))]
        distance, index = min(distances)
        if distance <= 28:
            self.selected_index = index
            self.selectedChanged.emit(index)
            self.update()

    def _slot_center_x(self, index: int) -> float:
        count = max(1, len(self.positions))
        left = 62
        right = max(left + 1, self.width() - 62)
        if count == 1:
            return (left + right) / 2
        return left + (right - left) * index / (count - 1)


class RackBuilderDialog(QtWidgets.QDialog):
    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        group_count: int = 13,
        gc_group: int | None = 1,
        air_group: int | None = 2,
        empty_group: int | None = 3,
        pairs: list[tuple[str, int, int]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("FrameByFrame-ASWAXS Rack Builder")
        self.positions: list[dict[str, object]] = []
        self._updating = False
        self._build_ui()
        apply_tool_theme(self)
        fit_window_to_available_screen(self, (1120, 720), minimum=(820, 560))
        self.set_group_count(group_count)
        self._seed_from_existing(gc_group, air_group, empty_group, pairs or [])
        self._select_row(0)

    def result_payload(self) -> RackResult:
        self._sync_pairs_from_table()
        gc = self._first_group_for_role(ROLE_GC)
        air = self._first_group_for_role(ROLE_AIR)
        empty = self._first_group_for_role(ROLE_EMPTY)
        pairs: list[tuple[str, int, int]] = []
        for index, position in enumerate(self.positions, start=1):
            if position.get("role") != ROLE_SAMPLE:
                continue
            name = str(position.get("name", "")).strip() or f"sample_{index}"
            solvent_group = int(position.get("solvent_group") or 0)
            if solvent_group <= 0:
                solvent_group = self._nearest_solvent(index)
            if solvent_group > 0:
                pairs.append((name, index, solvent_group))
        return RackResult(gc_group=gc, air_group=air, empty_group=empty, pairs=pairs)

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        menu_bar = QtWidgets.QMenuBar(self)
        root.setMenuBar(menu_bar)
        file_menu = self.file_menu = menu_bar.addMenu("File")
        file_menu.addAction("Rack Setup...", self.configure_rack)
        file_menu.addAction("Clear Assignments", self.clear_assignments)
        file_menu.addSeparator()
        file_menu.addAction("Close", self.reject, QtGui.QKeySequence.StandardKey.Close)

        self.rack_summary_label = QtWidgets.QLabel("Rack: 13 positions")
        self.rack_summary_label.setObjectName("ToolStatus")
        root.addWidget(self.rack_summary_label)

        legend = QtWidgets.QHBoxLayout()
        root.addLayout(legend)
        for role in ROLES:
            swatch = QtWidgets.QLabel()
            swatch.setFixedSize(16, 16)
            swatch.setStyleSheet(f"background: {ROLE_COLORS[role].name()}; border: 1px solid #333;")
            legend.addWidget(swatch)
            legend.addWidget(QtWidgets.QLabel(role))
        legend.addStretch(1)

        body = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root.addWidget(body, 1)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        self.canvas = RackCanvas()
        self.canvas.selectedChanged.connect(self._select_row)
        left_layout.addWidget(self.canvas)

        self.position_table = QtWidgets.QTableWidget(0, 4)
        self.position_table.setHorizontalHeaderLabels(["Group", "Role", "Name", "Solvent group"])
        self.position_table.horizontalHeader().setStretchLastSection(True)
        self.position_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.position_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.position_table.itemSelectionChanged.connect(self._table_selection_changed)
        self.position_table.itemChanged.connect(self._table_item_changed)
        left_layout.addWidget(self.position_table, 1)
        body.addWidget(left)

        editor = QtWidgets.QGroupBox("Selected Capillary")
        editor.setMinimumWidth(280)
        editor.setMaximumWidth(360)
        editor_layout = QtWidgets.QFormLayout(editor)
        self.selected_label = QtWidgets.QLabel("Group 1")
        self.role_combo = QtWidgets.QComboBox()
        self.role_combo.addItems(ROLES)
        self.role_combo.currentTextChanged.connect(self._apply_editor)
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.textChanged.connect(self._apply_editor)
        self.solvent_group_spin = QtWidgets.QSpinBox()
        self.solvent_group_spin.setRange(0, 999)
        self.solvent_group_spin.setFixedWidth(138)
        self.solvent_group_spin.setSpecialValueText("nearest solvent")
        self.solvent_group_spin.valueChanged.connect(self._apply_editor)
        editor_layout.addRow("Group", self.selected_label)
        editor_layout.addRow("Role", self.role_combo)
        editor_layout.addRow("Name", self.name_edit)
        editor_layout.addRow("Solvent group", self.solvent_group_spin)

        quick = QtWidgets.QGridLayout()
        for index, (label, role) in enumerate([
            ("Mark GC", ROLE_GC),
            ("Mark Air", ROLE_AIR),
            ("Mark Empty", ROLE_EMPTY),
            ("Mark Solvent", ROLE_SOLVENT),
            ("Mark Sample", ROLE_SAMPLE),
            ("Clear Position", ROLE_SKIP),
        ]):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=role: self._set_selected_role(value))
            quick.addWidget(button, index // 2, index % 2)
        editor_layout.addRow(quick)
        body.addWidget(editor)
        body.setStretchFactor(0, 4)
        body.setStretchFactor(1, 1)
        body.setSizes([820, 300])

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def set_group_count(self, count: int) -> None:
        current = self.positions
        self.positions = []
        for index in range(count):
            if index < len(current):
                self.positions.append(current[index])
            else:
                self.positions.append({"role": ROLE_SKIP, "name": "", "solvent_group": 0})
        if hasattr(self, "rack_summary_label"):
            self.rack_summary_label.setText(f"Rack: {count} position{'s' if count != 1 else ''}")
        self._refresh_table()
        self.canvas.set_positions(self.positions)

    def configure_rack(self) -> None:
        count, accepted = QtWidgets.QInputDialog.getInt(
            self,
            "Rack Setup",
            "Number of rack positions / measurement groups",
            value=max(1, len(self.positions)),
            min=1,
            max=96,
        )
        if accepted:
            self.set_group_count(count)

    def clear_assignments(self) -> None:
        if not self.positions:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Clear Rack Assignments",
            "Clear all sample, solvent, GC, air, and empty assignments?",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.positions = [{"role": ROLE_SKIP, "name": "", "solvent_group": 0} for _ in self.positions]
        self._refresh_table()
        self.canvas.set_positions(self.positions)
        self._select_row(0)

    def _seed_from_existing(
        self,
        gc_group: int | None,
        air_group: int | None,
        empty_group: int | None,
        pairs: list[tuple[str, int, int]],
    ) -> None:
        for group, role, name in [(gc_group, ROLE_GC, "GC"), (air_group, ROLE_AIR, "air"), (empty_group, ROLE_EMPTY, "empty")]:
            if group is not None and 1 <= group <= len(self.positions):
                self.positions[group - 1] = {"role": role, "name": name, "solvent_group": 0}
        for output_name, sample_group, solvent_group in pairs:
            if 1 <= solvent_group <= len(self.positions) and self.positions[solvent_group - 1].get("role") == ROLE_SKIP:
                self.positions[solvent_group - 1] = {"role": ROLE_SOLVENT, "name": "solvent", "solvent_group": 0}
            if 1 <= sample_group <= len(self.positions):
                self.positions[sample_group - 1] = {"role": ROLE_SAMPLE, "name": output_name, "solvent_group": solvent_group}
        self._refresh_table()
        self.canvas.set_positions(self.positions)

    def _refresh_table(self) -> None:
        self._updating = True
        try:
            self.position_table.setRowCount(len(self.positions))
            for row, position in enumerate(self.positions):
                group_item = QtWidgets.QTableWidgetItem(str(row + 1))
                group_item.setFlags(group_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.position_table.setItem(row, 0, group_item)
                role_item = QtWidgets.QTableWidgetItem(str(position.get("role", ROLE_SKIP)))
                self.position_table.setItem(row, 1, role_item)
                self.position_table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(position.get("name", ""))))
                solvent = int(position.get("solvent_group") or 0)
                self.position_table.setItem(row, 3, QtWidgets.QTableWidgetItem("" if solvent <= 0 else str(solvent)))
        finally:
            self._updating = False

    def _table_item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if self._updating:
            return
        row = item.row()
        if not (0 <= row < len(self.positions)):
            return
        role = self.position_table.item(row, 1).text().strip() if self.position_table.item(row, 1) else ROLE_SKIP
        if role not in ROLES:
            role = ROLE_SKIP
        name = self.position_table.item(row, 2).text().strip() if self.position_table.item(row, 2) else ""
        solvent_text = self.position_table.item(row, 3).text().strip() if self.position_table.item(row, 3) else ""
        self.positions[row] = {"role": role, "name": name, "solvent_group": int(solvent_text) if solvent_text.isdigit() else 0}
        self.canvas.set_positions(self.positions)
        self._select_row(row)

    def _table_selection_changed(self) -> None:
        rows = self.position_table.selectionModel().selectedRows() if self.position_table.selectionModel() else []
        if rows:
            self._select_row(rows[0].row())

    def _select_row(self, row: int) -> None:
        if not (0 <= row < len(self.positions)):
            return
        self._updating = True
        try:
            self.canvas.selected_index = row
            self.canvas.update()
            if self.position_table.currentRow() != row:
                self.position_table.selectRow(row)
            position = self.positions[row]
            self.selected_label.setText(f"Group {row + 1}")
            self.role_combo.setCurrentText(str(position.get("role", ROLE_SKIP)))
            self.name_edit.setText(str(position.get("name", "")))
            self.solvent_group_spin.setValue(int(position.get("solvent_group") or 0))
        finally:
            self._updating = False

    def _apply_editor(self, *_args: object) -> None:
        if self._updating:
            return
        row = self.canvas.selected_index
        if not (0 <= row < len(self.positions)):
            return
        self.positions[row] = {
            "role": self.role_combo.currentText(),
            "name": self.name_edit.text().strip(),
            "solvent_group": self.solvent_group_spin.value(),
        }
        self._refresh_table()
        self._select_row(row)
        self.canvas.set_positions(self.positions)

    def _set_selected_role(self, role: str) -> None:
        self.role_combo.setCurrentText(role)
        if role == ROLE_SAMPLE and not self.name_edit.text().strip():
            self.name_edit.setText(f"sample_{self.canvas.selected_index + 1}")
        elif role == ROLE_SOLVENT and not self.name_edit.text().strip():
            self.name_edit.setText("solvent")
        elif role == ROLE_SKIP:
            self.name_edit.clear()
            self.solvent_group_spin.setValue(0)

    def _sync_pairs_from_table(self) -> None:
        for row in range(self.position_table.rowCount()):
            item = self.position_table.item(row, 1)
            if item is not None:
                self._table_item_changed(item)

    def _first_group_for_role(self, role: str) -> int | None:
        for index, position in enumerate(self.positions, start=1):
            if position.get("role") == role:
                return index
        return None

    def _nearest_solvent(self, group: int) -> int:
        solvents = [index for index, position in enumerate(self.positions, start=1) if position.get("role") == ROLE_SOLVENT]
        if not solvents:
            return 0
        return min(solvents, key=lambda value: abs(value - group))


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    dialog = RackBuilderDialog()
    dialog.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
