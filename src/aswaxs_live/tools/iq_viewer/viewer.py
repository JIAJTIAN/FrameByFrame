"""Small read-only HDF5 inspection tools for the v6 dashboard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np
from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets, Signal, Slot
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from aswaxs_live.reduction.xanos_export import write_xanos_curve_file, xanos_curve_header_info
from aswaxs_live.tools.iq_viewer.qc_widget import FrameStabilityWidget
from aswaxs_live.app.theme import apply_tool_theme, fit_window_to_available_screen


H5_FILTER = "HDF5 files (*.h5 *.hdf5);;All files (*)"
IQ_VIEWER_INITIAL_SIZE = QtCore.QSize(1240, 900)
IQ_VIEWER_MINIMUM_SIZE = QtCore.QSize(860, 620)
IQ_VIEWER_LEFT_WIDTH = 390
IQ_VIEWER_PLOT_WIDTH = 850
IQ_X_LABEL = r"$q$ ($\mathrm{\AA}^{-1}$)"
IQ_Y_LABEL = r"$I(q)$ (a.u.)"
PUBLICATION_COLORS = [
    "#0072B2",  # blue
    "#D55E00",  # vermillion
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#E69F00",  # orange
    "#56B4E9",  # sky blue
    "#F0E442",  # yellow
    "#000000",  # black
]
SOURCE_FILTERS = [
    ("All data", "all"),
    ("SAXS / Pil300K", "saxs"),
    ("WAXS / Eig1M", "waxs"),
    ("Combined / stitched", "combined"),
]
MAX_CURVE_ROWS_PER_GROUP = 250
MAX_TREE_ITEMS = 6000
MAX_ERROR_BAR_POINTS = 450
MIN_Q_GRID_POINTS = 8
Q_DATASET_NAMES = {"q", "Q", "q_A^-1", "q_invA", "q_nm^-1"}
METADATA_GROUP_NAMES = {
    "metadata",
    "parameters",
    "normalization_factors",
    "subtraction_map",
    "detector_summary",
    "monitor_summary",
    "sample_summary",
    "scan_summary",
    "data_reference_summary",
}


@dataclass(frozen=True)
class H5CurveRecord:
    label: str
    h5_path: str | None
    group_path: str
    q_path: str
    i_path: str
    y_name: str
    sigma_path: str | None
    row: int | None


@dataclass(frozen=True)
class BackgroundCurve:
    label: str
    h5_path: str
    record: H5CurveRecord
    q: np.ndarray
    intensity: np.ndarray
    sigma: np.ndarray | None


class H5CurveLoadWorker(QtCore.QThread):
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(self, path: Path, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.path = path

    def run(self) -> None:
        try:
            if not self.path.exists():
                raise FileNotFoundError(f"HDF5 file/folder does not exist: {self.path}")
            h5_paths = _h5_paths_from_file_or_folder(self.path)
            curves: list[H5CurveRecord] = []
            for h5_path in h5_paths:
                with h5py.File(h5_path, "r") as handle:
                    source_label = _source_label(self.path, h5_path) if len(h5_paths) > 1 else ""
                    curves.extend(discover_iq_curves(handle, source_path=h5_path, source_label=source_label))
        except Exception as exc:  # noqa: BLE001 - pass filesystem/HDF5 failures back to the GUI.
            self.failed.emit(str(exc))
            return
        self.completed.emit(curves, h5_paths)


class H5IqViewerDialog(QtWidgets.QDialog):
    """Read-only HDF5 q-I curve viewer."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FrameByFrame-ASWAXS HDF5 I-q Plot Viewer")
        self.curves: list[H5CurveRecord] = []
        self.background_curve: BackgroundCurve | None = None
        self.background_record_key: tuple[str, str, int | None] | None = None
        self._background_pick_mode = False
        self._saved_sample_selection: list[int] = []
        self._plotted_points: list[tuple[np.ndarray, np.ndarray, str]] = []
        self._frame_stability_source_dirty = True
        self.h5_load_worker: H5CurveLoadWorker | None = None
        self._build_ui()
        apply_tool_theme(self)
        fit_window_to_available_screen(self, IQ_VIEWER_INITIAL_SIZE, minimum=IQ_VIEWER_MINIMUM_SIZE)

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.path_edit = QtWidgets.QLineEdit(self)
        self.path_edit.hide()

        menu_bar = QtWidgets.QMenuBar(self)
        root.setMenuBar(menu_bar)
        file_menu = self.file_menu = menu_bar.addMenu("File")
        file_menu.addAction("Open HDF5...", self.browse_file, QtGui.QKeySequence.StandardKey.Open)
        self.load_button = QtGui.QAction("Reload", self)
        self.load_button.setShortcut(QtGui.QKeySequence.StandardKey.Refresh)
        self.load_button.triggered.connect(self.load_file)
        file_menu.addAction(self.load_button)
        file_menu.addSeparator()
        file_menu.addAction("Export Selected XAnoS .dat...", self.export_selected_curves)
        file_menu.addAction("Export Pair Outputs...", self.export_pair_outputs)
        file_menu.addSeparator()
        file_menu.addAction("Close", self.close, QtGui.QKeySequence.StandardKey.Close)

        view_menu = self.view_menu = menu_bar.addMenu("View")
        self.curve_controls_action = QtGui.QAction("Curve Controls", self)
        self.curve_controls_action.setCheckable(True)
        self.curve_controls_action.setChecked(True)
        self.curve_controls_action.toggled.connect(self._set_curve_controls_visible)
        view_menu.addAction(self.curve_controls_action)

        self.viewer_tabs = QtWidgets.QTabWidget()
        curves_page = QtWidgets.QWidget()
        curves_root = QtWidgets.QVBoxLayout(curves_page)
        curves_root.setContentsMargins(0, 0, 0, 0)
        self.curves_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        curves_root.addWidget(self.curves_splitter, 1)
        self.viewer_tabs.addTab(curves_page, "I-q Curves")
        self.frame_stability_widget = FrameStabilityWidget(self, show_file_controls=False, compact=True)
        self.viewer_tabs.addTab(self.frame_stability_widget, "Frame Stability QC")
        self.viewer_tabs.currentChanged.connect(self._viewer_tab_changed)
        root.addWidget(self.viewer_tabs, 1)

        left = QtWidgets.QWidget()
        self.curve_controls_panel = left
        left.setMinimumWidth(300)
        left.setMaximumWidth(500)
        left_layout = QtWidgets.QVBoxLayout(left)

        source_row = QtWidgets.QHBoxLayout()
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Filter curves, e.g. final, stitched, normalized, group_05")
        self.filter_edit.textChanged.connect(self._refill_curve_list)
        self.source_combo = QtWidgets.QComboBox()
        for label, value in SOURCE_FILTERS:
            self.source_combo.addItem(label, value)
        self.source_combo.currentIndexChanged.connect(self._refill_curve_list)
        source_row.addWidget(QtWidgets.QLabel("Source"))
        source_row.addWidget(self.source_combo, 1)
        left_layout.addLayout(source_row)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.addWidget(QtWidgets.QLabel("Filter"))
        filter_row.addWidget(self.filter_edit, 1)
        left_layout.addLayout(filter_row)

        display_row = QtWidgets.QHBoxLayout()
        self.log_q_check = QtWidgets.QCheckBox("log q")
        self.log_q_check.setChecked(True)
        self.log_q_check.stateChanged.connect(self.plot_selected)
        self.log_i_check = QtWidgets.QCheckBox("log I")
        self.log_i_check.setChecked(True)
        self.log_i_check.stateChanged.connect(self.plot_selected)
        self.error_check = QtWidgets.QCheckBox("errors")
        self.error_check.setChecked(True)
        self.error_check.stateChanged.connect(self.plot_selected)
        self.legend_check = QtWidgets.QCheckBox("legend")
        self.legend_check.setChecked(True)
        self.legend_check.stateChanged.connect(self._update_plot_legend)
        self.max_curves_spin = QtWidgets.QSpinBox()
        self.max_curves_spin.setRange(1, 200)
        self.max_curves_spin.setValue(20)
        self.max_curves_spin.valueChanged.connect(self.plot_selected)
        display_row.addWidget(self.log_q_check)
        display_row.addWidget(self.log_i_check)
        display_row.addWidget(self.error_check)
        display_row.addWidget(self.legend_check)
        display_row.addStretch(1)
        display_row.addWidget(QtWidgets.QLabel("Max"))
        display_row.addWidget(self.max_curves_spin)
        left_layout.addLayout(display_row)

        action_row = QtWidgets.QHBoxLayout()
        plot_button = QtWidgets.QPushButton("Plot Selected")
        plot_button.setObjectName("PrimaryActionButton")
        plot_button.setMinimumWidth(112)
        plot_button.clicked.connect(self.plot_selected)
        export_button = QtWidgets.QPushButton("Export XAnoS .dat")
        export_button.setMinimumWidth(142)
        export_button.clicked.connect(self.export_selected_curves)
        action_row.addWidget(plot_button)
        action_row.addStretch(1)
        action_row.addWidget(export_button)
        left_layout.addLayout(action_row)

        background_controls = QtWidgets.QGridLayout()
        background_controls.setHorizontalSpacing(8)
        background_controls.setVerticalSpacing(4)
        self.select_background_button = QtWidgets.QPushButton("Select Background")
        self.select_background_button.setMinimumWidth(138)
        self.select_background_button.clicked.connect(self.select_background)
        clear_background_button = QtWidgets.QPushButton("Clear Background")
        clear_background_button.setMinimumWidth(132)
        clear_background_button.clicked.connect(self.clear_background)
        self.background_factor_spin = QtWidgets.QDoubleSpinBox()
        self.background_factor_spin.setRange(-1000.0, 1000.0)
        self.background_factor_spin.setDecimals(5)
        self.background_factor_spin.setSingleStep(0.01)
        self.background_factor_spin.setValue(0.99)
        self.background_factor_spin.valueChanged.connect(self.plot_selected)
        self.background_label = QtWidgets.QLabel("No background selected.")
        self.background_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.background_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.background_label.setMinimumHeight(self.background_label.sizeHint().height())
        background_controls.addWidget(self.select_background_button, 0, 0)
        background_controls.addWidget(clear_background_button, 0, 1)
        background_controls.addWidget(QtWidgets.QLabel("Factor"), 1, 0)
        background_controls.addWidget(self.background_factor_spin, 1, 1)
        background_controls.setColumnStretch(2, 1)
        left_layout.addLayout(background_controls)

        background_label_row = QtWidgets.QHBoxLayout()
        background_label_row.addWidget(self.background_label, 1)
        left_layout.addLayout(background_label_row)

        self.curve_list = QtWidgets.QListWidget()
        self.curve_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.curve_list.itemClicked.connect(self._maybe_set_clicked_background)
        self.curve_list.itemDoubleClicked.connect(lambda _item: self.plot_selected())
        left_layout.addWidget(self.curve_list, 2)

        pair_label = QtWidgets.QLabel("Sample - background pairs")
        pair_label.setStyleSheet("font-weight: bold;")
        left_layout.addWidget(pair_label)
        self.pair_table = QtWidgets.QTableWidget(0, 4)
        self.pair_table.setHorizontalHeaderLabels(["Output name", "Sample", "Background", "Factor"])
        self.pair_table.horizontalHeader().setStretchLastSection(False)
        self.pair_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.pair_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.pair_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.pair_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.pair_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.pair_table.setMaximumHeight(150)
        left_layout.addWidget(self.pair_table)

        pair_row_1 = QtWidgets.QHBoxLayout()
        add_pair = QtWidgets.QPushButton("Add Pair")
        add_pair.setToolTip("With a marked background, add every selected sample as a separate subtraction pair.")
        add_pair.clicked.connect(self.add_pair_from_selection)
        remove_pair = QtWidgets.QPushButton("Remove Pair")
        remove_pair.clicked.connect(self.remove_selected_pairs)
        clear_pairs = QtWidgets.QPushButton("Clear Pairs")
        clear_pairs.clicked.connect(self.clear_pair_rows)
        pair_row_1.addWidget(add_pair)
        pair_row_1.addWidget(remove_pair)
        pair_row_1.addWidget(clear_pairs)
        pair_row_1.addStretch(1)
        left_layout.addLayout(pair_row_1)

        pair_row_2 = QtWidgets.QHBoxLayout()
        plot_pairs = QtWidgets.QPushButton("Plot Pair Outputs")
        plot_pairs.setObjectName("PrimaryActionButton")
        plot_pairs.setMinimumWidth(130)
        plot_pairs.clicked.connect(self.plot_pair_outputs)
        export_pairs = QtWidgets.QPushButton("Export Pair Outputs")
        export_pairs.setMinimumWidth(140)
        export_pairs.clicked.connect(self.export_pair_outputs)
        pair_row_2.addWidget(plot_pairs)
        pair_row_2.addWidget(export_pairs)
        pair_row_2.addStretch(1)
        left_layout.addLayout(pair_row_2)

        self.status_label = QtWidgets.QLabel("Choose an analysis HDF5 file.")
        self.status_label.setObjectName("ToolStatus")
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.status_label.setMinimumHeight(self.status_label.sizeHint().height())
        left_layout.addWidget(self.status_label)
        self.curves_splitter.addWidget(left)

        plot_panel = QtWidgets.QWidget()
        plot_panel.setMinimumSize(380, 380)
        plot_panel.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        plot_layout = QtWidgets.QVBoxLayout(plot_panel)
        self.figure = Figure(figsize=(7.2, 7.2), dpi=110)
        self.figure.subplots_adjust(left=0.13, right=0.98, bottom=0.12, top=0.97)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setMinimumSize(380, 380)
        self.canvas.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.toolbar = NavigationToolbar(self.canvas, self)
        self.ax = self.figure.add_subplot(111)
        _apply_iq_axes_style(self.ax)
        self.coordinate_label = QtWidgets.QLabel("q: -, I: -")
        self.coordinate_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.coordinate_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.coordinate_label.setMinimumHeight(self.coordinate_label.sizeHint().height())
        self.coordinate_label.setMaximumHeight(self.coordinate_label.sizeHint().height() + 4)
        plot_layout.addWidget(self.toolbar)
        plot_layout.addWidget(self.canvas, 1)
        plot_layout.addWidget(self.coordinate_label)
        self.canvas.mpl_connect("motion_notify_event", self._on_plot_motion)
        self.curves_splitter.addWidget(plot_panel)
        self.curves_splitter.setChildrenCollapsible(True)
        self.curves_splitter.setSizes([IQ_VIEWER_LEFT_WIDTH, IQ_VIEWER_PLOT_WIDTH])
        self.curves_splitter.setStretchFactor(0, 0)
        self.curves_splitter.setStretchFactor(1, 1)

    def _set_curve_controls_visible(self, visible: bool) -> None:
        if not hasattr(self, "curve_controls_panel"):
            return
        self.curve_controls_panel.setVisible(visible)
        if visible:
            self.curves_splitter.setSizes([IQ_VIEWER_LEFT_WIDTH, max(600, self.curves_splitter.width() - IQ_VIEWER_LEFT_WIDTH)])

    def open_file(self, path: Path | str) -> None:
        self.path_edit.setText(str(path))
        self.load_file()
        self.show()
        self.raise_()
        self.activateWindow()

    def browse_file(self) -> None:
        start = Path(self.path_edit.text()).parent if self.path_edit.text().strip() else Path.home()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open HDF5 for I-q plotting", str(start), H5_FILTER)
        if path:
            self.path_edit.setText(path)
            self.load_file()

    def load_file(self) -> None:
        path = Path(self.path_edit.text().strip())
        if self.h5_load_worker is not None:
            self.status_label.setText("An HDF5 file is already being read. The viewer remains responsive while it finishes.")
            return
        self.curve_list.clear()
        self.curves = []
        self.status_label.setText(f"Reading HDF5 metadata from {path}...")
        self.load_button.setEnabled(False)
        self.h5_load_worker = H5CurveLoadWorker(path, self)
        self.h5_load_worker.completed.connect(lambda curves, paths, source=path: self._h5_load_complete(source, curves, paths))
        self.h5_load_worker.failed.connect(self._h5_load_failed)
        self.h5_load_worker.finished.connect(self._h5_load_finished)
        self.h5_load_worker.start()

    def _h5_load_complete(self, path: Path, curves: list[H5CurveRecord], h5_paths: list[Path]) -> None:
        self.curves = curves
        self._refill_curve_list(select_first=True)
        self._frame_stability_source_dirty = True
        if self.viewer_tabs.currentIndex() == 1:
            self._load_frame_stability_source()
        source_text = f" in {len(h5_paths)} HDF5 file(s)" if path.is_dir() else ""
        self.status_label.setText(f"Found {len(self.curves)} plottable q-data rows{source_text}.")
        self.plot_selected()

    def _h5_load_failed(self, message: str) -> None:
        self.status_label.setText(f"Could not read HDF5: {message}")

    def _h5_load_finished(self) -> None:
        if self.h5_load_worker is not None:
            self.h5_load_worker.deleteLater()
        self.h5_load_worker = None
        self.load_button.setEnabled(True)

    def _viewer_tab_changed(self, index: int) -> None:
        self.curve_controls_action.setEnabled(index == 0)
        if index == 1 and self._frame_stability_source_dirty:
            self._load_frame_stability_source()

    def _load_frame_stability_source(self) -> None:
        path_text = self.path_edit.text().strip()
        if not path_text:
            return
        self.frame_stability_widget.open_file(Path(path_text))
        self._frame_stability_source_dirty = False

    def _refill_curve_list(self, select_first: bool = False) -> None:
        if not isinstance(select_first, bool):
            select_first = False
        needle = self.filter_edit.text().strip().lower()
        source = self.source_combo.currentData() or "all"
        self.curve_list.clear()
        for index, curve in enumerate(self.curves):
            if not _curve_matches_source(curve, source):
                continue
            if needle and needle not in curve.label.lower() and needle not in curve.group_path.lower():
                continue
            item = QtWidgets.QListWidgetItem(curve.label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, index)
            self._style_curve_list_item(item, curve)
            self.curve_list.addItem(item)
        if select_first:
            if self.curve_list.count():
                self.curve_list.item(0).setSelected(True)

    def select_background(self) -> None:
        self._saved_sample_selection = self._selected_curve_indices()
        self._background_pick_mode = True
        self.select_background_button.setText("Click background curve...")
        self.status_label.setText("Click one curve in the left list to mark it as the background.")

    def clear_background(self) -> None:
        self.background_curve = None
        self.background_record_key = None
        self._background_pick_mode = False
        self.select_background_button.setText("Select Background")
        self.background_label.setText("No background selected.")
        self._refresh_curve_list_marks()
        self.plot_selected()

    def _maybe_set_clicked_background(self, item: QtWidgets.QListWidgetItem) -> None:
        if not self._background_pick_mode:
            return
        value = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if value is None:
            return
        self._set_background_from_record_index(int(value))
        self._restore_sample_selection()
        self._background_pick_mode = False
        self.select_background_button.setText("Select Background")
        self.plot_selected()

    def _set_background_from_record_index(self, record_index: int) -> None:
        path = Path(self.path_edit.text().strip())
        record = self.curves[record_index]
        h5_path = Path(record.h5_path) if record.h5_path else path
        try:
            q, intensity, sigma = _read_curve_from_h5_path(h5_path, record)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Could not set background curve: {exc}")
            return
        self.background_record_key = _record_key(record)
        self.background_curve = BackgroundCurve(
            label=f"{h5_path.name} | {record.label}",
            h5_path=str(h5_path),
            record=record,
            q=q,
            intensity=intensity,
            sigma=sigma,
        )
        self.background_label.setText(f"Background: {self.background_curve.label}")
        self._refresh_curve_list_marks()

    def _restore_sample_selection(self) -> None:
        self.curve_list.blockSignals(True)
        self.curve_list.clearSelection()
        saved = set(self._saved_sample_selection)
        for row in range(self.curve_list.count()):
            item = self.curve_list.item(row)
            record_index = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if record_index in saved and not self._is_background_record(self.curves[int(record_index)]):
                item.setSelected(True)
        self.curve_list.blockSignals(False)

    def _refresh_curve_list_marks(self) -> None:
        for row in range(self.curve_list.count()):
            item = self.curve_list.item(row)
            record_index = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if record_index is None:
                continue
            self._style_curve_list_item(item, self.curves[int(record_index)])

    def _is_background_record(self, curve: H5CurveRecord) -> bool:
        return self.background_record_key is not None and _record_key(curve) == self.background_record_key

    def _style_curve_list_item(self, item: QtWidgets.QListWidgetItem, curve: H5CurveRecord) -> None:
        label = curve.label
        item.setBackground(QtGui.QBrush())
        item.setToolTip(curve.label)
        if self._is_background_record(curve):
            label = f"[BG] {curve.label}"
            item.setBackground(QtGui.QColor("#fff2a8"))
            item.setToolTip(f"Background curve\n{curve.label}")
        item.setText(label)

    def plot_selected(self) -> None:
        self.ax.clear()
        self._plotted_points = []
        self.coordinate_label.setText("q: -, I: -")
        path = Path(self.path_edit.text().strip())
        selected = self._selected_curve_indices()
        selected = selected[: self.max_curves_spin.value()]
        if not selected:
            self.status_label.setText("Select one or more curves to plot.")
            _apply_iq_axes_style(self.ax)
            self.canvas.draw_idle()
            return
        background = self._background_curve_data()
        plotted = 0
        try:
            for record_index in selected:
                q, intensity, sigma, label, _record = self._prepared_curve_data(record_index, path, background)
                mask = _plot_mask(q, intensity, self.log_q_check.isChecked(), self.log_i_check.isChecked())
                if np.count_nonzero(mask) < 2:
                    continue
                color = _publication_color(plotted, max(1, len(selected)))
                self.ax.plot(q[mask], intensity[mask], linewidth=1.4, color=color, label=label)
                self._plotted_points.append((q[mask], intensity[mask], label))
                if self.error_check.isChecked() and sigma is not None:
                    self._plot_error_bars(q, intensity, sigma, mask, color)
                plotted += 1
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Could not plot selected curves: {exc}")
            return
        self.ax.set_xscale("log" if self.log_q_check.isChecked() else "linear")
        self.ax.set_yscale("log" if self.log_i_check.isChecked() else "linear")
        _apply_iq_axes_style(self.ax)
        if plotted:
            self._update_plot_legend()
        self.canvas.draw_idle()
        suffix = " with background subtraction" if background is not None else ""
        self.status_label.setText(f"Plotted {plotted}/{len(selected)} selected curves{suffix}.")

    def export_selected_curves(self) -> None:
        path = Path(self.path_edit.text().strip())
        selected = self._selected_curve_indices()
        if not selected:
            self.status_label.setText("Select one or more curves to export.")
            return
        background = self._background_curve_data()
        if len(selected) == 1:
            default_name = _safe_filename(self.curves[selected[0]].label) + ".dat"
            target, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export selected I-q data in XAnoS format", str(path.parent / default_name), "XAnoS data files (*.dat)"
            )
            if not target:
                return
            targets = [_xanos_dat_path(Path(target))]
        else:
            folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Export selected XAnoS .dat files to folder", str(path.parent))
            if not folder:
                return
            targets = [Path(folder) / (_safe_filename(self.curves[index].label) + ".dat") for index in selected]
        try:
            for record_index, target in zip(selected, targets, strict=True):
                q, intensity, sigma, label, record = self._prepared_curve_data(record_index, path, background)
                _write_curve_export(
                    target,
                    q,
                    intensity,
                    sigma,
                    label=label,
                    record=record,
                    background_label=background[3] if background is not None else None,
                    background_factor=self.background_factor_spin.value() if background is not None else None,
                )
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Could not export selected curves: {exc}")
            return
        self.status_label.setText(f"Exported {len(targets)} selected curve(s).")

    def add_pair_from_selection(self) -> None:
        selected = self._selected_curve_indices()
        if self.background_record_key is not None:
            background_index = self._curve_index_for_key(self.background_record_key)
            if background_index is None:
                self.status_label.setText("Marked background is not visible in the current list.")
                return
            sample_candidates = _fixed_background_sample_indices(selected, background_index)
            if not sample_candidates:
                self.status_label.setText("Select at least one sample curve that is not the marked background.")
                return
            added = 0
            for sample_index in sample_candidates:
                if self._pair_row_exists(sample_index, background_index):
                    continue
                self._append_pair_row(sample_index, background_index)
                added += 1
            skipped = len(sample_candidates) - added
            if added and skipped:
                self.status_label.setText(f"Added {added} sample-background pair(s); skipped {skipped} duplicate(s).")
            elif added:
                self.status_label.setText(f"Added {added} sample-background pair(s) using the marked background.")
            else:
                self.status_label.setText("All selected sample-background pairs are already in the table.")
            return
        elif len(selected) >= 2:
            current_item = self.curve_list.currentItem()
            current_index = current_item.data(QtCore.Qt.ItemDataRole.UserRole) if current_item is not None else None
            sample_index = int(current_index) if current_index in selected else selected[0]
            background_candidates = [index for index in selected if index != sample_index]
            background_index = background_candidates[0]
        else:
            self.status_label.setText("Select sample and background curves, or select a sample and mark one background.")
            return
        if sample_index == background_index:
            self.status_label.setText("Sample and background curve must be different.")
            return
        if self._pair_row_exists(sample_index, background_index):
            self.status_label.setText("This sample-background pair is already in the table.")
            return
        self._append_pair_row(sample_index, background_index)
        self.status_label.setText("Added one sample-background pair.")

    def remove_selected_pairs(self) -> None:
        rows = sorted({index.row() for index in self.pair_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.pair_table.removeRow(row)

    def clear_pair_rows(self) -> None:
        self.pair_table.setRowCount(0)

    def plot_pair_outputs(self) -> None:
        self.ax.clear()
        self._plotted_points = []
        self.coordinate_label.setText("q: -, I: -")
        try:
            payloads = self._pair_curve_payloads()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Could not build pair outputs: {exc}")
            return
        if not payloads:
            self.status_label.setText("Add one or more sample-background pairs first.")
            _apply_iq_axes_style(self.ax)
            self.canvas.draw_idle()
            return
        plotted = 0
        for label, q, intensity, sigma, _record, _background_label, _factor in payloads:
            mask = _plot_mask(q, intensity, self.log_q_check.isChecked(), self.log_i_check.isChecked())
            if np.count_nonzero(mask) < 2:
                continue
            color = _publication_color(plotted, max(1, len(payloads)))
            self.ax.plot(q[mask], intensity[mask], linewidth=1.4, color=color, label=label)
            self._plotted_points.append((q[mask], intensity[mask], label))
            if self.error_check.isChecked() and sigma is not None:
                self._plot_error_bars(q, intensity, sigma, mask, color)
            plotted += 1
        self.ax.set_xscale("log" if self.log_q_check.isChecked() else "linear")
        self.ax.set_yscale("log" if self.log_i_check.isChecked() else "linear")
        _apply_iq_axes_style(self.ax)
        if plotted:
            self._update_plot_legend()
        self.canvas.draw_idle()
        self.status_label.setText(f"Plotted {plotted}/{len(payloads)} pair-subtracted output(s).")

    def _update_plot_legend(self, _state: int | None = None) -> None:
        legend = self.ax.get_legend()
        if self.legend_check.isChecked():
            if legend is None:
                handles, labels = self.ax.get_legend_handles_labels()
                if handles and labels:
                    legend = self.ax.legend(fontsize=8, loc="best")
            if legend is not None:
                legend.set_visible(True)
                legend.set_draggable(True)
        elif legend is not None:
            legend.set_visible(False)
        self.canvas.draw_idle()

    def export_pair_outputs(self) -> None:
        try:
            payloads = self._pair_curve_payloads()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Could not build pair outputs: {exc}")
            return
        if not payloads:
            self.status_label.setText("Add one or more sample-background pairs first.")
            return
        path = Path(self.path_edit.text().strip())
        if len(payloads) == 1:
            default_name = _safe_filename(payloads[0][0]) + ".dat"
            target, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export pair-subtracted I-q data in XAnoS format", str(path.parent / default_name), "XAnoS data files (*.dat)"
            )
            if not target:
                return
            targets = [_xanos_dat_path(Path(target))]
        else:
            folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Export pair-subtracted XAnoS .dat files to folder", str(path.parent))
            if not folder:
                return
            targets = [Path(folder) / (_safe_filename(payload[0]) + ".dat") for payload in payloads]
        try:
            for payload, target in zip(payloads, targets, strict=True):
                label, q, intensity, sigma, record, background_label, factor = payload
                _write_curve_export(
                    target,
                    q,
                    intensity,
                    sigma,
                    label=label,
                    record=record,
                    background_label=background_label,
                    background_factor=factor,
                )
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Could not export pair outputs: {exc}")
            return
        self.status_label.setText(f"Exported {len(targets)} pair-subtracted output(s).")

    def _selected_curve_indices(self) -> list[int]:
        rows = sorted({index.row() for index in self.curve_list.selectedIndexes()})
        return [int(self.curve_list.item(row).data(QtCore.Qt.ItemDataRole.UserRole)) for row in rows]

    def _append_pair_row(self, sample_index: int, background_index: int) -> None:
        sample = self.curves[sample_index]
        background = self.curves[background_index]
        row = self.pair_table.rowCount()
        self.pair_table.insertRow(row)
        output_name = _default_pair_output_name(sample.label)
        items = [
            QtWidgets.QTableWidgetItem(output_name),
            QtWidgets.QTableWidgetItem(_compact_curve_label(sample.label)),
            QtWidgets.QTableWidgetItem(_compact_curve_label(background.label)),
            QtWidgets.QTableWidgetItem("1.0"),
        ]
        items[1].setData(QtCore.Qt.ItemDataRole.UserRole, sample_index)
        items[2].setData(QtCore.Qt.ItemDataRole.UserRole, background_index)
        for column, item in enumerate(items):
            if column in {1, 2}:
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.pair_table.setItem(row, column, item)

    def _pair_row_exists(self, sample_index: int, background_index: int) -> bool:
        for row in range(self.pair_table.rowCount()):
            sample_item = self.pair_table.item(row, 1)
            background_item = self.pair_table.item(row, 2)
            if sample_item is None or background_item is None:
                continue
            if sample_item.data(QtCore.Qt.ItemDataRole.UserRole) == sample_index and background_item.data(QtCore.Qt.ItemDataRole.UserRole) == background_index:
                return True
        return False

    def _pair_curve_payloads(
        self,
    ) -> list[tuple[str, np.ndarray, np.ndarray, np.ndarray | None, H5CurveRecord, str, float]]:
        path = Path(self.path_edit.text().strip())
        payloads: list[tuple[str, np.ndarray, np.ndarray, np.ndarray | None, H5CurveRecord, str, float]] = []
        for row in range(self.pair_table.rowCount()):
            output_item = self.pair_table.item(row, 0)
            sample_item = self.pair_table.item(row, 1)
            background_item = self.pair_table.item(row, 2)
            factor_item = self.pair_table.item(row, 3)
            if sample_item is None or background_item is None:
                continue
            sample_index = sample_item.data(QtCore.Qt.ItemDataRole.UserRole)
            background_index = background_item.data(QtCore.Qt.ItemDataRole.UserRole)
            if sample_index is None or background_index is None:
                continue
            factor = _float_table_value(factor_item, 1.0)
            sample_record = self.curves[int(sample_index)]
            background_record = self.curves[int(background_index)]
            sample_h5 = Path(sample_record.h5_path) if sample_record.h5_path else path
            background_h5 = Path(background_record.h5_path) if background_record.h5_path else path
            q, intensity, sigma = _read_curve_from_h5_path(sample_h5, sample_record)
            background_q, background_i, background_sigma = _read_curve_from_h5_path(background_h5, background_record)
            q, intensity, sigma = _subtract_background_curve(q, intensity, sigma, background_q, background_i, background_sigma, factor)
            output_name = output_item.text().strip() if output_item else ""
            if not output_name:
                output_name = _default_pair_output_name(sample_record.label)
            label = f"{output_name}: {_compact_curve_label(sample_record.label)} - {factor:.5g} x {_compact_curve_label(background_record.label)}"
            payloads.append((label, q, intensity, sigma, sample_record, background_record.label, factor))
        return payloads

    def _curve_index_for_key(self, key: tuple[str, str, int | None]) -> int | None:
        for index, curve in enumerate(self.curves):
            if _record_key(curve) == key:
                return index
        return None

    def _prepared_curve_data(
        self,
        record_index: int,
        default_path: Path,
        background: tuple[np.ndarray, np.ndarray, np.ndarray | None, str] | None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, str, H5CurveRecord]:
        record = self.curves[record_index]
        h5_path = Path(record.h5_path) if record.h5_path else default_path
        q, intensity, sigma = _read_curve_from_h5_path(h5_path, record)
        label = record.label
        if background is not None:
            q, intensity, sigma = _subtract_background_curve(
                q,
                intensity,
                sigma,
                background[0],
                background[1],
                background[2],
                self.background_factor_spin.value(),
            )
            label = f"{record.label} - {self.background_factor_spin.value():.5g} x {background[3]}"
        return q, intensity, sigma, label, record

    def _background_curve_data(self) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, str] | None:
        if self.background_curve is None:
            return None
        return (
            self.background_curve.q,
            self.background_curve.intensity,
            self.background_curve.sigma,
            self.background_curve.label,
        )

    def _plot_error_bars(self, q: np.ndarray, intensity: np.ndarray, sigma: np.ndarray, mask: np.ndarray, color: str) -> None:
        sigma_mask = mask & np.isfinite(sigma) & (sigma >= 0)
        if np.count_nonzero(sigma_mask) < 2:
            return
        x = q[sigma_mask]
        y = intensity[sigma_mask]
        err = sigma[sigma_mask]
        if x.size > MAX_ERROR_BAR_POINTS:
            indices = np.linspace(0, x.size - 1, MAX_ERROR_BAR_POINTS).astype(int)
            x = x[indices]
            y = y[indices]
            err = err[indices]
        self.ax.errorbar(x, y, yerr=err, fmt="none", ecolor=color, alpha=0.35, linewidth=0.7, capsize=0)

    def _on_plot_motion(self, event: Any) -> None:
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            self.coordinate_label.setText("q: -, I: -")
            return
        nearest = _nearest_plotted_point(event.xdata, event.ydata, self._plotted_points)
        if nearest is None:
            self.coordinate_label.setText(f"q: {event.xdata:.6g}, I: {event.ydata:.6g}")
            return
        q_value, i_value, label = nearest
        self.coordinate_label.setText(
            f"cursor q: {event.xdata:.6g}, I: {event.ydata:.6g} | nearest q: {q_value:.6g}, I: {i_value:.6g} ({label})"
        )


