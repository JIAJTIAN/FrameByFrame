from __future__ import annotations

import numpy as np
from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets, Signal, Slot
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from aswaxs_live.tools.iq_viewer.qc_widget import FrameStabilityWidget
from aswaxs_live.tools.iq_viewer.viewer import IQ_X_LABEL, IQ_Y_LABEL, PUBLICATION_COLORS

from .session import OnlineCurveRecord, OnlineCurveStore


class OnlineAnalysisWorkspace(QtWidgets.QWidget):
    """Live curve catalog, detector images, and in-memory frame QC."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = OnlineCurveStore()
        self._build_ui()

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        view_bar = QtWidgets.QHBoxLayout()
        self.browser_button = QtWidgets.QToolButton()
        self.browser_button.setText("Curve Browser")
        self.browser_button.setCheckable(True)
        self.browser_button.setChecked(True)
        self.browser_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.browser_button.setArrowType(QtCore.Qt.ArrowType.DownArrow)
        self.browser_button.toggled.connect(self._set_browser_visible)
        view_bar.addWidget(self.browser_button)
        view_bar.addStretch(1)
        root.addLayout(view_bar)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root.addWidget(self.splitter, 1)

        self.list_panel = QtWidgets.QWidget()
        self.list_panel.setMinimumWidth(220)
        self.list_panel.setMaximumWidth(340)
        list_layout = QtWidgets.QVBoxLayout(self.list_panel)
        list_layout.setContentsMargins(0, 0, 6, 0)
        list_layout.addWidget(QtWidgets.QLabel("Finished 1-D frame curves"))
        self.curve_list = QtWidgets.QListWidget()
        self.curve_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.curve_list.itemDoubleClicked.connect(lambda _item: self.plot_selected())
        list_layout.addWidget(self.curve_list, 1)
        buttons = QtWidgets.QVBoxLayout()
        buttons.setSpacing(5)
        self.plot_button = QtWidgets.QPushButton("Plot Selected")
        self.plot_button.setObjectName("PrimaryActionButton")
        self.plot_button.clicked.connect(self.plot_selected)
        self.qc_button = QtWidgets.QPushButton("Run QC on Selection")
        self.qc_button.setObjectName("PrimaryActionButton")
        self.qc_button.clicked.connect(self.run_qc)
        buttons.addWidget(self.plot_button)
        buttons.addWidget(self.qc_button)
        list_layout.addLayout(buttons)
        self.selection_status = QtWidgets.QLabel("Waiting for reduced curves.")
        self.selection_status.setWordWrap(True)
        list_layout.addWidget(self.selection_status)
        self.splitter.addWidget(self.list_panel)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setMinimumWidth(0)
        self.tabs.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        self.tabs.addTab(self._curve_page(), "I-q Curves")
        self.tabs.addTab(self._image_page(), "Detector Images")
        self.qc_widget = FrameStabilityWidget(self, show_file_controls=False, compact=True)
        self.qc_widget.setMinimumWidth(0)
        self.qc_widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        self.tabs.addTab(self.qc_widget, "Frame Stability QC")
        self.splitter.addWidget(self.tabs)
        self.splitter.setSizes([275, 1000])
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)

    def _set_browser_visible(self, visible: bool) -> None:
        self.list_panel.setVisible(visible)
        self.browser_button.setArrowType(QtCore.Qt.ArrowType.DownArrow if visible else QtCore.Qt.ArrowType.RightArrow)
        if visible:
            self.splitter.setSizes([275, max(700, self.splitter.width() - 275)])

    def _curve_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        self.curve_figure = Figure(figsize=(7, 6), dpi=100, facecolor="white")
        self.curve_canvas = FigureCanvas(self.curve_figure)
        self.curve_toolbar = NavigationToolbar(self.curve_canvas, self)
        self.curve_axis = self.curve_figure.add_subplot(111)
        self._style_iq_axis()
        layout.addWidget(self.curve_toolbar)
        layout.addWidget(self.curve_canvas, 1)
        return page

    def _image_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)
        self.image_views: dict[str, list[tuple[Figure, FigureCanvas, object]]] = {
            "Pil300K": [],
            "Eig1M": [],
        }
        layout.addWidget(self._detector_compare_page(), 1)
        return page

    def _detector_compare_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        figure = Figure(figsize=(11, 5.5), dpi=100, facecolor="white")
        canvas = FigureCanvas(figure)
        axes = figure.subplots(1, 2)
        for detector, title, axis in zip(
            ("Pil300K", "Eig1M"), ("Pil300K / SAXS", "Eig1M / WAXS"), axes
        ):
            axis.set_title(title)
            axis.set_axis_off()
            self.image_views[detector].append((figure, canvas, axis))
        layout.addWidget(NavigationToolbar(canvas, self))
        layout.addWidget(canvas, 1)
        return page

    @Slot(object)
    def add_curve(self, payload: object) -> None:
        record = self.store.add_payload(dict(payload))
        item = QtWidgets.QListWidgetItem(record.label)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, len(self.store.records) - 1)
        item.setToolTip(record.source_path)
        self.curve_list.addItem(item)
        self.selection_status.setText(f"{len(self.store.records)} reduced frame curve(s) available in this session.")
        self.curve_list.clearSelection()
        item.setSelected(True)
        self.curve_list.scrollToItem(item)
        self.plot_selected(switch_tab=False)

    @Slot(str, object, object)
    def update_detector_image(self, detector: str, image: object, metadata: object) -> None:
        values = np.asarray(image, dtype=float)
        display = np.log1p(np.clip(values, 0.0, None))
        info = dict(metadata)
        title = (
            f"{info.get('experiment_title', 'Online experiment')} | {detector} | "
            f"M{int(info['group_index']):04d} F{int(info['frame_index']):03d}"
        )
        canvases: set[FigureCanvas] = set()
        figures: set[Figure] = set()
        for figure, canvas, axis in self.image_views.get(detector, []):
            axis.clear()
            axis.imshow(display, origin="lower", cmap="magma", interpolation="nearest", aspect="equal")
            axis.set_title(title)
            axis.set_xlabel("Detector x (pixel)")
            axis.set_ylabel("Detector y (pixel)")
            figures.add(figure)
            canvases.add(canvas)
        for figure in figures:
            figure.tight_layout()
        for canvas in canvases:
            canvas.draw_idle()

    def selected_indices(self) -> list[int]:
        return sorted(int(item.data(QtCore.Qt.ItemDataRole.UserRole)) for item in self.curve_list.selectedItems())

    def plot_selected(self, *, switch_tab: bool = True) -> None:
        indices = self.selected_indices()
        if not indices:
            self.selection_status.setText("Select one or more completed curves.")
            return
        self.curve_axis.clear()
        for number, index in enumerate(indices[:100]):
            record = self.store.records[index]
            self.curve_axis.plot(
                record.q,
                record.intensity,
                linewidth=1.0,
                color=PUBLICATION_COLORS[number % len(PUBLICATION_COLORS)],
                label=record.label,
            )
        self._style_iq_axis()
        if len(indices) <= 15:
            self.curve_axis.legend(fontsize=7, loc="best")
        self.curve_figure.tight_layout()
        self.curve_canvas.draw_idle()
        if switch_tab:
            self.tabs.setCurrentIndex(0)
        suffix = " (first 100 shown)" if len(indices) > 100 else ""
        self.selection_status.setText(f"Plotted {min(len(indices), 100)} selected curve(s){suffix}.")

    def run_qc(self) -> None:
        try:
            label, series = self.store.frame_series(self.selected_indices())
            self.qc_widget.set_frame_series(label, series, analyze=True)
        except ValueError as exc:
            self.selection_status.setText(str(exc))
            return
        self.tabs.setCurrentIndex(2)
        self.selection_status.setText(f"QC calculated from {series.frame_index.size} selected in-memory curves.")

    def _style_iq_axis(self) -> None:
        self.curve_axis.set_xscale("log")
        self.curve_axis.set_yscale("log")
        self.curve_axis.set_xlabel(IQ_X_LABEL)
        self.curve_axis.set_ylabel(IQ_Y_LABEL)
        self.curve_axis.grid(True, which="both", color="#d9dde3", linewidth=0.5, alpha=0.75)
