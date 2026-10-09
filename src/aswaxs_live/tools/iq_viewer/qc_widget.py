"""Interactive frame-stability quality review for reduced SAXS series."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets, Signal, Slot
from matplotlib import colormaps
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from aswaxs_live.reduction.frame_qc import (
    FrameSeries,
    FrameSourceSeries,
    FrameStabilityResult,
    FrameStabilitySettings,
    analyze_frame_series,
    discover_frame_source_series,
    discover_stored_frame_stability_results,
    reduce_source_series,
)
from aswaxs_live.app.theme import apply_tool_theme, fit_window_to_available_screen


class _ProgressProxy:
    def __init__(self, emit) -> None:
        self.emit = emit

    def put(self, message: object) -> None:
        self.emit(str(message))


class FrameReductionWorker(QtCore.QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, source: FrameSourceSeries, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.source = source

    def run(self) -> None:
        try:
            series = reduce_source_series(self.source, _ProgressProxy(self.progress.emit))
        except Exception as exc:  # noqa: BLE001 - report provenance/reduction errors in the GUI.
            self.failed.emit(str(exc))
            return
        self.completed.emit(series)


class FrameStabilityWidget(QtWidgets.QWidget):
    """Review frame drift and a conservative recommended averaging prefix."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        show_file_controls: bool = True,
        compact: bool = False,
    ) -> None:
        super().__init__(parent)
        self.show_file_controls = show_file_controls
        self.compact = compact
        self.sources: dict[str, FrameSourceSeries] = {}
        self.stored_results: dict[str, object] = {}
        self.series_cache: dict[str, FrameSeries] = {}
        self.worker: FrameReductionWorker | None = None
        self.result: FrameStabilityResult | None = None
        self._build_ui()
        apply_tool_theme(self)

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.controls_widget = QtWidgets.QWidget()
        controls_root = QtWidgets.QVBoxLayout(self.controls_widget)
        controls_root.setContentsMargins(0, 0, 0, 0)
        controls_root.setSpacing(8)

        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.setMinimumWidth(260)
        self.group_combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.group_combo.currentIndexChanged.connect(self._series_selection_changed)
        self.previous_series_button = QtWidgets.QToolButton()
        self.previous_series_button.setArrowType(QtCore.Qt.ArrowType.LeftArrow)
        self.previous_series_button.setToolTip("Previous QC series (Alt+Left)")
        self.previous_series_button.setShortcut(QtGui.QKeySequence("Alt+Left"))
        self.previous_series_button.clicked.connect(lambda: self._browse_series(-1))
        self.next_series_button = QtWidgets.QToolButton()
        self.next_series_button.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.next_series_button.setToolTip("Next QC series (Alt+Right)")
        self.next_series_button.setShortcut(QtGui.QKeySequence("Alt+Right"))
        self.next_series_button.clicked.connect(lambda: self._browse_series(1))
        self._update_series_navigation()

        if self.compact:
            compact_bar = QtWidgets.QHBoxLayout()
            self.controls_button = QtWidgets.QToolButton()
            self.controls_button.setText("QC Settings")
            self.controls_button.setCheckable(True)
            self.controls_button.setChecked(False)
            self.controls_button.setArrowType(QtCore.Qt.ArrowType.RightArrow)
            self.controls_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.controls_button.toggled.connect(self._set_controls_visible)
            self.table_button = QtWidgets.QToolButton()
            self.table_button.setText("Frame Table")
            self.table_button.setCheckable(True)
            self.table_button.setChecked(False)
            self.table_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.table_button.toggled.connect(self._set_table_visible)
            compact_bar.addWidget(QtWidgets.QLabel("Series"))
            compact_bar.addWidget(self.previous_series_button)
            compact_bar.addWidget(self.group_combo, 1)
            compact_bar.addWidget(self.next_series_button)
            compact_bar.addSpacing(12)
            compact_bar.addWidget(self.controls_button)
            compact_bar.addWidget(self.table_button)
            root.addLayout(compact_bar)

        self.path_edit = QtWidgets.QLineEdit()
        if self.show_file_controls:
            file_row = QtWidgets.QHBoxLayout()
            browse = QtWidgets.QPushButton("Browse")
            browse.clicked.connect(self.browse_file)
            load = QtWidgets.QPushButton("Load History")
            load.clicked.connect(self.load_file)
            file_row.addWidget(QtWidgets.QLabel("Analysis HDF5"))
            file_row.addWidget(self.path_edit, 1)
            file_row.addWidget(browse)
            file_row.addWidget(load)
            controls_root.addLayout(file_row)

        primary_controls = QtWidgets.QHBoxLayout()
        primary_controls.setSpacing(8)
        self.q_auto = QtWidgets.QCheckBox("Auto q range")
        self.q_auto.setChecked(True)
        self.q_min = self._q_spin()
        self.q_max = self._q_spin()
        self.low_auto = QtWidgets.QCheckBox("Auto low-q")
        self.low_auto.setChecked(True)
        self.low_min = self._q_spin()
        self.low_max = self._q_spin()
        self.reference_combo = QtWidgets.QComboBox()
        self.reference_combo.addItem("Compare with first frame", "first")
        self.reference_combo.addItem("Compare with previous frame", "previous")
        self.reference_combo.setMinimumWidth(210)
        self.run_button = QtWidgets.QPushButton("Reduce Frames and Run QC")
        self.run_button.setMinimumWidth(220)
        self.run_button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.run_button.clicked.connect(self.run_analysis)
        show_average = QtWidgets.QPushButton("Show Recommended Average")
        show_average.setMinimumWidth(220)
        show_average.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        show_average.clicked.connect(self.show_recommended_average)
        if not self.compact:
            primary_controls.addWidget(QtWidgets.QLabel("Series"))
            primary_controls.addWidget(self.previous_series_button)
            primary_controls.addWidget(self.group_combo, 1)
            primary_controls.addWidget(self.next_series_button)
        primary_controls.addWidget(QtWidgets.QLabel("Similarity reference"))
        primary_controls.addWidget(self.reference_combo)
        controls_root.addLayout(primary_controls)

        action_controls = QtWidgets.QHBoxLayout()
        action_controls.setSpacing(8)
        action_controls.addWidget(self.run_button, 1)
        action_controls.addWidget(show_average, 1)
        controls_root.addLayout(action_controls)

        range_controls = QtWidgets.QGridLayout()
        range_controls.setHorizontalSpacing(8)
        range_controls.setVerticalSpacing(4)
        range_controls.addWidget(self.q_auto, 0, 0)
        range_controls.addWidget(QtWidgets.QLabel("q minimum"), 0, 1)
        range_controls.addWidget(self.q_min, 0, 2)
        range_controls.addWidget(QtWidgets.QLabel("q maximum"), 0, 3)
        range_controls.addWidget(self.q_max, 0, 4)
        range_controls.addWidget(self.low_auto, 1, 0)
        range_controls.addWidget(QtWidgets.QLabel("low-q minimum"), 1, 1)
        range_controls.addWidget(self.low_min, 1, 2)
        range_controls.addWidget(QtWidgets.QLabel("low-q maximum"), 1, 3)
        range_controls.addWidget(self.low_max, 1, 4)
        range_controls.setColumnStretch(5, 1)
        controls_root.addLayout(range_controls)
        self.q_auto.toggled.connect(self._update_range_controls)
        self.low_auto.toggled.connect(self._update_range_controls)
        self._update_range_controls()
        root.addWidget(self.controls_widget)
        if self.compact:
            self.controls_widget.hide()

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        root.addWidget(splitter, 1)
        plot_panel = QtWidgets.QWidget()
        plot_layout = QtWidgets.QVBoxLayout(plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        self.figure = Figure(figsize=(13, 8), dpi=100, facecolor="white")
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, self)
        plot_layout.addWidget(self.toolbar)
        plot_layout.addWidget(self.canvas, 1)
        splitter.addWidget(plot_panel)

        self.table = QtWidgets.QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(
            ["Frame", "Q/Q1", "Low-q/Q1", "Reduced chi2", "CorMap p", "Run", "Rg", "I(0)", "q*", "FWHM", "QC"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self.table)
        self.plot_table_splitter = splitter
        splitter.setChildrenCollapsible(True)
        splitter.setSizes([690, 220])
        if self.compact:
            self.table.hide()

        self.status_label = QtWidgets.QLabel("Open an analysis HDF5 to read its raw-frame reduction history.")
        self.status_label.setObjectName("ToolStatus")
        self.status_label.setWordWrap(True)
        if self.compact:
            self.status_label.setMaximumHeight(48)
        root.addWidget(self.status_label)

    def _set_controls_visible(self, visible: bool) -> None:
        self.controls_widget.setVisible(visible)
        if hasattr(self, "controls_button"):
            self.controls_button.setArrowType(QtCore.Qt.ArrowType.DownArrow if visible else QtCore.Qt.ArrowType.RightArrow)

    def _set_table_visible(self, visible: bool) -> None:
        if not hasattr(self, "table"):
            return
        self.table.setVisible(visible)
        if visible:
            self.plot_table_splitter.setSizes([700, 240])

    def _browse_series(self, step: int) -> None:
        count = self.group_combo.count()
        if count < 2:
            return
        self.group_combo.setCurrentIndex((self.group_combo.currentIndex() + step) % count)

    def _update_series_navigation(self) -> None:
        enabled = self.group_combo.count() > 1
        self.previous_series_button.setEnabled(enabled)
        self.next_series_button.setEnabled(enabled)

    @staticmethod
    def _q_spin() -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(0.0, 1_000_000.0)
        spin.setDecimals(6)
        spin.setSingleStep(0.001)
        spin.setFixedWidth(92)
        return spin

    def _update_range_controls(self) -> None:
        self.q_min.setEnabled(not self.q_auto.isChecked())
        self.q_max.setEnabled(not self.q_auto.isChecked())
        self.low_min.setEnabled(not self.low_auto.isChecked())
        self.low_max.setEnabled(not self.low_auto.isChecked())

    def open_file(self, path: Path | str) -> None:
        target = self._resolve_analysis_h5(Path(path))
        self.path_edit.setText(str(target if target is not None else path))
        self.load_file()

    def set_frame_series(self, label: str, series: FrameSeries, *, analyze: bool = True) -> None:
        """Load already-reduced frames without reopening their raw HDF5 files."""
        self.sources = {}
        self.stored_results = {}
        self.series_cache = {label: series}
        self.result = None
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem(label)
        self.group_combo.blockSignals(False)
        self._update_series_navigation()
        self.run_button.setText("Run QC")
        self.run_button.setEnabled(True)
        if analyze:
            self._analyze_cached_series(label, series)
        else:
            self.status_label.setText(f"Loaded {series.frame_index.size} in-memory frame curve(s) for {label}.")

    def browse_file(self) -> None:
        start = Path(self.path_edit.text()).parent if self.path_edit.text().strip() else Path.home()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open frame-resolved analysis HDF5", str(start), "HDF5 files (*.h5 *.hdf5);;All files (*)"
        )
        if path:
            self.path_edit.setText(path)
            self.load_file()

    def load_file(self) -> None:
        path = self._resolve_analysis_h5(Path(self.path_edit.text().strip()))
        self.sources = {}
        self.stored_results = {}
        self.series_cache = {}
        self.group_combo.clear()
        self._update_series_navigation()
        if path is None:
            self.status_label.setText("No analysis HDF5 file was found at the selected location.")
            return
        try:
            self.stored_results = discover_stored_frame_stability_results(path)
            self.sources = discover_frame_source_series(path)
        except (OSError, RuntimeError, ValueError) as exc:
            self.status_label.setText(f"Could not load frame series: {exc}")
            return
        self.path_edit.setText(str(path))
        labels = list(self.stored_results)
        labels.extend(label for label in self.sources if label not in self.stored_results)
        self.group_combo.addItems(labels)
        self._update_series_navigation()
        if not labels:
            self.status_label.setText(
                "No usable frame history was found. The analysis must retain its reduction manifest or complete raw-file provenance."
            )
            return
        if self.stored_results:
            self._show_stored_result(labels[0])
        else:
            self.status_label.setText(
                f"Found {len(self.sources)} legacy energy/group series. Select one, then reduce its raw frames for QC."
            )

    def run_analysis(self) -> None:
        label = self.group_combo.currentText()
        if label in self.stored_results:
            self._show_stored_result(label)
            return
        source = self.sources.get(label)
        if source is None or self.worker is not None:
            return
        series = self.series_cache.get(label)
        if series is None:
            self.run_button.setEnabled(False)
            self.group_combo.setEnabled(False)
            self.status_label.setText(
                f"Reducing {len(source.items)} raw frame(s) read-only for {label}. The stored average is not being changed."
            )
            self.worker = FrameReductionWorker(source, self)
            self.worker.progress.connect(self.status_label.setText)
            self.worker.completed.connect(lambda value, selected=label: self._frame_reduction_complete(selected, value))
            self.worker.failed.connect(self._frame_reduction_failed)
            self.worker.finished.connect(self._worker_finished)
            self.worker.start()
            return
        self._analyze_cached_series(label, series)

    def _series_selection_changed(self) -> None:
        label = self.group_combo.currentText()
        if label in self.stored_results:
            self._show_stored_result(label)
        elif label in self.series_cache:
            self.run_button.setText("Run QC")
            self.run_button.setEnabled(True)
            self._analyze_cached_series(label, self.series_cache[label])
        elif label in self.sources:
            self.run_button.setText("Reduce Frames and Run QC")
            self.run_button.setEnabled(True)
            self.result = None
            self.status_label.setText(
                f"{label}: {len(self.sources[label].items)} source frame(s) recorded. Click Reduce Frames and Run QC."
            )

    def _frame_reduction_complete(self, label: str, series: FrameSeries) -> None:
        self.series_cache[label] = series
        self._analyze_cached_series(label, series)

    def _frame_reduction_failed(self, message: str) -> None:
        self.status_label.setText(f"Could not reduce provenance frames: {message}")

    def _worker_finished(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = None
        self.run_button.setText("Run QC Again")
        self.run_button.setEnabled(True)
        self.group_combo.setEnabled(True)

    def _analyze_cached_series(self, label: str, series: FrameSeries) -> None:
        settings = FrameStabilitySettings(
            q_min=None if self.q_auto.isChecked() else self.q_min.value(),
            q_max=None if self.q_auto.isChecked() else self.q_max.value(),
            low_q_min=None if self.low_auto.isChecked() else self.low_min.value(),
            low_q_max=None if self.low_auto.isChecked() else self.low_max.value(),
            reference_mode=str(self.reference_combo.currentData()),
        )
        try:
            self.result = analyze_frame_series(series, settings)
        except ValueError as exc:
            self.result = None
            self.status_label.setText(f"Frame QC could not be calculated: {exc}")
            return
        if self.q_auto.isChecked():
            self.q_min.setValue(self.result.q_range[0])
            self.q_max.setValue(self.result.q_range[1])
        if self.low_auto.isChecked():
            self.low_min.setValue(self.result.low_q_range[0])
            self.low_max.setValue(self.result.low_q_range[1])
        self._plot_result(label, self.result)
        self._fill_table(self.result)
        recommended_count = int(np.count_nonzero(self.result.recommended))
        failure = "none" if self.result.first_failure_frame is None else str(self.result.first_failure_frame)
        onset = "none" if self.result.damage_onset_frame is None else str(self.result.damage_onset_frame)
        self.status_label.setText(
            f"{label}: recommend initial {recommended_count}/{self.result.frame_index.size} frame(s). "
            f"First QC failure: {failure}; three-consecutive-failure onset: {onset}. "
            "Recommendation is advisory and has not changed the stored average."
        )

    def _show_stored_result(self, label: str) -> None:
        stored = self.stored_results.get(label)
        if stored is None:
            return
        if stored.result is None:
            self.result = None
            self.figure.clear()
            axis = self.figure.subplots(1, 1)
            axis.axis("off")
            axis.text(
                0.5,
                0.5,
                stored.message or "Frame-stability QC is not applicable for this series.",
                ha="center",
                va="center",
                wrap=True,
            )
            self.canvas.draw_idle()
            self.table.setRowCount(0)
            self.run_button.setText("QC Not Applicable")
            self.run_button.setEnabled(False)
            self.status_label.setText(
                f"{label}: {stored.message or stored.status}. No raw HDF5 frames were reopened or reduced."
            )
            return
        self.result = stored.result
        self.q_min.setValue(self.result.q_range[0])
        self.q_max.setValue(self.result.q_range[1])
        self.low_min.setValue(self.result.low_q_range[0])
        self.low_max.setValue(self.result.low_q_range[1])
        self._plot_result(label, self.result)
        self._fill_table(self.result)
        recommended_count = int(np.count_nonzero(self.result.recommended))
        failure = "none" if self.result.first_failure_frame is None else str(self.result.first_failure_frame)
        onset = "none" if self.result.damage_onset_frame is None else str(self.result.damage_onset_frame)
        self.run_button.setText("Stored QC Loaded")
        self.run_button.setEnabled(False)
        self.status_label.setText(
            f"{label}: stored averaging-time QC loaded; no raw HDF5 frames were reopened or reduced. "
            f"Recommended initial frames: {recommended_count}/{self.result.frame_index.size}; "
            f"first failure: {failure}; consecutive-failure onset: {onset}."
        )

    def _plot_result(self, label: str, result: FrameStabilityResult) -> None:
        self.figure.clear()
        axes = self.figure.subplots(2, 3)
        frames = result.frame_index
        colors = colormaps["viridis"](np.linspace(0.05, 0.95, frames.size))
        for index, values in enumerate(result.intensity_common):
            axes[0, 0].plot(result.q_common, values, color=colors[index], linewidth=0.9, alpha=0.8)
        axes[0, 0].set_xscale("log")
        axes[0, 0].set_yscale("log")
        axes[0, 0].set_xlabel(r"$q$ ($\mathrm{\AA}^{-1}$)")
        axes[0, 0].set_ylabel(r"$I(q)$ (a.u.)")
        axes[0, 0].set_title("Frame-resolved I(q)")

        heatmap = axes[0, 1].pcolormesh(
            result.q_common,
            frames,
            result.relative_intensity,
            shading="auto",
            cmap="RdBu_r",
            vmin=0.95,
            vmax=1.05,
        )
        axes[0, 1].set_xscale("log")
        axes[0, 1].set_xlabel(r"$q$ ($\mathrm{\AA}^{-1}$)")
        axes[0, 1].set_ylabel("Frame")
        axes[0, 1].set_title(r"$I_i(q)/I_1(q)$")
        self.figure.colorbar(heatmap, ax=axes[0, 1], pad=0.02)

        self._ratio_axis(axes[0, 2], frames, result.invariant_ratio, r"$Q_i/Q_1$", "Invariant-like stability")
        self._ratio_axis(axes[1, 0], frames, result.low_q_ratio, r"$I_{low,i}/I_{low,1}$", "Low-q stability")

        axes[1, 1].plot(frames, result.reduced_chi2, "o-", color="#0072B2", label=r"reduced $\chi^2$")
        axes[1, 1].axhline(3.0, color="#D55E00", linestyle="--", linewidth=1)
        axes[1, 1].set_xlabel("Frame")
        axes[1, 1].set_ylabel(r"reduced $\chi^2$")
        cormap_axis = axes[1, 1].twinx()
        cormap_axis.plot(frames, result.cormap_p, "s-", color="#009E73", label="CorMap p")
        cormap_axis.axhline(0.01, color="#009E73", linestyle=":", linewidth=1)
        cormap_axis.set_ylabel("CorMap p")
        axes[1, 1].set_title("Statistical similarity")

        axes[1, 2].plot(frames, result.rg, "o-", color="#CC79A7", label="Rg")
        axes[1, 2].set_xlabel("Frame")
        axes[1, 2].set_ylabel(r"$R_g$ ($\mathrm{\AA}$)")
        peak_axis = axes[1, 2].twinx()
        peak_axis.plot(frames, result.peak_q, "s-", color="#E69F00", label=r"q*")
        peak_axis.set_ylabel(r"$q^*$ ($\mathrm{\AA}^{-1}$)")
        axes[1, 2].set_title("Optional structural trends")
        for axis in axes.flat:
            axis.grid(True, color="#d9dde3", linewidth=0.5, alpha=0.8)
        self.figure.suptitle(label)
        self.figure.subplots_adjust(
            left=0.065,
            right=0.94,
            bottom=0.10,
            top=0.88,
            wspace=0.42,
            hspace=0.62 if self.compact else 0.48,
        )
        self.canvas.draw_idle()

    @staticmethod
    def _ratio_axis(axis, frames: np.ndarray, values: np.ndarray, ylabel: str, title: str) -> None:
        axis.plot(frames, values, "o-", color="#0072B2")
        axis.axhline(1.0, color="#20242a", linewidth=0.8)
        axis.axhspan(0.95, 1.05, color="#009E73", alpha=0.10)
        axis.set_xlabel("Frame")
        axis.set_ylabel(ylabel)
        axis.set_title(title)

    def _fill_table(self, result: FrameStabilityResult) -> None:
        self.table.setRowCount(result.frame_index.size)
        arrays = [
            result.frame_index,
            result.invariant_ratio,
            result.low_q_ratio,
            result.reduced_chi2,
            result.cormap_p,
            result.longest_run,
            result.rg,
            result.i0,
            result.peak_q,
            result.peak_fwhm,
        ]
        for row in range(result.frame_index.size):
            for column, values in enumerate(arrays):
                value = values[row]
                text = str(int(value)) if column in {0, 5} else (f"{float(value):.6g}" if np.isfinite(value) else "-")
                item = QtWidgets.QTableWidgetItem(text)
                item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
            label = result.labels[row]
            item = QtWidgets.QTableWidgetItem(label)
            item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            color = {"Good": "#dff2e5", "Acceptable": "#fff3cd", "Bad": "#f8d7da"}[label]
            item.setBackground(QtGui.QColor(color))
            self.table.setItem(row, 10, item)

    def show_recommended_average(self) -> None:
        if self.result is None or not np.any(self.result.recommended):
            self.status_label.setText("Run QC before displaying a recommended average.")
            return
        axis = self.figure.axes[0] if self.figure.axes else None
        if axis is None:
            return
        average = np.nanmean(self.result.intensity_common[self.result.recommended], axis=0)
        axis.plot(self.result.q_common, average, color="black", linewidth=2.4, label="recommended average")
        axis.legend(loc="best", fontsize=8)
        self.canvas.draw_idle()

    @staticmethod
    def _resolve_analysis_h5(path: Path) -> Path | None:
        path = path.expanduser()
        if path.is_file() and path.suffix.lower() in {".h5", ".hdf5"}:
            return path.resolve()
        if not path.is_dir():
            return None
        candidates = list(path.glob("*_analysis.h5")) + list(path.glob("analysis.h5"))
        if not candidates:
            candidates = list(path.rglob("*_analysis.h5"))
        return max(candidates, key=lambda item: item.stat().st_mtime_ns).resolve() if candidates else None


class FrameStabilityDialog(QtWidgets.QDialog):
    """Standalone wrapper retained for direct tool testing and future reuse."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SAXS Frame Stability QC")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        menu_bar = QtWidgets.QMenuBar(self)
        layout.setMenuBar(menu_bar)
        self.widget = FrameStabilityWidget(self, show_file_controls=False)
        file_menu = self.file_menu = menu_bar.addMenu("File")
        file_menu.addAction("Open Analysis HDF5...", self.widget.browse_file, QtGui.QKeySequence.StandardKey.Open)
        file_menu.addAction("Reload", self.widget.load_file, QtGui.QKeySequence.StandardKey.Refresh)
        file_menu.addSeparator()
        file_menu.addAction("Close", self.close, QtGui.QKeySequence.StandardKey.Close)
        layout.addWidget(self.widget)
        fit_window_to_available_screen(self, (1500, 960), minimum=(860, 620))

    def open_file(self, path: Path | str) -> None:
        self.widget.open_file(path)
        self.show()
        self.raise_()
        self.activateWindow()