class H5StructureViewerDialog(QtWidgets.QDialog):
    """Read-only HDF5 structure and metadata browser."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FrameByFrame-ASWAXS HDF5 Structure / Metadata Viewer")
        self._build_ui()
        apply_tool_theme(self)
        fit_window_to_available_screen(self, (1180, 760), minimum=(760, 520))

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.path_edit = QtWidgets.QLineEdit(self)
        self.path_edit.hide()
        menu_bar = QtWidgets.QMenuBar(self)
        root.setMenuBar(menu_bar)
        file_menu = self.file_menu = menu_bar.addMenu("File")
        file_menu.addAction("Open HDF5...", self.browse_file, QtGui.QKeySequence.StandardKey.Open)
        file_menu.addAction("Reload", self.load_file, QtGui.QKeySequence.StandardKey.Refresh)
        file_menu.addSeparator()
        file_menu.addAction("Close", self.close, QtGui.QKeySequence.StandardKey.Close)

        search_row = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search structure/metadata")
        self.search_edit.textChanged.connect(self._apply_tree_filter)
        search_row.addWidget(QtWidgets.QLabel("Search"))
        search_row.addWidget(self.search_edit, 1)
        root.addLayout(search_row)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Name", "Kind", "Shape", "Dtype", "Preview / attrs"])
        self.tree.header().setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self._show_selected_details)
        splitter.addWidget(self.tree)

        right = QtWidgets.QTabWidget()
        self.details_text = QtWidgets.QPlainTextEdit()
        self.details_text.setReadOnly(True)
        self.metadata_text = QtWidgets.QPlainTextEdit()
        self.metadata_text.setReadOnly(True)
        right.addTab(self.details_text, "Selected Item")
        right.addTab(self.metadata_text, "Metadata Summary")
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.status_label = QtWidgets.QLabel("Choose an HDF5 file.")
        self.status_label.setObjectName("ToolStatus")
        root.addWidget(self.status_label)

    def open_file(self, path: Path | str) -> None:
        self.path_edit.setText(str(path))
        self.load_file()
        self.show()
        self.raise_()
        self.activateWindow()

    def browse_file(self) -> None:
        start = Path(self.path_edit.text()).parent if self.path_edit.text().strip() else Path.home()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open HDF5 structure viewer", str(start), H5_FILTER)
        if path:
            self.path_edit.setText(path)
            self.load_file()

    def load_file(self) -> None:
        path = Path(self.path_edit.text().strip())
        self.tree.clear()
        self.details_text.clear()
        self.metadata_text.clear()
        if not path.exists():
            self.status_label.setText("HDF5 file does not exist.")
            return
        counter = {"items": 0, "truncated": False}
        try:
            with h5py.File(path, "r") as handle:
                root = QtWidgets.QTreeWidgetItem(["/", "file", "", "", _attrs_preview(handle)])
                root.setData(0, QtCore.Qt.ItemDataRole.UserRole, "/")
                self.tree.addTopLevelItem(root)
                add_h5_children(root, handle, counter)
                root.setExpanded(True)
                self.metadata_text.setPlainText(metadata_summary(handle))
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Could not read HDF5: {exc}")
            return
        suffix = " Tree truncated for speed." if counter["truncated"] else ""
        self.status_label.setText(f"Loaded {counter['items']} HDF5 items from {path}.{suffix}")
        self._apply_tree_filter()

    def _show_selected_details(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            self.details_text.clear()
            return
        item = items[0]
        path = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or item.text(0)
        lines = [
            f"Path: {path}",
            f"Kind: {item.text(1)}",
            f"Shape: {item.text(2)}",
            f"Dtype: {item.text(3)}",
            "",
            item.text(4),
        ]
        self.details_text.setPlainText("\n".join(line for line in lines if line is not None))

    def _apply_tree_filter(self) -> None:
        needle = self.search_edit.text().strip().lower()

        def apply(item: QtWidgets.QTreeWidgetItem) -> bool:
            text = " ".join(item.text(column) for column in range(item.columnCount())).lower()
            child_match = False
            for child_index in range(item.childCount()):
                child_match = apply(item.child(child_index)) or child_match
            matched = not needle or needle in text or child_match
            item.setHidden(not matched)
            if needle and child_match:
                item.setExpanded(True)
            return matched

        for index in range(self.tree.topLevelItemCount()):
            apply(self.tree.topLevelItem(index))


def discover_iq_curves(handle: h5py.File, source_path: Path | None = None, source_label: str = "") -> list[H5CurveRecord]:
    records: list[H5CurveRecord] = []

    def visitor(name: str, obj: h5py.Group | h5py.Dataset) -> None:
        if not isinstance(obj, h5py.Group):
            return
        q_obj = _q_dataset_for_group(obj)
        if q_obj is None:
            return
        if q_obj.ndim == 0 or q_obj.ndim > 2:
            return
        candidates = _curve_y_datasets(obj, q_obj)
        energy = obj.get("energy")
        for y_name, i_obj in candidates:
            rows = _curve_row_count(q_obj, i_obj)
            rows = min(rows, MAX_CURVE_ROWS_PER_GROUP)
            sigma_path = _matching_sigma_path(obj, y_name, i_obj.shape)
            for row in range(rows):
                row_index = row if _dataset_has_curve_rows(i_obj) else None
                label = _curve_label(obj, y_name, row_index, energy)
                if source_label:
                    label = f"{source_label} | {label}"
                records.append(
                    H5CurveRecord(
                        label=label,
                        h5_path=str(source_path) if source_path is not None else None,
                        group_path=obj.name,
                        q_path=q_obj.name,
                        i_path=i_obj.name,
                        y_name=y_name,
                        sigma_path=sigma_path,
                        row=row_index,
                    )
                )

    handle.visititems(visitor)
    return sorted(records, key=lambda record: (_stage_sort_key(record.group_path, record.y_name), record.label))


def read_curve_row(handle: h5py.File, record: H5CurveRecord) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    q_dataset = handle[record.q_path]
    intensity_dataset = handle[record.i_path]
    sigma_dataset = handle[record.sigma_path] if record.sigma_path else None
    if record.row is None:
        q = np.asarray(q_dataset[()], dtype=float)
        intensity = np.asarray(intensity_dataset[()], dtype=float)
        sigma = np.asarray(sigma_dataset[()], dtype=float) if sigma_dataset is not None else None
        return q, intensity, sigma
    row = int(record.row)
    intensity = np.asarray(intensity_dataset[row] if intensity_dataset.ndim > 1 else intensity_dataset[()], dtype=float)
    if sigma_dataset is None:
        sigma = None
    else:
        sigma = np.asarray(sigma_dataset[row] if sigma_dataset.ndim > 1 else sigma_dataset[()], dtype=float)
    q_row = row if q_dataset.ndim > 1 and row < q_dataset.shape[0] else 0
    q = np.asarray(q_dataset[q_row] if q_dataset.ndim > 1 else q_dataset[()], dtype=float)
    return q, intensity, sigma


def _read_curve_from_h5_path(h5_path: Path, record: H5CurveRecord) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    with h5py.File(h5_path, "r") as handle:
        q, intensity, sigma = read_curve_row(handle, record)
    q = np.asarray(q, dtype=float).reshape(-1)
    intensity = np.asarray(intensity, dtype=float).reshape(-1)
    n = min(q.size, intensity.size)
    q = q[:n]
    intensity = intensity[:n]
    if sigma is not None:
        sigma = np.asarray(sigma, dtype=float).reshape(-1)[:n]
    return q, intensity, sigma


def _record_key(record: H5CurveRecord) -> tuple[str, str, int | None]:
    return (record.h5_path or "", record.i_path, record.row)


def _curve_matches_source(record: H5CurveRecord, source: str) -> bool:
    if source == "all":
        return True
    text = " ".join(
        value.lower()
        for value in (
            record.label,
            record.group_path,
            record.q_path,
            record.i_path,
            record.h5_path or "",
        )
    )
    if source == "saxs":
        return any(token in text for token in ("pil300k", "saxs")) and not _curve_matches_source(record, "combined")
    if source == "waxs":
        return any(token in text for token in ("eig1m", "eiger", "waxs")) and not _curve_matches_source(record, "combined")
    if source == "combined":
        return any(token in text for token in ("stitched", "combined", "asaxs_outputs", "/final/", "final /", "legacy final"))
    return True


def _plot_mask(q: np.ndarray, intensity: np.ndarray, log_q: bool, log_i: bool) -> np.ndarray:
    mask = np.isfinite(q) & np.isfinite(intensity)
    if log_q:
        mask &= q > 0
    if log_i:
        mask &= intensity > 0
    return mask


def _apply_iq_axes_style(ax: Any) -> None:
    ax.set_xlabel(IQ_X_LABEL)
    ax.set_ylabel(IQ_Y_LABEL)
    ax.grid(True, which="major", color="#d0d0d0", linewidth=0.7, alpha=0.75)
    ax.grid(True, which="minor", color="#e8e8e8", linewidth=0.45, alpha=0.55)
    ax.tick_params(which="both", direction="in", top=True, right=True)
    try:
        ax.set_box_aspect(1)
    except AttributeError:
        pass


def _publication_color(index: int, total: int) -> str:
    if total <= len(PUBLICATION_COLORS):
        return PUBLICATION_COLORS[index % len(PUBLICATION_COLORS)]
    try:
        from matplotlib import colormaps

        return colormaps["viridis"](index / max(1, total - 1))
    except Exception:  # noqa: BLE001 - color fallback should never break plotting.
        return PUBLICATION_COLORS[index % len(PUBLICATION_COLORS)]


def _nearest_plotted_point(
    q_value: float,
    i_value: float,
    plotted_points: list[tuple[np.ndarray, np.ndarray, str]],
) -> tuple[float, float, str] | None:
    best: tuple[float, float, str] | None = None
    best_distance = np.inf
    for q, intensity, label in plotted_points:
        if q.size == 0:
            continue
        distances = (q - q_value) ** 2 + (intensity - i_value) ** 2
        if not distances.size:
            continue
        index = int(np.nanargmin(distances))
        distance = float(distances[index])
        if distance < best_distance:
            best_distance = distance
            best = (float(q[index]), float(intensity[index]), label)
    return best


def _safe_filename(label: str, max_length: int = 120) -> str:
    text = re.sub(r"[^\w.\-]+", "_", label.strip(), flags=re.ASCII).strip("._")
    return (text or "curve")[:max_length]


def _compact_curve_label(label: str, max_length: int = 46) -> str:
    text = str(label).strip()
    if len(text) <= max_length:
        return text
    return "..." + text[-(max_length - 3) :]


def _default_pair_output_name(sample_label: str) -> str:
    text = _safe_filename(sample_label, max_length=40)
    return text or "pair_output"


def _fixed_background_sample_indices(selected: list[int], background_index: int) -> list[int]:
    return list(dict.fromkeys(index for index in selected if index != background_index))


def _float_table_value(item: QtWidgets.QTableWidgetItem | None, default: float) -> float:
    if item is None:
        return default
    try:
        return float(item.text().strip())
    except ValueError:
        return default


def _write_curve_export(
    path: Path,
    q: np.ndarray,
    intensity: np.ndarray,
    sigma: np.ndarray | None,
    *,
    label: str,
    record: H5CurveRecord,
    background_label: str | None,
    background_factor: float | None,
) -> None:
    source_h5 = Path(record.h5_path) if record.h5_path else Path("unknown_analysis.h5")
    energy_kev = np.nan
    header_info: dict[str, float | str | None] = {}
    if record.h5_path and source_h5.exists():
        with h5py.File(source_h5, "r") as handle:
            group = handle.get(record.group_path)
            if isinstance(group, h5py.Group):
                energy_kev, header_info = xanos_curve_header_info(handle, group, record.row)
    metadata_extra: dict[str, object] = {
        "viewer_label": label,
        "q_path": record.q_path,
        "i_path": record.i_path,
    }
    if record.sigma_path:
        metadata_extra["sigma_path"] = record.sigma_path
    if background_label is not None and background_factor is not None:
        metadata_extra["background_label"] = background_label
        metadata_extra["background_factor"] = float(background_factor)
    write_xanos_curve_file(
        _xanos_dat_path(path),
        q,
        intensity,
        sigma,
        analysis_h5=source_h5,
        h5_data_path=record.group_path,
        output_name=label,
        energy_index=(record.row or 0) + 1,
        energy_kev=energy_kev,
        cf=header_info.get("CF", 1.0),
        thickness=header_info.get("Thickness", 1.0),
        xrf_bkg=header_info.get("xrf_bkg", 0.0),
        metadata_extra=metadata_extra,
    )


def _xanos_dat_path(path: Path) -> Path:
    return path if path.suffix.lower() == ".dat" else path.with_suffix(".dat")


def _subtract_background_curve(
    q: np.ndarray,
    intensity: np.ndarray,
    sigma: np.ndarray | None,
    background_q: np.ndarray,
    background_intensity: np.ndarray,
    background_sigma: np.ndarray | None,
    factor: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    background_on_q = _interpolate_to_q(background_q, background_intensity, q)
    corrected = intensity - factor * background_on_q
    corrected_sigma: np.ndarray | None = None
    if sigma is not None:
        corrected_sigma = np.asarray(sigma, dtype=float).copy()
    if background_sigma is not None:
        background_sigma_on_q = _interpolate_to_q(background_q, background_sigma, q)
        if corrected_sigma is None:
            corrected_sigma = np.abs(factor) * background_sigma_on_q
        else:
            corrected_sigma = np.sqrt(corrected_sigma**2 + (factor * background_sigma_on_q) ** 2)
    return q, corrected, corrected_sigma


def _interpolate_to_q(source_q: np.ndarray, source_y: np.ndarray, target_q: np.ndarray) -> np.ndarray:
    source_q = np.asarray(source_q, dtype=float).reshape(-1)
    source_y = np.asarray(source_y, dtype=float).reshape(-1)
    target_q = np.asarray(target_q, dtype=float).reshape(-1)
    n = min(source_q.size, source_y.size)
    source_q = source_q[:n]
    source_y = source_y[:n]
    if source_q.size == target_q.size and np.allclose(source_q, target_q, rtol=1e-7, atol=1e-12, equal_nan=False):
        return source_y.copy()
    mask = np.isfinite(source_q) & np.isfinite(source_y)
    source_q = source_q[mask]
    source_y = source_y[mask]
    if source_q.size < 2:
        return np.full_like(target_q, np.nan, dtype=float)
    order = np.argsort(source_q)
    source_q = source_q[order]
    source_y = source_y[order]
    unique_q, unique_indices = np.unique(source_q, return_index=True)
    unique_y = source_y[unique_indices]
    if unique_q.size < 2:
        return np.full_like(target_q, np.nan, dtype=float)
    interpolated = np.interp(target_q, unique_q, unique_y, left=np.nan, right=np.nan)
    interpolated[~np.isfinite(target_q)] = np.nan
    return interpolated


def add_h5_children(parent: QtWidgets.QTreeWidgetItem, group: h5py.Group | h5py.File, counter: dict[str, Any]) -> None:
    for name in sorted(group.keys()):
        if counter["items"] >= MAX_TREE_ITEMS:
            counter["truncated"] = True
            return
        child = group[name]
        counter["items"] += 1
        if isinstance(child, h5py.Group):
            item = QtWidgets.QTreeWidgetItem([name, "group", "", "", _attrs_preview(child)])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, child.name)
            parent.addChild(item)
            add_h5_children(item, child, counter)
        elif isinstance(child, h5py.Dataset):
            item = QtWidgets.QTreeWidgetItem([name, "dataset", str(child.shape), str(child.dtype), _dataset_preview(child)])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, child.name)
            parent.addChild(item)
            attrs = _attrs_preview(child)
            if attrs:
                attrs_item = QtWidgets.QTreeWidgetItem(["@attrs", "attrs", "", "", attrs])
                attrs_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, child.name + "/@attrs")
                item.addChild(attrs_item)


def metadata_summary(handle: h5py.File) -> str:
    sections: list[str] = []

    def visitor(name: str, obj: h5py.Group | h5py.Dataset) -> None:
        if not isinstance(obj, h5py.Group):
            return
        base_name = name.rsplit("/", 1)[-1]
        if base_name not in METADATA_GROUP_NAMES and not name.endswith("original_metadata"):
            return
        lines = _metadata_group_lines(obj)
        if lines:
            sections.append(f"[/{name}]\n" + "\n".join(lines))

    handle.visititems(visitor)
    return "\n\n".join(sections) if sections else "No metadata/parameter groups found."


def _metadata_group_lines(group: h5py.Group, limit: int = 80) -> list[str]:
    lines: list[str] = []
    for key in sorted(group.keys()):
        child = group[key]
        if isinstance(child, h5py.Dataset):
            lines.append(f"{key}: {_dataset_metadata_value(child)}")
        elif isinstance(child, h5py.Group):
            scalar_children = [
                subkey
                for subkey in sorted(child.keys())
                if isinstance(child[subkey], h5py.Dataset) and (child[subkey].shape == () or child[subkey].size <= 8)
            ]
            if scalar_children:
                lines.append(f"{key}/: " + "; ".join(f"{subkey}={_dataset_metadata_value(child[subkey])}" for subkey in scalar_children[:8]))
        if len(lines) >= limit:
            lines.append("...")
            break
    return lines


def _dataset_metadata_value(dataset: h5py.Dataset) -> str:
    try:
        if dataset.shape == () or dataset.size <= 8:
            return _value_preview(dataset[()])
    except Exception:  # noqa: BLE001
        pass
    return f"shape={dataset.shape}, dtype={dataset.dtype}"


def _curve_label(group: h5py.Group, y_name: str, row: int | None, energy: h5py.Dataset | None) -> str:
    base = _friendly_group_label(group.name)
    y_label = y_name if y_name != "I" else "I"
    if row is None:
        return f"{base} | {y_label}"
    energy_label = ""
    if isinstance(energy, h5py.Dataset) and energy.ndim <= 1 and row < energy.shape[0]:
        try:
            energy_value = float(energy[row])
            if np.isfinite(energy_value):
                energy_label = f" {energy_value:.4f} keV"
        except (TypeError, ValueError):
            energy_label = ""
    return f"{base} | {y_label} row {row + 1:03d}{energy_label}"


def _friendly_group_label(path: str) -> str:
    text = path.strip("/")
    replacements = {
        "entry/asaxs_outputs/": "ASAXS final / ",
        "entry/final/corrected_I_q_E": "Legacy final",
        "entry/process_01_reduction/data": "Detector group averages",
        "entry/process_02_background_subtraction/data": "Background corrected",
        "entry/process_03_glassy_carbon_normalization/data": "GC normalized",
        "entry/stitched_averages/curves/": "Stitched / ",
    }
    for old, new in replacements.items():
        if text.startswith(old):
            return text.replace(old, new, 1)
    return "/" + text


def _is_numeric_dataset(dataset: h5py.Dataset) -> bool:
    return np.issubdtype(dataset.dtype, np.number)


def _h5_paths_from_file_or_folder(path: Path) -> list[Path]:
    if path.is_file():
        paths = [path]
        for detector in ("Pil300K", "Eig1M"):
            detector_dir = path.parent / detector
            if detector_dir.exists() and detector_dir.is_dir():
                paths.extend(sorted(detector_dir.glob("*_analysis.h5")))
                paths.extend(sorted(detector_dir.glob("*_analysis.hdf5")))
        return _unique_paths(paths)
    patterns = ("*_analysis.h5", "*_analysis.hdf5", "analysis.h5", "analysis.hdf5")
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(sorted(path.rglob(pattern)))
    return _unique_paths(paths)


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for h5_path in paths:
        resolved = h5_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(h5_path)
    return unique


def _source_label(root: Path, h5_path: Path) -> str:
    try:
        relative = h5_path.relative_to(root)
    except ValueError:
        relative = h5_path.name
    text = str(relative).replace("\\", "/")
    return text[:-3] if text.endswith(".h5") else text


def _curve_y_datasets(group: h5py.Group, q_dataset: h5py.Dataset) -> list[tuple[str, h5py.Dataset]]:
    candidates: list[tuple[str, h5py.Dataset]] = []
    for name in sorted(group.keys(), key=_y_dataset_sort_key):
        if group[name].name == q_dataset.name:
            continue
        if _is_error_dataset_name(name):
            continue
        child = group[name]
        if not isinstance(child, h5py.Dataset) or not _is_numeric_dataset(child):
            continue
        if not _is_curve_compatible(q_dataset, child):
            continue
        candidates.append((name, child))
    return candidates


def _q_dataset_for_group(group: h5py.Group) -> h5py.Dataset | None:
    for name in Q_DATASET_NAMES:
        child = group.get(name)
        if isinstance(child, h5py.Dataset) and _is_numeric_dataset(child) and _looks_like_q_grid(child):
            return child
    for name, child in group.items():
        if not isinstance(child, h5py.Dataset) or not _is_numeric_dataset(child) or not _looks_like_q_grid(child):
            continue
        lowered = name.lower()
        if lowered in {"q", "q_values", "q_grid"} or lowered.startswith(("q_inv", "q_a", "q_nm")):
            return child
    return None


def _looks_like_q_grid(dataset: h5py.Dataset) -> bool:
    return 1 <= dataset.ndim <= 2 and int(dataset.size) >= MIN_Q_GRID_POINTS


def _is_curve_compatible(q_dataset: h5py.Dataset, y_dataset: h5py.Dataset) -> bool:
    if y_dataset.ndim == 0 or y_dataset.ndim > 2:
        return False
    q_shape = q_dataset.shape
    y_shape = y_dataset.shape
    if q_dataset.ndim == 1 and y_dataset.ndim == 1:
        return q_shape[0] == y_shape[0]
    if q_dataset.ndim == 2 and y_dataset.ndim == 2:
        return q_shape == y_shape
    if q_dataset.ndim == 2 and y_dataset.ndim == 1:
        return q_shape[1] == y_shape[0]
    if q_dataset.ndim == 1 and y_dataset.ndim == 2:
        return q_shape[0] == y_shape[1]
    return False


def _curve_row_count(q_dataset: h5py.Dataset, y_dataset: h5py.Dataset) -> int:
    if _dataset_has_curve_rows(y_dataset):
        return int(y_dataset.shape[0])
    if q_dataset.ndim == 2 and y_dataset.ndim == 1:
        return 1
    return 1


def _dataset_has_curve_rows(dataset: h5py.Dataset) -> bool:
    return dataset.ndim == 2


def _matching_sigma_path(group: h5py.Group, y_name: str, y_shape: tuple[int, ...]) -> str | None:
    candidates = []
    if y_name == "I":
        candidates.extend(["sigma_I", "I_error", "error"])
    elif y_name.startswith("I_"):
        candidates.append("sigma_" + y_name[2:])
    elif y_name.startswith("intensity"):
        candidates.append(y_name.replace("intensity", "sigma", 1))
    for candidate in candidates:
        child = group.get(candidate)
        if isinstance(child, h5py.Dataset) and _is_numeric_dataset(child) and child.shape == y_shape:
            return child.name
    return None


def _is_error_dataset_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in {"sigma", "sigma_i", "i_error", "error", "errors"} or lowered.startswith(("sigma_", "error_"))


def _y_dataset_sort_key(name: str) -> tuple[int, str]:
    if name == "I":
        return (0, name)
    if name.startswith("I_"):
        return (1, name)
    if name.startswith("sigma"):
        return (2, name)
    return (3, name)


def _stage_sort_key(path: str, y_name: str) -> tuple[int, str, str]:
    order = [
        "asaxs_outputs",
        "/final/",
        "process_03_glassy_carbon_normalization",
        "process_02_background_subtraction",
        "stitched_averages",
        "process_01_reduction",
    ]
    rank = next((index for index, token in enumerate(order) if token in path), len(order))
    return (rank, path, y_name)


def _attrs_preview(obj: h5py.Group | h5py.Dataset | h5py.File) -> str:
    parts: list[str] = []
    for key in sorted(obj.attrs.keys()):
        parts.append(f"{key}={_value_preview(obj.attrs[key])}")
        if len(parts) >= 8:
            parts.append("...")
            break
    return "; ".join(parts)


def _dataset_preview(dataset: h5py.Dataset) -> str:
    attrs = _attrs_preview(dataset)
    if dataset.size == 0:
        return attrs
    preview = ""
    if dataset.shape == () or dataset.size <= 8:
        try:
            preview = _value_preview(dataset[()])
        except Exception:  # noqa: BLE001
            preview = ""
    if attrs and preview:
        return f"{preview}; attrs: {attrs}"
    return preview or attrs


def _value_preview(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    array = np.asarray(value)
    if array.shape == ():
        scalar = array.item()
        if isinstance(scalar, bytes):
            return scalar.decode("utf-8", errors="replace")
        return str(scalar)
    flat = array.reshape(-1)
    shown = ", ".join(_value_preview(item) for item in flat[:4])
    suffix = ", ..." if flat.size > 4 else ""
    return f"[{shown}{suffix}]"
