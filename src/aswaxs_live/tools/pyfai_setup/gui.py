"""Calibration GUI for HDF5 inspection, EDF export, PONI setup, and masks."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from aswaxs_live.app.qt_runtime import suppress_glx_warning

suppress_glx_warning()

from aswaxs_live.app.qt import QtCore, QtGui, QtWidgets, Signal, Slot

import matplotlib
matplotlib.use("qtagg")
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from pyFAI import load as load_poni

from aswaxs_live.app.theme import apply_tool_theme, fit_window_to_available_screen

from .io_utils import DEFAULT_DATASET_PATH, load_hdf5_image
from .processing import (
    export_image_as_edf,
    export_mask_as_edf,
    launch_pyfai_calib2,
    launch_pyfai_drawmask,
    launch_pyfai_integrate,
    load_mask,
    load_mask_from_edf,
    load_poni_summary,
    save_mask,
    write_pyfai_integrate_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class PreprocessingWindow(QtWidgets.QMainWindow):
    """Interactive pyFAI setup window; scientific reduction happens elsewhere."""
    def __init__(self, initial_path: str | None = None):
        super().__init__()
        self.setWindowTitle("FrameByFrame-ASWAXS pyFAI Setup")

        self.current_file: Path | None = None
        self.current_image: np.ndarray | None = None
        self.current_dataset_path = DEFAULT_DATASET_PATH
        self.current_metadata: dict[str, object] = {}
        self.current_mask: np.ndarray | None = None
        self.current_mask_path: Path | None = None
        self.current_poni_path: Path | None = None
        self.current_edf_path: Path | None = None
        self.current_mask_edf_path: Path | None = None
        self.current_integrate_config_path: Path | None = None
        self.current_integrate_output_dir: Path | None = None
        self.current_radial_curve: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self._calib2_process = None
        self._drawmask_process = None
        self._integrate_process = None

        self._build_ui()
        self._build_menus()
        apply_tool_theme(self)
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.tabBar().setStyleSheet(
            "QTabBar::tab { min-width: 70px; padding: 5px 8px; }"
        )
        fit_window_to_available_screen(self, (1680, 1020), minimum=(900, 640))
        if initial_path:
            self.load_hdf5_file(initial_path)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.main_splitter = QtWidgets.QSplitter()
        root.addWidget(self.main_splitter)

        left = QtWidgets.QScrollArea()
        left.setWidgetResizable(True)
        left_content = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_content)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(10)
        left.setWidget(left_content)
        left.setMinimumWidth(440)
        left.setMaximumWidth(520)
        self.main_splitter.addWidget(left)

        figure_widget = QtWidgets.QWidget()
        figure_layout = QtWidgets.QVBoxLayout(figure_widget)
        figure_layout.setContentsMargins(0, 0, 0, 0)
        self.figure = Figure(figsize=(12, 9))
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, self)
        self.image_ax = self.figure.add_subplot(111)
        figure_layout.addWidget(self.toolbar)
        figure_layout.addWidget(self.canvas)
        self.main_splitter.addWidget(figure_widget)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([460, 1200])

        self.tabs = QtWidgets.QTabWidget()
        left_layout.addWidget(self.tabs, 1)
        self.load_tab = QtWidgets.QWidget()
        self.calibration_tab = QtWidgets.QWidget()
        self.mask_tab = QtWidgets.QWidget()
        self.integrate_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.load_tab, "1. Load")
        self.tabs.addTab(self.calibration_tab, "2. Calibrate")
        self.tabs.addTab(self.mask_tab, "3. Mask")
        self.tabs.addTab(self.integrate_tab, "4. Integrate")

        self.load_layout = QtWidgets.QVBoxLayout(self.load_tab)
        self.load_layout.setContentsMargins(0, 0, 0, 0)
        self.calibration_layout = QtWidgets.QVBoxLayout(self.calibration_tab)
        self.calibration_layout.setContentsMargins(0, 0, 0, 0)
        self.calibration_layout.setSpacing(10)
        self.mask_layout = QtWidgets.QVBoxLayout(self.mask_tab)
        self.mask_layout.setContentsMargins(0, 0, 0, 0)
        self.mask_layout.setSpacing(10)
        self.integrate_layout = QtWidgets.QVBoxLayout(self.integrate_tab)
        self.integrate_layout.setContentsMargins(0, 0, 0, 0)
        self.integrate_layout.setSpacing(10)

        self._build_load_group()
        self._build_calibration_group()
        self._build_mask_group()
        self._build_integrate_group()
        for page_layout in (self.load_layout, self.calibration_layout, self.mask_layout, self.integrate_layout):
            page_layout.addStretch(1)

        navigation = QtWidgets.QHBoxLayout()
        self.step_back_button = QtWidgets.QPushButton("Back")
        self.step_next_button = QtWidgets.QPushButton("Next")
        self.step_next_button.setObjectName("PrimaryActionButton")
        self.step_back_button.clicked.connect(lambda: self._set_step(self.tabs.currentIndex() - 1))
        self.step_next_button.clicked.connect(lambda: self._set_step(self.tabs.currentIndex() + 1))
        navigation.addWidget(self.step_back_button)
        navigation.addStretch(1)
        navigation.addWidget(self.step_next_button)
        left_layout.addLayout(navigation)
        self.tabs.currentChanged.connect(self._step_changed)
        self._step_changed(0)

        self.status_label = QtWidgets.QLabel("Load an HDF5 file to begin.")
        self.status_label.setObjectName("ToolStatus")
        self.status_label.setWordWrap(True)
        left_layout.addWidget(self.status_label)
        left_layout.addStretch(1)

    def _build_load_group(self) -> None:
        box = QtWidgets.QGroupBox("1. Loaded HDF5")
        form = QtWidgets.QFormLayout(box)

        self.file_path_edit = QtWidgets.QLineEdit(self)
        self.file_path_edit.hide()
        self.dataset_path_edit = QtWidgets.QLineEdit(DEFAULT_DATASET_PATH, self)
        self.dataset_path_edit.hide()

        self.file_info_label = QtWidgets.QLabel("No file loaded.")
        self.file_info_label.setWordWrap(True)
        self.detector_info_label = QtWidgets.QLabel("Detector: -")
        self.detector_info_label.setWordWrap(True)
        self.metadata_label = QtWidgets.QLabel("Metadata: -")
        self.metadata_label.setWordWrap(True)
        self.detector_position_label = QtWidgets.QLabel("Detector position: -")
        self.detector_position_label.setWordWrap(True)

        open_note = QtWidgets.QLabel("Use File > Open HDF5 to begin or change the source image.")
        open_note.setWordWrap(True)
        open_note.setObjectName("BuilderReview")
        form.addRow(open_note)
        form.addRow("Loaded file", self.file_info_label)
        form.addRow("Detector", self.detector_info_label)
        form.addRow("Metadata", self.metadata_label)
        form.addRow("Position", self.detector_position_label)
        self.load_layout.addWidget(box)

    def _build_menus(self) -> None:
        file_menu = self.file_menu = self.menuBar().addMenu("File")
        file_menu.addAction("Open HDF5...", self._browse_hdf5, QtGui.QKeySequence.StandardKey.Open)
        file_menu.addAction("HDF5 Dataset Path...", self._edit_dataset_path)
        file_menu.addSeparator()
        file_menu.addAction("Load PONI...", self._load_poni_dialog)
        file_menu.addAction("Load Mask...", self._load_mask_dialog)
        file_menu.addAction("Save Mask...", self._save_mask_dialog)
        file_menu.addAction("Save Radial I(q)...", self._save_current_radial_curve)
        file_menu.addSeparator()
        file_menu.addAction("Close", self.close, QtGui.QKeySequence.StandardKey.Close)

        view_menu = self.view_menu = self.menuBar().addMenu("View")
        for index, label in enumerate(("Load", "Calibrate", "Mask", "Integrate")):
            view_menu.addAction(label, lambda _checked=False, step=index: self._set_step(step))

    def _edit_dataset_path(self) -> None:
        value, accepted = QtWidgets.QInputDialog.getText(
            self,
            "HDF5 Dataset Path",
            "Image dataset path",
            text=self.dataset_path_edit.text().strip() or DEFAULT_DATASET_PATH,
        )
        if accepted and value.strip():
            self.dataset_path_edit.setText(value.strip())
            self.status_label.setText(f"HDF5 image dataset: {value.strip()}")

    def _set_step(self, index: int) -> None:
        self.tabs.setCurrentIndex(max(0, min(index, self.tabs.count() - 1)))

    def _step_changed(self, index: int) -> None:
        self.step_back_button.setEnabled(index > 0)
        self.step_next_button.setVisible(index < self.tabs.count() - 1)

    def _build_integrate_group(self) -> None:
        box = QtWidgets.QGroupBox("4. Test Integration In pyFAI")
        form = QtWidgets.QFormLayout(box)

        self.integrate_note_label = QtWidgets.QLabel(
            "Uses HDF5 metadata, the exported EDF bridge, optional loaded PONI/mask, and these settings to open pyFAI-integrate."
        )
        self.integrate_note_label.setWordWrap(True)
        self.integrate_npt_spin = QtWidgets.QSpinBox()
        self.integrate_npt_spin.setRange(16, 20000)
        self.integrate_npt_spin.setValue(1000)
        self.integrate_unit_edit = QtWidgets.QLineEdit("q_A^-1")
        self.azimuth_min_spin = QtWidgets.QDoubleSpinBox()
        self.azimuth_min_spin.setRange(-360.0, 360.0)
        self.azimuth_min_spin.setDecimals(3)
        self.azimuth_min_spin.setValue(-180.0)
        self.azimuth_max_spin = QtWidgets.QDoubleSpinBox()
        self.azimuth_max_spin.setRange(-360.0, 360.0)
        self.azimuth_max_spin.setDecimals(3)
        self.azimuth_max_spin.setValue(180.0)
        azimuth_row = QtWidgets.QWidget()
        azimuth_layout = QtWidgets.QHBoxLayout(azimuth_row)
        azimuth_layout.setContentsMargins(0, 0, 0, 0)
        azimuth_layout.addWidget(QtWidgets.QLabel("min"))
        azimuth_layout.addWidget(self.azimuth_min_spin)
        azimuth_layout.addWidget(QtWidgets.QLabel("max"))
        azimuth_layout.addWidget(self.azimuth_max_spin)
        azimuth_layout.addWidget(QtWidgets.QLabel("deg"))
        self.integrate_output_dir_edit = QtWidgets.QLineEdit()
        self.integrate_output_dir_edit.setPlaceholderText("Choose pyFAI-integrate output folder.")
        browse_integrate_output = QtWidgets.QPushButton("Browse")
        browse_integrate_output.clicked.connect(self._browse_integrate_output_dir)
        integrate_output_row = QtWidgets.QWidget()
        integrate_output_layout = QtWidgets.QHBoxLayout(integrate_output_row)
        integrate_output_layout.setContentsMargins(0, 0, 0, 0)
        integrate_output_layout.addWidget(self.integrate_output_dir_edit, 1)
        integrate_output_layout.addWidget(browse_integrate_output)
        self.integrate_config_label = QtWidgets.QLabel("Config: -")
        self.integrate_config_label.setWordWrap(True)
        self.integrate_output_label = QtWidgets.QLabel("Output: -")
        self.integrate_output_label.setWordWrap(True)

        radial_preview_button = QtWidgets.QPushButton("Integrate Current Image")
        radial_preview_button.clicked.connect(self._integrate_current_image)
        save_curve_button = QtWidgets.QPushButton("Save Current I(q)")
        save_curve_button.clicked.connect(self._save_current_radial_curve)
        integrate_button = QtWidgets.QPushButton("Launch pyFAI-integrate")
        integrate_button.clicked.connect(self._launch_integrate)

        form.addRow("", self.integrate_note_label)
        form.addRow("Radial bins", self.integrate_npt_spin)
        form.addRow("Unit", self.integrate_unit_edit)
        form.addRow("Azimuth range", azimuth_row)
        form.addRow("Output folder", integrate_output_row)
        integrate_actions = QtWidgets.QWidget()
        integrate_actions_layout = QtWidgets.QGridLayout(integrate_actions)
        integrate_actions_layout.setContentsMargins(0, 0, 0, 0)
        integrate_actions_layout.addWidget(radial_preview_button, 0, 0)
        integrate_actions_layout.addWidget(save_curve_button, 0, 1)
        integrate_actions_layout.addWidget(integrate_button, 1, 0, 1, 2)
        form.addRow("", integrate_actions)
        form.addRow("pyFAI config", self.integrate_config_label)
        form.addRow("pyFAI output", self.integrate_output_label)
        self.integrate_layout.addWidget(box)

    def _build_calibration_group(self) -> None:
        box = QtWidgets.QGroupBox("2. Calibration In pyFAI")
        form = QtWidgets.QFormLayout(box)

        self.calibrant_edit = QtWidgets.QLineEdit("AgBh")
        self.calibration_note_label = QtWidgets.QLabel(
            "Workflow: load HDF5, export EDF bridge, open pyFAI-calib2, save .poni, then load the .poni back here."
        )
        self.calibration_note_label.setWordWrap(True)
        self.edf_path_label = QtWidgets.QLabel("EDF bridge: -")
        self.edf_path_label.setWordWrap(True)
        self.calib2_prefill_label = QtWidgets.QLabel("pyFAI prefill: detector=-, energy=-, distance=-")
        self.calib2_prefill_label.setWordWrap(True)
        self.poni_path_label = QtWidgets.QLabel("PONI: -")
        self.poni_path_label.setWordWrap(True)
        self.poni_summary_label = QtWidgets.QLabel("Calibration summary: -")
        self.poni_summary_label.setWordWrap(True)

        export_button = QtWidgets.QPushButton("Export EDF Bridge")
        export_button.clicked.connect(self._export_current_edf)
        calib2_button = QtWidgets.QPushButton("Launch pyFAI-calib2")
        calib2_button.clicked.connect(self._launch_calib2)
        load_poni_button = QtWidgets.QPushButton("Load PONI")
        load_poni_button.clicked.connect(self._load_poni_dialog)

        form.addRow("Calibrant", self.calibrant_edit)
        form.addRow("", self.calibration_note_label)
        calibration_actions = QtWidgets.QWidget()
        calibration_actions_layout = QtWidgets.QGridLayout(calibration_actions)
        calibration_actions_layout.setContentsMargins(0, 0, 0, 0)
        calibration_actions_layout.addWidget(export_button, 0, 0)
        calibration_actions_layout.addWidget(calib2_button, 0, 1)
        calibration_actions_layout.addWidget(load_poni_button, 1, 0, 1, 2)
        form.addRow("", calibration_actions)
        form.addRow("EDF bridge", self.edf_path_label)
        form.addRow("pyFAI prefill", self.calib2_prefill_label)
        form.addRow("Loaded PONI", self.poni_path_label)
        form.addRow("PONI summary", self.poni_summary_label)
        self.calibration_layout.addWidget(box)

    def _build_mask_group(self) -> None:
        box = QtWidgets.QGroupBox("3. Mask In pyFAI")
        form = QtWidgets.QFormLayout(box)

        self.mask_note_label = QtWidgets.QLabel(
            "Workflow: export EDF bridge for mask authoring, open pyFAI-drawmask, save the mask, then import it here."
        )
        self.mask_note_label.setWordWrap(True)
        self.mask_path_label = QtWidgets.QLabel("Mask: -")
        self.mask_path_label.setWordWrap(True)
        self.mask_stats_label = QtWidgets.QLabel("Mask pixels: -")
        self.mask_stats_label.setWordWrap(True)

        export_mask_edf_button = QtWidgets.QPushButton("Export Mask EDF Bridge")
        export_mask_edf_button.clicked.connect(self._export_mask_edf_bridge)
        drawmask_button = QtWidgets.QPushButton("Launch pyFAI-drawmask")
        drawmask_button.clicked.connect(self._launch_drawmask)
        import_mask_button = QtWidgets.QPushButton("Import pyFAI Mask EDF")
        import_mask_button.clicked.connect(self._import_mask_edf_dialog)
        save_mask_button = QtWidgets.QPushButton("Save Mask As .npy")
        save_mask_button.clicked.connect(self._save_mask_dialog)
        load_mask_button = QtWidgets.QPushButton("Load Mask .npy")
        load_mask_button.clicked.connect(self._load_mask_dialog)

        form.addRow("", self.mask_note_label)
        mask_actions = QtWidgets.QWidget()
        mask_actions_layout = QtWidgets.QGridLayout(mask_actions)
        mask_actions_layout.setContentsMargins(0, 0, 0, 0)
        mask_actions_layout.addWidget(export_mask_edf_button, 0, 0)
        mask_actions_layout.addWidget(drawmask_button, 0, 1)
        mask_actions_layout.addWidget(import_mask_button, 1, 0, 1, 2)
        mask_actions_layout.addWidget(save_mask_button, 2, 0)
        mask_actions_layout.addWidget(load_mask_button, 2, 1)
        form.addRow("", mask_actions)
        form.addRow("Loaded mask", self.mask_path_label)
        form.addRow("Mask summary", self.mask_stats_label)
        self.mask_layout.addWidget(box)

    def _browse_hdf5(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open HDF5", "", "HDF5 files (*.h5 *.hdf5)")
        if path:
            self.file_path_edit.setText(path)
            self.load_hdf5_file(path)

    def load_hdf5_file(self, path: str) -> None:
        if not path:
            self.status_label.setText("Choose an HDF5 file first.")
            return
        dataset_path = self.dataset_path_edit.text().strip() or DEFAULT_DATASET_PATH
        try:
            loaded = load_hdf5_image(path, dataset_path=dataset_path)
        except Exception as exc:
            self.status_label.setText(f"Failed to load HDF5: {exc}")
            return

        self.current_file = loaded.path
        self.current_image = loaded.image
        self.current_dataset_path = loaded.dataset_path
        self.current_metadata = loaded.metadata
        self.current_mask = None
        self.current_mask_path = None
        self.current_poni_path = None
        self.current_edf_path = None
        self.current_mask_edf_path = None
        self.current_integrate_config_path = None
        self.current_integrate_output_dir = None
        self.current_radial_curve = None

        self.file_path_edit.setText(str(loaded.path))
        self.dataset_path_edit.setText(loaded.dataset_path)
        self.file_info_label.setText(f"{loaded.path.name} | shape={loaded.image.shape} | dtype={loaded.image.dtype}")
        detector = loaded.metadata.get("detector_name") or "unknown"
        self.detector_info_label.setText(str(detector))
        distance_source = loaded.metadata.get("distance_source")
        self.metadata_label.setText(
            f"energy_keV={loaded.metadata.get('mono_energy_keV')}, "
            f"SAXS_distance_m={loaded.metadata.get('distance_m')} ({distance_source}), "
            f"SPDS_point_detector={loaded.metadata.get('saxs_point_detector')}"
        )
        self.detector_position_label.setText(self._format_detector_positions(loaded.metadata))
        self.edf_path_label.setText("EDF bridge: -")
        self.poni_path_label.setText("PONI: -")
        self.poni_summary_label.setText("Calibration summary: -")
        self.integrate_config_label.setText("Config: -")
        self.integrate_output_label.setText("Output: -")
        self.integrate_output_dir_edit.setText(str(self._default_integrate_output_dir()))
        self.mask_path_label.setText("Mask: -")
        self.mask_stats_label.setText("Mask pixels: -")
        detector_text = loaded.metadata.get("detector_name") or "-"
        energy_text = loaded.metadata.get("mono_energy_keV")
        distance_text = loaded.metadata.get("distance_mm")
        self.calib2_prefill_label.setText(
            f"detector={detector_text}, energy_keV={energy_text if energy_text is not None else '-'}, "
            f"distance_mm={distance_text if distance_text is not None else '-'}"
        )
        self._update_image_view()
        self.status_label.setText("HDF5 loaded. You can export EDF, calibrate, mask, or launch pyFAI-integrate with metadata prefilled.")

    def _format_detector_positions(self, metadata: dict[str, object]) -> str:
        saxs = metadata.get("saxs_detector_position_mm") or metadata.get("saxs_detector_position") or {}
        waxs = metadata.get("waxs_detector_position_with_units") or metadata.get("waxs_detector_position") or {}

        def format_position(values: object) -> str:
            if not isinstance(values, dict) or not values:
                return "-"
            return ", ".join(f"{key}={value}" for key, value in values.items())

        return (
            "SAXS stage: "
            + format_position(saxs)
            + "\nWAXS detector: "
            + format_position(waxs)
            + "\nNote: linear motor readings are in mm. SAXS area detector distance is fixed near 5.6 m; "
            + "SPDS is the SAXS point detector; WD_Z is WAXS distance; WD_RY is WAXS rotation about y."
        )

    def _default_edf_path(self) -> Path:
        assert self.current_file is not None
        return self._pyfai_bridge_dir() / f"{self.current_file.stem}.pyfai_setup.edf"

    def _default_mask_edf_path(self) -> Path:
        assert self.current_file is not None
        return self._pyfai_bridge_dir() / f"{self.current_file.stem}.pyfai_mask_input.edf"

    def _default_integrate_config_path(self) -> Path:
        assert self.current_file is not None
        return self._pyfai_bridge_dir() / f"{self.current_file.stem}.azimint.json"

    def _default_integrate_output_dir(self) -> Path:
        assert self.current_file is not None
        run_stamp = time.strftime("%Y%m%d_%H%M%S")
        return self._pyfai_bridge_dir() / "integrated" / f"{self.current_file.stem}_{run_stamp}"

    def _browse_integrate_output_dir(self) -> None:
        start = self.integrate_output_dir_edit.text().strip()
        if not start and self.current_file is not None:
            start = str(self._default_integrate_output_dir())
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose pyFAI-integrate output folder",
            start or str(Path.home()),
        )
        if folder:
            self.integrate_output_dir_edit.setText(folder)

    def _pyfai_bridge_dir(self) -> Path:
        assert self.current_file is not None
        safe_name = "".join(char if char.isalnum() or char in "._-" else "_" for char in self.current_file.stem).strip("._")
        return PROJECT_ROOT / "outputs" / "pyfai_bridge" / (safe_name or "hdf5_image")

    def _export_current_edf(self) -> None:
        if self.current_image is None or self.current_file is None:
            self.status_label.setText("Load an HDF5 file before exporting EDF.")
            return
        path = self._default_edf_path()
        export_image_as_edf(path, self.current_image, metadata=self.current_metadata)
        self.current_edf_path = path
        self.edf_path_label.setText(str(path))
        self.status_label.setText(f"Exported EDF bridge to {path.name}.")

    def _launch_calib2(self) -> None:
        if self.current_image is None or self.current_file is None:
            self.status_label.setText("Load an HDF5 file before launching pyFAI-calib2.")
            return
        if self.current_edf_path is None or not self.current_edf_path.exists():
            self._export_current_edf()
        mask_path = None
        if self.current_mask is not None and self.current_file is not None:
            mask_path = self._pyfai_bridge_dir() / f"{self.current_file.stem}.pyfai_mask.edf"
            export_mask_as_edf(mask_path, self.current_mask)
        try:
            self._calib2_process = launch_pyfai_calib2(
                edf_path=self.current_edf_path,
                calibrant=self.calibrant_edit.text().strip() or "AgBh",
                detector_name=self.current_metadata.get("detector_name"),
                energy_kev=self._metadata_float("mono_energy_keV"),
                distance_m=self._metadata_distance_m(),
                mask_path=mask_path,
            )
        except Exception as exc:
            self.status_label.setText(f"Failed to launch pyFAI-calib2: {exc}")
            return
        self.status_label.setText(
            f"pyFAI-calib2 launched on {self.current_edf_path.name} with detector/energy/distance prefilled when available. "
            f"Save the .poni from pyFAI, then load it here."
        )

    def _load_poni_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load PONI", "", "PONI file (*.poni)")
        if not path:
            return
        self.current_poni_path = Path(path).expanduser().resolve()
        try:
            summary = load_poni_summary(self.current_poni_path)
        except Exception as exc:
            self.status_label.setText(f"Failed to load PONI: {exc}")
            return
        self.poni_path_label.setText(str(self.current_poni_path))
        self.poni_summary_label.setText(summary)
        self.status_label.setText("PONI loaded. You can now continue with mask authoring.")

    def _export_mask_edf_bridge(self) -> None:
        if self.current_image is None or self.current_file is None:
            self.status_label.setText("Load an HDF5 file before exporting a mask EDF bridge.")
            return
        path = self._default_mask_edf_path()
        export_image_as_edf(path, self.current_image, metadata=self.current_metadata)
        self.current_mask_edf_path = path
        self.status_label.setText(f"Exported mask EDF bridge to {path.name}.")

    def _launch_drawmask(self) -> None:
        if self.current_image is None or self.current_file is None:
            self.status_label.setText("Load an HDF5 file before launching pyFAI-drawmask.")
            return
        if self.current_mask_edf_path is None or not self.current_mask_edf_path.exists():
            self._export_mask_edf_bridge()
        try:
            self._drawmask_process = launch_pyfai_drawmask(self.current_mask_edf_path)
        except Exception as exc:
            self.status_label.setText(f"Failed to launch pyFAI-drawmask: {exc}")
            return
        self.status_label.setText(
            f"pyFAI-drawmask launched on {self.current_mask_edf_path.name}. Save the mask EDF in pyFAI, then import it here."
        )

    def _launch_integrate(self) -> None:
        if self.current_image is None or self.current_file is None:
            self.status_label.setText("Load an HDF5 file before launching pyFAI-integrate.")
            return
        if self.current_edf_path is None or not self.current_edf_path.exists():
            self._export_current_edf()
        mask_path = None
        if self.current_mask is not None:
            mask_path = self._pyfai_bridge_dir() / f"{self.current_file.stem}.pyfai_integrate_mask.edf"
            export_mask_as_edf(mask_path, self.current_mask)
        config_path = self._default_integrate_config_path()
        output_dir = Path(self.integrate_output_dir_edit.text().strip()).expanduser() if self.integrate_output_dir_edit.text().strip() else self._default_integrate_output_dir()
        try:
            self.current_integrate_config_path = write_pyfai_integrate_config(
                config_path,
                poni_path=self.current_poni_path if self.current_poni_path and self.current_poni_path.exists() else None,
                mask_path=mask_path,
                npt=self.integrate_npt_spin.value(),
                unit=self.integrate_unit_edit.text().strip() or "q_A^-1",
                h5_metadata=self.current_metadata,
                image_shape=tuple(int(part) for part in self.current_image.shape),
                azimuth_range=self._azimuth_range(),
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            self.current_integrate_output_dir = output_dir
            self._integrate_process = launch_pyfai_integrate(
                edf_path=self.current_edf_path,
                config_path=self.current_integrate_config_path,
                output_dir=output_dir,
            )
        except Exception as exc:
            self.status_label.setText(f"Failed to launch pyFAI-integrate: {exc}")
            return
        self.integrate_config_label.setText(str(self.current_integrate_config_path))
        self.integrate_output_label.setText(str(output_dir))
        self.status_label.setText(
            f"pyFAI-integrate launched on {self.current_edf_path.name} with HDF5 metadata"
            f"{' and loaded PONI' if self.current_poni_path else ''} linked."
        )

    def _integrate_current_image(self) -> None:
        if self.current_image is None:
            self.status_label.setText("Load an HDF5 file before integrating.")
            return
        if self.current_poni_path is None or not self.current_poni_path.exists():
            self.status_label.setText("Load a calibrated PONI before native radial integration.")
            return
        mask = self.current_mask.astype(bool) if self.current_mask is not None else None
        try:
            ai = load_poni(str(self.current_poni_path))
            variance = np.abs(np.asarray(self.current_image, dtype=np.float32))
            result = ai.integrate1d(
                self.current_image,
                self.integrate_npt_spin.value(),
                mask=mask,
                unit=self.integrate_unit_edit.text().strip() or "q_A^-1",
                variance=variance,
                error_model="poisson",
                azimuth_range=self._azimuth_range(),
            )
            q = np.asarray(result.radial, dtype=float)
            intensity = np.asarray(result.intensity, dtype=float)
            sigma = np.asarray(getattr(result, "sigma", np.full_like(intensity, np.nan)), dtype=float)
        except Exception as exc:
            self.status_label.setText(f"Failed to integrate current image: {exc}")
            return
        self.current_radial_curve = (q, intensity, sigma)
        self.image_ax.clear()
        keep = np.isfinite(q) & np.isfinite(intensity)
        self.image_ax.plot(q[keep], intensity[keep], lw=1.2)
        self.image_ax.set_title("Radial Integration Preview")
        self.image_ax.set_xlabel(self.integrate_unit_edit.text().strip() or "q_A^-1")
        self.image_ax.set_ylabel("I(q)")
        self.image_ax.set_yscale("log")
        self.figure.tight_layout()
        self.canvas.draw_idle()
        self.status_label.setText(f"Integrated current image: {np.count_nonzero(keep)} q bins.")

    def _save_current_radial_curve(self) -> None:
        if self.current_radial_curve is None:
            self.status_label.setText("Integrate the current image before saving I(q).")
            return
        start = str(self._pyfai_bridge_dir() / f"{self.current_file.stem}_radial.dat") if self.current_file else ""
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save radial I(q)", start, "DAT files (*.dat);;Text files (*.txt);;All files (*)")
        if not path:
            return
        q, intensity, sigma = self.current_radial_curve
        output = Path(path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        header = "\n".join(
            [
                "FrameByFrame-ASWAXS pyFAI radial integration preview",
                f"source_h5={self.current_file}",
                f"poni={self.current_poni_path}",
                f"unit={self.integrate_unit_edit.text().strip() or 'q_A^-1'}",
                f"azimuth_range_deg={self._azimuth_range()}",
                "columns=q I sigma_I",
            ]
        )
        np.savetxt(output, np.column_stack([q, intensity, sigma]), header=header, comments="#")
        self.status_label.setText(f"Saved radial I(q) to {output}.")

    def _azimuth_range(self) -> tuple[float, float] | None:
        lower = float(self.azimuth_min_spin.value())
        upper = float(self.azimuth_max_spin.value())
        if lower <= -180.0 and upper >= 180.0:
            return None
        if lower >= upper:
            raise ValueError("Azimuth min must be smaller than azimuth max.")
        return lower, upper

    def _import_mask_edf_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import pyFAI Mask EDF", "", "EDF files (*.edf)")
        if not path:
            return
        try:
            mask = load_mask_from_edf(path)
        except Exception as exc:
            self.status_label.setText(f"Failed to import mask EDF: {exc}")
            return
        self.current_mask = mask
        self.current_mask_path = Path(path).expanduser().resolve()
        self.mask_path_label.setText(str(self.current_mask_path))
        self.mask_stats_label.setText(f"Mask pixels: {int(mask.sum())}")
        self._update_image_view()
        self.status_label.setText("Imported mask EDF.")

    def _save_mask_dialog(self) -> None:
        if self.current_mask is None:
            self.status_label.setText("Import or load a mask before saving it.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Mask", "", "NumPy mask (*.npy)")
        if not path:
            return
        save_mask(path, self.current_mask)
        self.current_mask_path = Path(path).expanduser().resolve()
        self.mask_path_label.setText(str(self.current_mask_path))
        self.status_label.setText(f"Saved mask to {path}.")

    def _load_mask_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Mask", "", "NumPy mask (*.npy)")
        if not path:
            return
        try:
            mask = load_mask(path)
        except Exception as exc:
            self.status_label.setText(f"Failed to load mask: {exc}")
            return
        self.current_mask = mask
        self.current_mask_path = Path(path).expanduser().resolve()
        self.mask_path_label.setText(str(self.current_mask_path))
        self.mask_stats_label.setText(f"Mask pixels: {int(mask.sum())}")
        self._update_image_view()
        self.status_label.setText("Mask loaded.")

    def _update_image_view(self) -> None:
        self.image_ax.clear()
        if self.current_image is None:
            self.canvas.draw_idle()
            return
        self.image_ax.imshow(self.current_image, cmap="magma", origin="lower")
        if self.current_mask is not None:
            overlay = np.ma.masked_where(~self.current_mask.astype(bool), self.current_mask)
            self.image_ax.imshow(overlay, cmap="cool", origin="lower", alpha=0.35)
        self.image_ax.set_title("Detector View")
        self.image_ax.set_xlabel("column")
        self.image_ax.set_ylabel("row")
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def _metadata_float(self, key: str) -> float | None:
        value = self.current_metadata.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _metadata_distance_m(self) -> float | None:
        distance_m = self._metadata_float("distance_m")
        if distance_m is not None:
            return distance_m
        distance_mm = self._metadata_float("distance_mm")
        if distance_mm is None:
            return None
        return distance_mm * 1e-3


def run_app(initial_path: str | None = None) -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = PreprocessingWindow(initial_path=initial_path)
    window.show()
    return app.exec()
